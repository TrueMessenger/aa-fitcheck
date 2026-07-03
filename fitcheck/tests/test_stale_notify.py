"""Stale-submission notifications and the old->new BOM diff they carry.

Covers services.fit_diff (bom_diff / archive_for_version / diff_for_submission),
the recheck_pending_submissions task's pilot/approved-holder notifications, and
the submission_detail "what changed" panel.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from ..constants import Section
from ..models import ArchivedFitVersion, FitSubmission, SdeType
from ..services.check_runner import submit_fit
from ..services.eft_parser import parse_eft
from ..services.fit_diff import archive_for_version, bom_diff, diff_for_submission
from ..tasks import recheck_pending_submissions
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


def _snapshot_row(section, type_id, name, qty, charge_type_id=None):
    row = {"section": section, "type_id": type_id, "name": name, "qty": qty}
    if charge_type_id is not None:
        row["charge_type_id"] = charge_type_id
    return row


class BomDiffCase(TestCase):
    """Unit tests for services.fit_diff.bom_diff and its two callers."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Armor Brawl")
        cls.hs_item = add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        cls.web_item = add_item(cls.fit, Section.MED, T.WEB_II, 1)

    def test_added_module_appears_in_added(self):
        old_items = [
            _snapshot_row(Section.LOW, T.HEAT_SINK_II, "Heat Sink II", 3),
        ]
        diff = bom_diff(old_items, self.fit)
        self.assertEqual(len(diff.added), 1)
        entry = diff.added[0]
        self.assertEqual(entry.name, "Stasis Webifier II")
        self.assertEqual(entry.new_qty, 1)
        self.assertEqual(entry.section_label, Section.MED.label)

    def test_removed_module_appears_in_removed_with_snapshot_name(self):
        old_items = [
            _snapshot_row(Section.LOW, T.HEAT_SINK_II, "Heat Sink II", 3),
            _snapshot_row(Section.MED, T.WEB_II, "Stasis Webifier II", 1),
            _snapshot_row(Section.MED, T.CAP_RECHARGER_II, "Cap Recharger II", 1),
        ]
        diff = bom_diff(old_items, self.fit)
        self.assertEqual(len(diff.removed), 1)
        entry = diff.removed[0]
        self.assertEqual(entry.name, "Cap Recharger II")
        self.assertEqual(entry.old_qty, 1)

    def test_quantity_change_appears_in_changed(self):
        old_items = [
            _snapshot_row(Section.LOW, T.HEAT_SINK_II, "Heat Sink II", 2),
            _snapshot_row(Section.MED, T.WEB_II, "Stasis Webifier II", 1),
        ]
        diff = bom_diff(old_items, self.fit)
        self.assertEqual(len(diff.changed), 1)
        entry = diff.changed[0]
        self.assertEqual(entry.name, "Heat Sink II")
        self.assertEqual(entry.old_qty, 2)
        self.assertEqual(entry.new_qty, 3)

    def test_loaded_charge_swap_resolves_names_with_fallback(self):
        # T.MULTIFREQ_L has an SdeType row from the fixtures; T.MULTIFREQ_L_NAVY's
        # row is removed so the resolver must fall back to "Type <id>" for it.
        SdeType.objects.filter(type_id=T.MULTIFREQ_L_NAVY).delete()
        current_item = add_item(
            self.fit, Section.HIGH, T.PULSE_LASER_II, 1, charge_type_id=T.MULTIFREQ_L_NAVY
        )
        old_items = [
            _snapshot_row(Section.LOW, T.HEAT_SINK_II, "Heat Sink II", 3),
            _snapshot_row(Section.MED, T.WEB_II, "Stasis Webifier II", 1),
            _snapshot_row(
                Section.HIGH,
                T.PULSE_LASER_II,
                "Focused Medium Pulse Laser II",
                1,
                charge_type_id=T.MULTIFREQ_L,
            ),
        ]
        diff = bom_diff(old_items, self.fit)
        current_item.delete()
        self.assertEqual(len(diff.changed), 1)
        entry = diff.changed[0]
        self.assertEqual(entry.name, "Focused Medium Pulse Laser II")
        self.assertIsNone(entry.old_qty)
        self.assertIsNone(entry.new_qty)
        self.assertEqual(entry.old_charge, "Multifrequency L")
        self.assertEqual(entry.new_charge, f"Type {T.MULTIFREQ_L_NAVY}")

    def test_identical_boms_produce_empty_diff(self):
        old_items = [
            _snapshot_row(Section.LOW, T.HEAT_SINK_II, "Heat Sink II", 3),
            _snapshot_row(Section.MED, T.WEB_II, "Stasis Webifier II", 1),
        ]
        diff = bom_diff(old_items, self.fit)
        self.assertTrue(diff.is_empty)
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.removed, [])
        self.assertEqual(diff.changed, [])

    def test_archive_for_version_picks_earliest_covering_archive(self):
        ArchivedFitVersion.objects.create(
            fit=self.fit, version=1, eft_source="v1", ship_type_id=T.HARBINGER
        )
        ArchivedFitVersion.objects.create(
            fit=self.fit, version=3, eft_source="v3", ship_type_id=T.HARBINGER
        )
        ArchivedFitVersion.objects.create(
            fit=self.fit, version=5, eft_source="v5", ship_type_id=T.HARBINGER
        )
        # A submission at version 2 should land on the archive at 3 (earliest
        # archive whose version is >= 2), not the one at 5.
        archive = archive_for_version(self.fit, 2)
        self.assertEqual(archive.version, 3)

    def test_archive_for_version_returns_none_when_none_qualifies(self):
        ArchivedFitVersion.objects.create(
            fit=self.fit, version=1, eft_source="v1", ship_type_id=T.HARBINGER
        )
        archive = archive_for_version(self.fit, 5)
        self.assertIsNone(archive)

    def test_diff_for_submission_none_when_fit_has_no_archives(self):
        member = create_user("member")
        eft = "[Harbinger, X]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"
        submission = submit_fit(member, self.fit, parse_eft(eft))
        self.assertIsNone(diff_for_submission(submission))


class RecheckTaskNotificationCase(TestCase):
    """Behavior of tasks.recheck_pending_submissions around staleness."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Armor Brawl")
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        cls.member = create_user("member")

    def setUp(self):
        cache.clear()

    def _submit(self):
        return submit_fit(
            self.member,
            self.fit,
            parse_eft("[Harbinger, X]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"),
        )

    def _archive_current_bom_and_change_it(self):
        """Archive the fit's current BOM at its current version, then mutate the
        live BOM and bump the version - mirrors what update_fit_bom does, without
        pulling in the EFT-import machinery this task doesn't depend on."""
        old_version = self.fit.version
        item = self.fit.items.get(module_type_id=T.HEAT_SINK_II)
        ArchivedFitVersion.objects.create(
            fit=self.fit,
            version=old_version,
            eft_source=self.fit.eft_source,
            ship_type_id=T.HARBINGER,
            policy_snapshot={
                "items": [
                    {
                        "section": item.section,
                        "type_id": item.module_type_id,
                        "name": item.module_type.name,
                        "qty": item.quantity,
                    }
                ]
            },
        )
        item.quantity = 5
        item.save(update_fields=["quantity"])
        self.fit.bump_version()

    @patch("fitcheck.tasks.notify")
    def test_stale_pending_with_archive_notifies_with_diff(self, notify_mock):
        submission = self._submit()
        self._archive_current_bom_and_change_it()

        recheck_pending_submissions(self.fit.pk)

        notify_mock.assert_called_once()
        _args, kwargs = notify_mock.call_args
        self.assertEqual(kwargs["level"], "warning")
        self.assertIn("Heat Sink II", kwargs["message"])
        self.assertIn("3", kwargs["message"])
        self.assertIn("5", kwargs["message"])
        submission.refresh_from_db()
        self.assertEqual(submission.fit_version, self.fit.version)

    @patch("fitcheck.tasks.notify")
    def test_stale_pending_without_archive_uses_fallback_text(self, notify_mock):
        self._submit()
        self.fit.bump_version()  # policy-only bump - no archive covers this submission

        recheck_pending_submissions(self.fit.pk)

        notify_mock.assert_called_once()
        _args, kwargs = notify_mock.call_args
        self.assertEqual(kwargs["level"], "warning")
        self.assertIn("policy rules changed", kwargs["message"])

    @patch("fitcheck.tasks.notify")
    def test_stale_approved_notified_once_and_never_regraded(self, notify_mock):
        submission = self._submit()
        submission.status = FitSubmission.Status.APPROVED
        submission.save(update_fields=["status"])
        self._archive_current_bom_and_change_it()
        old_verdict = submission.verdict
        old_fit_version = submission.fit_version

        recheck_pending_submissions(self.fit.pk)

        notify_mock.assert_called_once()
        _args, kwargs = notify_mock.call_args
        self.assertEqual(kwargs["level"], "info")
        submission.refresh_from_db()
        self.assertEqual(submission.status, FitSubmission.Status.APPROVED)
        self.assertEqual(submission.verdict, old_verdict)
        self.assertEqual(submission.fit_version, old_fit_version)

        # Same fit version - repeat run must not re-notify (cache guard).
        notify_mock.reset_mock()
        recheck_pending_submissions(self.fit.pk)
        notify_mock.assert_not_called()

    @patch("fitcheck.tasks.notify")
    def test_stale_rejected_never_notified(self, notify_mock):
        submission = self._submit()
        submission.status = FitSubmission.Status.REJECTED
        submission.save(update_fields=["status"])
        self._archive_current_bom_and_change_it()

        recheck_pending_submissions(self.fit.pk)

        notify_mock.assert_not_called()

    @patch("fitcheck.tasks.FITCHECK_NOTIFY_PILOTS_STALE", False)
    @patch("fitcheck.tasks.notify")
    def test_notify_disabled_suppresses_stale_notifications(self, notify_mock):
        submission = self._submit()
        self._archive_current_bom_and_change_it()

        recheck_pending_submissions(self.fit.pk)

        # Pending re-grade still happens (fit_version catches up)...
        submission.refresh_from_db()
        self.assertEqual(submission.fit_version, self.fit.version)
        # ...but no stale-style notify (with its "re-checked"/diff wording) fires.
        # The BOM change here also flips the verdict, so the old verdict-changed
        # branch legitimately still notifies once - it uses different copy and
        # carries no diff appendix, so we assert on that instead of call count.
        for call in notify_mock.call_args_list:
            message = call.kwargs.get("message", "")
            self.assertNotIn("What changed:", message)
            self.assertNotIn("policy rules changed", message)

    @patch("fitcheck.tasks.notify")
    def test_non_stale_pending_with_unchanged_verdict_not_notified(self, notify_mock):
        self._submit()  # not stale: fit_version still matches fit.version

        recheck_pending_submissions(self.fit.pk)

        notify_mock.assert_not_called()


class SubmissionDetailStaleDiffViewCase(TestCase):
    """View-level rendering of the "what changed" panel."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Armor Brawl")
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        cls.member = create_user("member")

    def setUp(self):
        cache.clear()

    def _submit(self):
        return submit_fit(
            self.member,
            self.fit,
            parse_eft("[Harbinger, X]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"),
        )

    def test_stale_submission_shows_diff_panel(self):
        submission = self._submit()
        old_version = self.fit.version
        item = self.fit.items.get(module_type_id=T.HEAT_SINK_II)
        ArchivedFitVersion.objects.create(
            fit=self.fit,
            version=old_version,
            eft_source=self.fit.eft_source,
            ship_type_id=T.HARBINGER,
            policy_snapshot={
                "items": [
                    {
                        "section": item.section,
                        "type_id": item.module_type_id,
                        "name": item.module_type.name,
                        "qty": item.quantity,
                    }
                ]
            },
        )
        item.quantity = 5
        item.save(update_fields=["quantity"])
        self.fit.bump_version()

        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:submission_detail", args=[submission.pk])
        )
        self.assertContains(
            response, "The fit changed after this submission was graded"
        )
        self.assertContains(response, "Heat Sink II")

    def test_non_stale_submission_has_no_diff_panel(self):
        submission = self._submit()
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:submission_detail", args=[submission.pk])
        )
        self.assertNotContains(
            response, "The fit changed after this submission was graded"
        )
