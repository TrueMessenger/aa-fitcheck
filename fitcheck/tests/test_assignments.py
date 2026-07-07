"""Tests for the per-(Doctrine, Fit) policy snapshot rework.

Two halves:

1. Attach/detach helpers correctly clone the source policies + overrides
   into FitAssignment + AssignmentItemPolicy + AssignmentItemOverride, and
   evolve independently of the fit's defaults once cloned.
2. The engine's `check_fit_for_doctrine` consumes the assignment's
   snapshot (not the fit defaults), so the same parsed fit can yield
   different verdicts in different doctrines.
"""

from django.test import TestCase
from django.urls import reverse
from eveuniverse.models import EveType

from ..constants import Section
from ..models import (
    AssignmentItemOverride,
    AssignmentItemPolicy,
    FitAssignment,
    FitItemOverride,
    FitSubmission,
)
from ..models.doctrine import SubstitutionPolicy
from ..services.assignments import (
    assignment_differs,
    assignment_item_differs,
    attach_fit_to_doctrine,
    detach_fit_from_doctrine,
    differing_assignments,
    resync_assignment_from_source,
)
from ..services.check_runner import recheck_submission, submit_fit
from ..services.compliance import check_fit, check_fit_for_doctrine
from ..services.eft_parser import parse_eft
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


class TestAttachAndDetach(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.doctrine = create_doctrine("Avatar")
        self.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(self.fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.VARIANTS)
        # Add a source-level override so we can prove it's cloned.
        navy_hs = EveType.objects.get(id=T.HEAT_SINK_IMPERIAL)
        hs_item = self.fit.items.get(module_type_id=T.HEAT_SINK_II)
        FitItemOverride.objects.create(
            item=hs_item, alt_type=navy_hs, mode=FitItemOverride.Mode.INCLUDE,
        )

    def test_attach_clones_policies_and_overrides(self):
        assignment = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.user)

        self.assertIsInstance(assignment, FitAssignment)
        self.assertIn(self.doctrine, self.fit.doctrines.all())  # back-compat M2M
        policies = list(assignment.item_policies.all())
        self.assertEqual(len(policies), 1)
        policy = policies[0]
        self.assertEqual(policy.module_type_id, T.HEAT_SINK_II)
        self.assertEqual(policy.quantity, 3)
        self.assertEqual(policy.policy, SubstitutionPolicy.VARIANTS)
        overrides = list(policy.overrides.all())
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0].alt_type_id, T.HEAT_SINK_IMPERIAL)
        self.assertEqual(overrides[0].mode, AssignmentItemOverride.Mode.INCLUDE)

    def test_attach_is_idempotent(self):
        first = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.user)
        second = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.user)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            AssignmentItemPolicy.objects.filter(assignment=first).count(), 1
        )

    def test_detach_removes_assignment_and_m2m(self):
        attach_fit_to_doctrine(self.fit, self.doctrine, user=self.user)
        result = detach_fit_from_doctrine(self.fit, self.doctrine)

        self.assertTrue(result)
        self.assertNotIn(self.doctrine, self.fit.doctrines.all())
        self.assertFalse(
            FitAssignment.objects.filter(doctrine=self.doctrine, fit=self.fit).exists()
        )

    def test_assignment_policies_evolve_independently_of_source(self):
        """Editing an AssignmentItemPolicy doesn't bleed back into the
        source DoctrineFitItem - that's the whole point of the snapshot."""
        attach_fit_to_doctrine(self.fit, self.doctrine, user=self.user)
        policy = AssignmentItemPolicy.objects.get(
            assignment__doctrine=self.doctrine, assignment__fit=self.fit
        )
        policy.policy = SubstitutionPolicy.EXACT
        policy.save(update_fields=["policy"])

        source_item = self.fit.items.first()
        self.assertEqual(source_item.policy, SubstitutionPolicy.VARIANTS)

    def test_editing_source_does_not_cascade_to_existing_assignment(self):
        """Conversely, changing the source fit's default after an attach
        doesn't move the assignment - the snapshot is frozen at attach time."""
        attach_fit_to_doctrine(self.fit, self.doctrine, user=self.user)
        source_item = self.fit.items.first()
        source_item.policy = SubstitutionPolicy.EXACT
        source_item.save(update_fields=["policy"])

        snapshot = AssignmentItemPolicy.objects.get(
            assignment__doctrine=self.doctrine, source_item=source_item
        )
        self.assertEqual(snapshot.policy, SubstitutionPolicy.VARIANTS)

    def test_attach_clones_charge_policy_pair(self):
        """The charge-demand governance pair (charge_policy/
        charge_min_quantity_pct) is copied from the fit onto the fresh
        assignment at attach time, same as the per-item policies."""
        self.fit.charge_policy = SubstitutionPolicy.ANY
        self.fit.charge_min_quantity_pct = 50
        self.fit.save(update_fields=["charge_policy", "charge_min_quantity_pct"])

        assignment = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.user)

        self.assertEqual(assignment.charge_policy, SubstitutionPolicy.ANY)
        self.assertEqual(assignment.charge_min_quantity_pct, 50)


class TestCheckFitForDoctrine(TestCase):
    """Engine adapter: same parsed fit, two doctrines, divergent verdicts
    when the per-doctrine policies differ."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.strict_doctrine = create_doctrine("Strict Armor")
        self.loose_doctrine = create_doctrine("Permissive Armor")
        self.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(self.fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.EXACT)

        attach_fit_to_doctrine(self.fit, self.strict_doctrine, user=self.user)
        attach_fit_to_doctrine(self.fit, self.loose_doctrine, user=self.user)

        # In the loose doctrine, swap the policy to Variants so navy Heat
        # Sinks (faction) are accepted in place of T2.
        loose_policy = AssignmentItemPolicy.objects.get(
            assignment__doctrine=self.loose_doctrine, module_type_id=T.HEAT_SINK_II
        )
        loose_policy.policy = SubstitutionPolicy.VARIANTS
        loose_policy.save(update_fields=["policy"])

    def test_no_assignment_falls_back_to_source_defaults(self):
        """Fits never attached to any doctrine still run with their
        defaults - check_fit() and check_fit_for_doctrine() agree."""
        standalone_doctrine = create_doctrine("Unattached")
        parsed = parse_eft(
            "[Harbinger, Mine]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"
        )
        # No assignment for (standalone_doctrine, fit) - we expect fallback.
        result = check_fit_for_doctrine(parsed, self.fit, standalone_doctrine)
        self.assertEqual(result.verdict, FitSubmission.Verdict.COMPLIANT)

    def test_divergent_verdicts_across_doctrines(self):
        """The pilot brought 2 navy Heat Sinks + 1 T2. Strict doctrine
        rejects the substitution (EXACT); loose doctrine accepts it (VARIANTS)."""
        parsed = parse_eft(
            "[Harbinger, Mine]\nImperial Navy Heat Sink\nImperial Navy Heat Sink\nHeat Sink II\n"
        )

        strict_result = check_fit_for_doctrine(parsed, self.fit, self.strict_doctrine)
        loose_result = check_fit_for_doctrine(parsed, self.fit, self.loose_doctrine)

        self.assertEqual(strict_result.verdict, FitSubmission.Verdict.NON_COMPLIANT)
        self.assertEqual(loose_result.verdict, FitSubmission.Verdict.COMPLIANT_SUBS)

    def test_assignment_overrides_apply_independently(self):
        """Adding an override on the assignment doesn't move the source."""
        navy_hs = EveType.objects.get(id=T.HEAT_SINK_IMPERIAL)
        strict_policy = AssignmentItemPolicy.objects.get(
            assignment__doctrine=self.strict_doctrine, module_type_id=T.HEAT_SINK_II
        )
        AssignmentItemOverride.objects.create(
            assignment_item=strict_policy, alt_type=navy_hs,
            mode=AssignmentItemOverride.Mode.INCLUDE,
        )

        parsed = parse_eft(
            "[Harbinger, Mine]\nImperial Navy Heat Sink\nImperial Navy Heat Sink\nHeat Sink II\n"
        )
        result = check_fit_for_doctrine(parsed, self.fit, self.strict_doctrine)
        self.assertEqual(result.verdict, FitSubmission.Verdict.COMPLIANT_SUBS)
        # Plain check_fit (source defaults, EXACT, no override) still rejects.
        source_only = check_fit(parsed, self.fit)
        self.assertEqual(source_only.verdict, FitSubmission.Verdict.NON_COMPLIANT)


class TestSubmissionGradingUsesSnapshot(TestCase):
    """The live grading path (submit_fit / recheck_submission) routes through
    check_fit_for_doctrine when the submission carries a doctrine, so the
    per-(doctrine, fit) snapshot - not the fit's source defaults - decides the
    verdict. This is the end-to-end half of the policy-snapshot rework."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.strict = create_doctrine("Strict")
        self.loose = create_doctrine("Loose")
        self.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(self.fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.EXACT)
        attach_fit_to_doctrine(self.fit, self.strict, user=self.user)
        attach_fit_to_doctrine(self.fit, self.loose, user=self.user)
        # Loosen only the loose doctrine's snapshot to Variants.
        loose_policy = AssignmentItemPolicy.objects.get(
            assignment__doctrine=self.loose, module_type_id=T.HEAT_SINK_II
        )
        loose_policy.policy = SubstitutionPolicy.VARIANTS
        loose_policy.save(update_fields=["policy"])
        self.eft = (
            "[Harbinger, Mine]\nImperial Navy Heat Sink\n"
            "Imperial Navy Heat Sink\nHeat Sink II\n"
        )

    def test_verdict_follows_the_doctrine_snapshot(self):
        strict_sub = submit_fit(
            self.user, self.fit, parse_eft(self.eft), doctrine=self.strict
        )
        loose_sub = submit_fit(
            self.user, self.fit, parse_eft(self.eft), doctrine=self.loose
        )
        self.assertEqual(strict_sub.doctrine, self.strict)
        self.assertEqual(strict_sub.verdict, FitSubmission.Verdict.NON_COMPLIANT)
        self.assertEqual(loose_sub.verdict, FitSubmission.Verdict.COMPLIANT_SUBS)

    def test_no_doctrine_grades_against_source_defaults(self):
        sub = submit_fit(self.user, self.fit, parse_eft(self.eft))
        self.assertIsNone(sub.doctrine)
        # Source default is EXACT - navy heat sinks are rejected.
        self.assertEqual(sub.verdict, FitSubmission.Verdict.NON_COMPLIANT)

    def test_recheck_preserves_doctrine_routing(self):
        loose_sub = submit_fit(
            self.user, self.fit, parse_eft(self.eft), doctrine=self.loose
        )
        self.assertEqual(loose_sub.verdict, FitSubmission.Verdict.COMPLIANT_SUBS)
        # Tighten the loose snapshot to EXACT, then re-check the stored items.
        policy = AssignmentItemPolicy.objects.get(
            assignment__doctrine=self.loose, module_type_id=T.HEAT_SINK_II
        )
        policy.policy = SubstitutionPolicy.EXACT
        policy.save(update_fields=["policy"])
        recheck_submission(loose_sub)
        loose_sub.refresh_from_db()
        self.assertEqual(loose_sub.doctrine, self.loose)
        self.assertEqual(loose_sub.verdict, FitSubmission.Verdict.NON_COMPLIANT)


class TestAssignmentItemsView(TestCase):
    """The per-(doctrine, fit) editor renders, accepts policy edits, and
    keeps changes scoped to the assignment - the source DoctrineFitItem
    policy is untouched."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.manager = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.doctrine = create_doctrine("Avatar")
        self.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(self.fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.VARIANTS)
        self.assignment = attach_fit_to_doctrine(
            self.fit, self.doctrine, user=self.manager
        )

    def test_view_403s_without_manage_perm(self):
        member = create_user("member")
        self.client.force_login(member)
        response = self.client.get(
            reverse("fitcheck:manage_assignment_items", args=[self.assignment.pk])
        )
        # @permission_required redirects to login.
        self.assertEqual(response.status_code, 302)

    def test_view_renders(self):
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("fitcheck:manage_assignment_items", args=[self.assignment.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Avatar")  # doctrine badge
        self.assertContains(response, "Brawl")   # fit name

    def test_post_edits_assignment_only(self):
        """Saving the formset moves the assignment policy without touching
        the source DoctrineFitItem."""
        self.client.force_login(self.manager)
        policy = AssignmentItemPolicy.objects.get(assignment=self.assignment)
        url = reverse("fitcheck:manage_assignment_items", args=[self.assignment.pk])
        response = self.client.post(
            url,
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-id": str(policy.pk),
                "form-0-policy": SubstitutionPolicy.EXACT,
                "form-0-allow_mutated": "on",
                "form-0-min_quantity_pct": "100",
                "form-0-notes": "",
            },
        )
        # Either a 302 redirect (success) or 200 with re-render (errors).
        self.assertIn(response.status_code, (200, 302))
        policy.refresh_from_db()
        self.assertEqual(policy.policy, SubstitutionPolicy.EXACT)
        source_item = self.fit.items.first()
        # Source default stayed VARIANTS.
        self.assertEqual(source_item.policy, SubstitutionPolicy.VARIANTS)


class TestAssignmentOverrideEditing(TestCase):
    """The per-assignment editor can now add/remove exceptions and edit abyssal
    attributes via dedicated endpoints, isolated from the source fit."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.client.force_login(self.user)
        self.doctrine = create_doctrine("Avatar")
        self.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(self.fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.MEET_OR_BEAT)
        add_item(self.fit, Section.MED, T.WEB_II, 1, policy=SubstitutionPolicy.MEET_OR_BEAT)
        self.assignment = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.user)
        self.ai_hs = self.assignment.item_policies.get(module_type_id=T.HEAT_SINK_II)
        self.ai_web = self.assignment.item_policies.get(module_type_id=T.WEB_II)

    def test_add_and_remove_override_isolated_from_source(self):
        resp = self.client.post(
            reverse("fitcheck:assignment_override_add_bulk", args=[self.ai_hs.pk]),
            {"type_ids": [T.HEAT_SINK_IMPERIAL], "mode": "I"},
        )
        self.assertEqual(resp.status_code, 302)
        ov = AssignmentItemOverride.objects.get(assignment_item=self.ai_hs)
        self.assertEqual(ov.alt_type_id, T.HEAT_SINK_IMPERIAL)
        # Source fit item did NOT gain the override (isolation).
        self.assertFalse(self.fit.items.get(module_type_id=T.HEAT_SINK_II).overrides.exists())

        resp = self.client.post(
            reverse("fitcheck:assignment_override_remove", args=[ov.pk])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(AssignmentItemOverride.objects.filter(pk=ov.pk).exists())

    def test_attribute_save_sets_bounds_on_assignment_only(self):
        resp = self.client.post(
            reverse("fitcheck:assignment_attribute_policy_save", args=[self.ai_web.pk]),
            {"attr_ids": [20, 54]},  # WEB_STRENGTH, WEB_RANGE
        )
        self.assertEqual(resp.status_code, 302)
        self.ai_web.refresh_from_db()
        self.assertEqual(set(self.ai_web.checked_attributes), {20, 54})
        # Source web item's attributes stayed at the default (empty).
        self.assertEqual(self.fit.items.get(module_type_id=T.WEB_II).checked_attributes, [])

    def test_attribute_candidates_endpoint_returns_abyssal_name(self):
        resp = self.client.get(
            reverse("fitcheck:assignment_attribute_candidates", args=[self.ai_web.pk])
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["base_type_id"], T.WEB_II)
        self.assertEqual(data["abyssal_name"], "Abyssal Stasis Webifier")


class TestFitSettingsDoesNotEditDoctrines(TestCase):
    """H1 regression: doctrine links own a per-(doctrine, fit) snapshot and must
    be written only through services/assignments (decision 7). The Fit Settings
    form no longer exposes `doctrines`, so a settings save - even one that smuggles
    a `doctrines` param - must leave the FitAssignment snapshot untouched."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.manager = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.client.force_login(self.manager)
        self.doctrine = create_doctrine("Avatar")
        self.other = create_doctrine("Other")
        self.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(self.fit, Section.LOW, T.HEAT_SINK_II, 3)
        attach_fit_to_doctrine(self.fit, self.doctrine, user=self.manager)

    def test_settings_save_ignores_doctrines_param(self):
        before = AssignmentItemPolicy.objects.filter(assignment__fit=self.fit).count()
        self.assertGreater(before, 0)

        resp = self.client.post(
            reverse("fitcheck:manage_fit_settings", args=[self.fit.pk]),
            {
                "name": "Brawl",
                "description": "",
                # Smuggle a doctrines param - the form must ignore it entirely.
                "doctrines": [str(self.other.pk)],
            },
        )
        self.assertIn(resp.status_code, (200, 302))
        self.fit.refresh_from_db()
        # Original link + snapshot survive; the bogus param attached nothing.
        self.assertIn(self.doctrine, self.fit.doctrines.all())
        self.assertNotIn(self.other, self.fit.doctrines.all())
        self.assertTrue(
            FitAssignment.objects.filter(fit=self.fit, doctrine=self.doctrine).exists()
        )
        self.assertFalse(
            FitAssignment.objects.filter(fit=self.fit, doctrine=self.other).exists()
        )
        self.assertEqual(
            AssignmentItemPolicy.objects.filter(assignment__fit=self.fit).count(), before
        )


class TestSnapshotDriftDetection(TestCase):
    """The per-(doctrine, fit) snapshot is a clone; editing the source template
    does NOT propagate. These helpers detect that drift so the UI can warn and
    offer a re-sync (the #454 root cause)."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.manager = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.doctrine = create_doctrine("Avatar")
        self.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(
            self.fit, Section.LOW, T.HEAT_SINK_II, 3,
            policy=SubstitutionPolicy.VARIANTS, min_quantity_pct=10,
        )
        self.assignment = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.manager)
        self.source = self.fit.items.get(module_type_id=T.HEAT_SINK_II)
        self.policy = self.assignment.item_policies.get(module_type_id=T.HEAT_SINK_II)

    def test_in_sync_after_attach(self):
        self.assertFalse(assignment_item_differs(self.policy))
        self.assertFalse(assignment_differs(self.assignment))
        self.assertEqual(differing_assignments(self.fit), set())

    def test_source_min_quantity_pct_change_is_drift(self):
        """The #454 case: editing the source template leeway leaves the snapshot
        stale - which the helpers must flag."""
        self.source.min_quantity_pct = 50
        self.source.save(update_fields=["min_quantity_pct"])
        self.assertTrue(assignment_item_differs(self.policy))
        self.assertTrue(assignment_differs(self.assignment))
        self.assertEqual(differing_assignments(self.fit), {self.assignment.pk})

    def test_source_policy_change_is_drift(self):
        self.source.policy = SubstitutionPolicy.ANY
        self.source.save(update_fields=["policy"])
        self.assertTrue(assignment_item_differs(self.policy))

    def test_override_change_is_drift(self):
        FitItemOverride.objects.create(
            item=self.source,
            alt_type=EveType.objects.get(id=T.HEAT_SINK_IMPERIAL),
            mode=FitItemOverride.Mode.INCLUDE,
        )
        self.assertTrue(assignment_item_differs(self.policy))

    def test_added_source_module_is_drift_even_when_rows_match(self):
        """A module added to the BOM after the snapshot was cloned drifts the
        assignment via the key-set check, even though every existing row matches."""
        add_item(self.fit, Section.MED, T.WEB_II, 1, policy=SubstitutionPolicy.VARIANTS)
        self.assertFalse(assignment_item_differs(self.policy))  # existing row fine
        self.assertTrue(assignment_differs(self.assignment))    # but BOM changed
        self.assertEqual(differing_assignments(self.fit), {self.assignment.pk})

    def test_reordered_meta_groups_is_not_drift(self):
        """Comparison is order-insensitive for the list fields."""
        self.policy.allowed_meta_groups = list(reversed(self.source.allowed_meta_groups))
        self.policy.save(update_fields=["allowed_meta_groups"])
        self.assertFalse(assignment_item_differs(self.policy))

    def test_charge_policy_pair_mismatch_is_drift(self):
        """The charge-demand governance pair lives on FitAssignment/DoctrineFit
        directly (no per-item row), so drift detection compares it separately
        from the item-level checks above."""
        self.fit.charge_min_quantity_pct = 50
        self.fit.save(update_fields=["charge_min_quantity_pct"])
        self.assertTrue(assignment_differs(self.assignment))
        self.assertEqual(differing_assignments(self.fit), {self.assignment.pk})

    def test_charge_policy_pair_in_sync_is_not_drift(self):
        self.assertEqual(
            self.assignment.charge_policy, self.fit.charge_policy
        )
        self.assertEqual(
            self.assignment.charge_min_quantity_pct, self.fit.charge_min_quantity_pct
        )
        self.assertFalse(assignment_differs(self.assignment))


class TestResyncFromSource(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.manager = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.doctrine = create_doctrine("Avatar")
        self.other = create_doctrine("Other")
        self.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(
            self.fit, Section.LOW, T.HEAT_SINK_II, 3,
            policy=SubstitutionPolicy.VARIANTS, min_quantity_pct=10,
        )
        self.assignment = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.manager)
        self.other_assignment = attach_fit_to_doctrine(self.fit, self.other, user=self.manager)
        self.source = self.fit.items.get(module_type_id=T.HEAT_SINK_II)

    def test_resync_restores_snapshot_to_template(self):
        # Drift the snapshot (a per-combination customization).
        policy = self.assignment.item_policies.get(module_type_id=T.HEAT_SINK_II)
        policy.min_quantity_pct = 100
        policy.policy = SubstitutionPolicy.EXACT
        policy.save(update_fields=["min_quantity_pct", "policy"])
        self.assertTrue(assignment_differs(self.assignment))

        resync_assignment_from_source(self.assignment)

        synced = self.assignment.item_policies.get(module_type_id=T.HEAT_SINK_II)
        self.assertEqual(synced.min_quantity_pct, 10)
        self.assertEqual(synced.policy, SubstitutionPolicy.VARIANTS)
        self.assertFalse(assignment_differs(self.assignment))

    def test_resync_re_copies_charge_policy_pair(self):
        self.fit.charge_policy = SubstitutionPolicy.ANY
        self.fit.charge_min_quantity_pct = 0
        self.fit.save(update_fields=["charge_policy", "charge_min_quantity_pct"])
        self.assertTrue(assignment_differs(self.assignment))

        resync_assignment_from_source(self.assignment)

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.charge_policy, SubstitutionPolicy.ANY)
        self.assertEqual(self.assignment.charge_min_quantity_pct, 0)
        self.assertFalse(assignment_differs(self.assignment))

    def test_resync_re_clones_overrides(self):
        FitItemOverride.objects.create(
            item=self.source,
            alt_type=EveType.objects.get(id=T.HEAT_SINK_IMPERIAL),
            mode=FitItemOverride.Mode.INCLUDE,
        )
        self.assertTrue(assignment_differs(self.assignment))
        resync_assignment_from_source(self.assignment)
        synced = self.assignment.item_policies.get(module_type_id=T.HEAT_SINK_II)
        self.assertEqual(
            {(o.alt_type_id, o.mode) for o in synced.overrides.all()},
            {(T.HEAT_SINK_IMPERIAL, FitItemOverride.Mode.INCLUDE)},
        )

    def test_resync_leaves_other_assignment_untouched(self):
        # Customize the OTHER assignment, then re-sync only the first.
        other_policy = self.other_assignment.item_policies.get(module_type_id=T.HEAT_SINK_II)
        other_policy.policy = SubstitutionPolicy.EXACT
        other_policy.save(update_fields=["policy"])
        self.source.min_quantity_pct = 50
        self.source.save(update_fields=["min_quantity_pct"])

        resync_assignment_from_source(self.assignment)

        # First re-synced to template; the other keeps its EXACT customization.
        other_policy.refresh_from_db()
        self.assertEqual(other_policy.policy, SubstitutionPolicy.EXACT)
        self.assertTrue(assignment_differs(self.other_assignment))

    def test_resync_view_requires_manage_perm(self):
        member = create_user("member")
        self.client.force_login(member)
        resp = self.client.post(
            reverse("fitcheck:manage_assignment_resync", args=[self.assignment.pk])
        )
        self.assertEqual(resp.status_code, 302)  # redirect to login

    def test_resync_view_rejects_get(self):
        self.client.force_login(self.manager)
        resp = self.client.get(
            reverse("fitcheck:manage_assignment_resync", args=[self.assignment.pk])
        )
        self.assertEqual(resp.status_code, 405)  # require_POST

    def test_resync_view_syncs_and_bumps_version(self):
        self.client.force_login(self.manager)
        self.source.min_quantity_pct = 50
        self.source.save(update_fields=["min_quantity_pct"])
        before_fit_version = self.fit.version
        before_assignment_version = self.assignment.version

        resp = self.client.post(
            reverse("fitcheck:manage_assignment_resync", args=[self.assignment.pk])
        )
        self.assertEqual(resp.status_code, 302)
        synced = self.assignment.item_policies.get(module_type_id=T.HEAT_SINK_II)
        self.assertEqual(synced.min_quantity_pct, 50)
        # A snapshot resync bumps the assignment's own ladder - only that
        # doctrine's submissions go stale; the global fit version is untouched.
        self.assignment.refresh_from_db()
        self.assertGreater(self.assignment.version, before_assignment_version)
        self.fit.refresh_from_db()
        self.assertEqual(self.fit.version, before_fit_version)


class TestDriftSurfacedInTemplates(TestCase):
    """Smoke tests: the drift state and the manager jump-links render."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.manager = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.client.force_login(self.manager)
        self.doctrine = create_doctrine("Avatar")
        self.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(
            self.fit, Section.LOW, T.HEAT_SINK_II, 3,
            policy=SubstitutionPolicy.VARIANTS, min_quantity_pct=10,
        )
        self.assignment = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.manager)

    def test_assignment_editor_shows_drift_banner_when_template_changed(self):
        source = self.fit.items.get(module_type_id=T.HEAT_SINK_II)
        source.min_quantity_pct = 50
        source.save(update_fields=["min_quantity_pct"])
        resp = self.client.get(
            reverse("fitcheck:manage_assignment_items", args=[self.assignment.pk])
        )
        self.assertContains(resp, "Re-sync from template")

    def test_assignment_editor_no_banner_when_in_sync(self):
        resp = self.client.get(
            reverse("fitcheck:manage_assignment_items", args=[self.assignment.pk])
        )
        self.assertNotContains(resp, "Re-sync from template")

    def test_source_editor_shows_used_in_panel(self):
        resp = self.client.get(
            reverse("fitcheck:manage_fit_items", args=[self.fit.pk])
        )
        self.assertContains(resp, "Used in 1 doctrine")
        self.assertContains(resp, "Avatar")

    def test_submission_detail_shows_combination_edit_link(self):
        eft = "[Harbinger, Mine]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"
        submission = submit_fit(
            self.manager, self.fit, parse_eft(eft), doctrine=self.doctrine
        )
        resp = self.client.get(
            reverse("fitcheck:submission_detail", args=[submission.pk])
        )
        self.assertContains(resp, "Edit policy for this combination")
        self.assertContains(
            resp, reverse("fitcheck:manage_assignment_items", args=[self.assignment.pk])
        )
