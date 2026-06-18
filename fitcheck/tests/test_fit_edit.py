"""Tests for the manual BOM-update flow (services/fit_edit.update_fit_bom):
archive the old version, carry per-item policy + overrides forward by
(section, type), and version-bump."""

from django.test import TestCase

from ..models import ArchivedFitVersion, FitItemOverride
from ..models.doctrine import SubstitutionPolicy
from ..services.doctrine_import import import_fit
from ..services.fit_edit import update_fit_bom
from .testdata.factories import create_user
from .testdata.sde_fixtures import T, create_sde_testdata

OLD_EFT = "[Harbinger, BOM Test]\nHeat Sink II\nCap Recharger II\n"
NEW_EFT = "[Harbinger, BOM Test]\nHeat Sink II\nStasis Webifier II\n"


class UpdateFitBomTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.user = create_user("manager", permissions=("basic_access", "manage_doctrines"))

    def _make_fit(self):
        fit = import_fit(OLD_EFT, self.user, name="BOM Test")
        # Customise the Heat Sink II policy + add an include override so we can
        # prove the carry-forward keeps non-default per-item policy.
        hs = fit.items.get(module_type_id=T.HEAT_SINK_II)
        hs.policy = SubstitutionPolicy.MEET_OR_BEAT
        hs.checked_attributes = [64]
        hs.min_quantity_pct = 100
        hs.notes = "keep this"
        hs.save()
        FitItemOverride.objects.create(
            item=hs, alt_type_id=T.HEAT_SINK_IMPERIAL, mode=FitItemOverride.Mode.INCLUDE
        )
        return fit

    def test_carries_policy_forward_and_archives(self):
        fit = self._make_fit()
        self.assertEqual(fit.version, 1)

        result = update_fit_bom(fit, NEW_EFT, self.user)
        fit.refresh_from_db()

        # Version bumped, BOM timestamp set.
        self.assertEqual(fit.version, 2)
        self.assertIsNotNone(fit.bom_updated_at)
        self.assertEqual(fit.eft_source, NEW_EFT)

        # Heat Sink II survived: its custom policy + override carried forward.
        hs = fit.items.get(module_type_id=T.HEAT_SINK_II)
        self.assertEqual(hs.policy, SubstitutionPolicy.MEET_OR_BEAT)
        self.assertEqual(hs.checked_attributes, [64])
        self.assertEqual(hs.notes, "keep this")
        self.assertTrue(
            hs.overrides.filter(alt_type_id=T.HEAT_SINK_IMPERIAL).exists()
        )

        # Cap Recharger removed; Stasis Webifier added with default policy.
        self.assertFalse(fit.items.filter(module_type_id=T.CAP_RECHARGER_II).exists())
        web = fit.items.get(module_type_id=T.WEB_II)
        self.assertEqual(web.policy, fit.default_policy)

        # Result summary by name.
        self.assertIn("Heat Sink II", result.carried)
        self.assertIn("Stasis Webifier II", result.added)
        self.assertIn("Cap Recharger II", result.dropped)

        # The superseded version is archived with its EFT + a policy snapshot.
        archive = ArchivedFitVersion.objects.get(fit=fit, version=1)
        self.assertEqual(archive.eft_source, OLD_EFT)
        self.assertEqual(len(archive.policy_snapshot["items"]), 2)
        snap_hs = next(
            row for row in archive.policy_snapshot["items"]
            if row["type_id"] == T.HEAT_SINK_II
        )
        self.assertEqual(snap_hs["policy"], SubstitutionPolicy.MEET_OR_BEAT)
        self.assertEqual(len(snap_hs["overrides"]), 1)

    def test_bad_eft_raises_and_changes_nothing(self):
        from ..services.doctrine_import import DoctrineImportError

        fit = self._make_fit()
        with self.assertRaises(DoctrineImportError):
            update_fit_bom(fit, "not a real fit", self.user)
        fit.refresh_from_db()
        self.assertEqual(fit.version, 1)
        self.assertFalse(ArchivedFitVersion.objects.filter(fit=fit).exists())


class UpdateFitBomAssignmentSnapshotTests(TestCase):
    """A BOM update must keep every per-(doctrine, fit) snapshot intact.

    Regression: `AssignmentItemPolicy.source_item` is CASCADE, so deleting the
    source items during the rebuild used to wipe all assignment snapshots -
    leaving submissions graded against the doctrine with an empty 'Doctrine
    Expects' (every pilot module flagged EXTRA)."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.user = create_user("manager", permissions=("basic_access", "manage_doctrines"))

    def _attach(self):
        from ..constants import Section
        from ..models import AssignmentItemOverride
        from ..services.assignments import attach_fit_to_doctrine
        from .testdata.factories import create_doctrine

        fit = import_fit(OLD_EFT, self.user, name="BOM Test")
        doctrine = create_doctrine("Avatar")
        assignment = attach_fit_to_doctrine(fit, doctrine, user=self.user)
        # Give the assignment a custom Heat Sink policy + override so we can
        # prove the PER-DOCTRINE exception (not just the source) carries forward.
        ai_hs = assignment.item_policies.get(module_type_id=T.HEAT_SINK_II)
        ai_hs.policy = SubstitutionPolicy.MEET_OR_BEAT
        ai_hs.notes = "doctrine-specific"
        ai_hs.save(update_fields=["policy", "notes"])
        AssignmentItemOverride.objects.create(
            assignment_item=ai_hs,
            alt_type_id=T.HEAT_SINK_IMPERIAL,
            mode=AssignmentItemOverride.Mode.INCLUDE,
        )
        return fit, doctrine, assignment

    def test_bom_update_preserves_assignment_snapshot(self):
        fit, _doctrine, assignment = self._attach()

        update_fit_bom(fit, NEW_EFT, self.user)

        policies = {p.module_type_id: p for p in assignment.item_policies.all()}
        # Heat Sink II survived with its per-doctrine policy + override carried.
        self.assertIn(T.HEAT_SINK_II, policies)
        hs = policies[T.HEAT_SINK_II]
        self.assertEqual(hs.policy, SubstitutionPolicy.MEET_OR_BEAT)
        self.assertEqual(hs.notes, "doctrine-specific")
        self.assertTrue(hs.overrides.filter(alt_type_id=T.HEAT_SINK_IMPERIAL).exists())
        # New module added to the snapshot; dropped module removed.
        self.assertIn(T.WEB_II, policies)
        self.assertNotIn(T.CAP_RECHARGER_II, policies)
        # Snapshot re-links to the NEW source items (not the deleted ones).
        new_hs_item = fit.items.get(module_type_id=T.HEAT_SINK_II)
        self.assertEqual(hs.source_item_id, new_hs_item.pk)

    def test_reclone_repairs_emptied_snapshot(self):
        """The one-shot repair re-clones a snapshot that a pre-fix update wiped."""
        from ..models import AssignmentItemPolicy
        from ..services.assignments import reclone_empty_assignment_snapshots

        fit, _doctrine, assignment = self._attach()
        # Simulate the old damage: drop the snapshot but keep the source items.
        AssignmentItemPolicy.objects.filter(assignment=assignment).delete()
        self.assertEqual(assignment.item_policies.count(), 0)

        repaired = reclone_empty_assignment_snapshots()

        self.assertIn(assignment.pk, repaired)
        rebuilt = {p.module_type_id for p in assignment.item_policies.all()}
        self.assertEqual(rebuilt, {i.module_type_id for i in fit.items.all()})
        # A second run is a no-op (idempotent - snapshot is no longer empty).
        self.assertEqual(reclone_empty_assignment_snapshots(), [])
