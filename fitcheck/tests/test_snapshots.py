"""Compliance snapshot collection, retention/purge lifecycle, the W003 deploy
check, and the Diagnostics reporting-data panel + controls."""

import datetime as dt
from unittest import mock

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .. import checks, tasks
from ..constants import Section
from ..models import ComplianceSnapshot, DoctrineCategory, FitSubmission
from ..services.assignments import attach_fit_to_doctrine
from ..services.check_runner import submit_fit
from ..services.eft_parser import parse_eft
from ..services.snapshots import purge_snapshots, take_snapshots
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata

COMPLIANT_EFT = "[Harbinger, Mine]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"
SHORT_EFT = "[Harbinger, Mine]\nHeat Sink II\n"


class SnapshotTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(None, T.HARBINGER, name="Armor Brawl")
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        attach_fit_to_doctrine(cls.fit, cls.doctrine)
        cls.compliant = create_user("snap-compliant")
        cls.failing = create_user("snap-failing")
        cls.silent = create_user("snap-silent")

    def _submit(self, user, eft):
        return submit_fit(
            user, self.fit, parse_eft(eft), eft_text=eft, doctrine=self.doctrine
        )


class TakeSnapshotsTests(SnapshotTestCase):
    def test_counts_split_audience_by_compliance_state(self):
        self._submit(self.compliant, COMPLIANT_EFT)
        self._submit(self.failing, SHORT_EFT)
        result = take_snapshots()
        self.assertEqual(result["doctrines"], 1)
        snap = ComplianceSnapshot.objects.get(doctrine=self.doctrine)
        self.assertEqual(snap.audience_count, 3)
        self.assertEqual(snap.compliant_count, 1)
        self.assertEqual(snap.compliant_subs_count, 0)
        self.assertEqual(snap.non_compliant_count, 1)
        self.assertEqual(snap.no_submission_count, 1)

    def test_category_restricts_audience(self):
        group = Group.objects.create(name="Snap Team")
        self.compliant.groups.add(group)
        category = DoctrineCategory.objects.create(name="Snap Cat")
        category.selected_groups.add(group)
        self.doctrine.categories.add(category)
        take_snapshots()
        snap = ComplianceSnapshot.objects.get(doctrine=self.doctrine)
        self.assertEqual(snap.audience_count, 1)
        self.assertEqual(snap.no_submission_count, 1)

    def test_same_day_rerun_updates_in_place(self):
        take_snapshots()
        self._submit(self.compliant, COMPLIANT_EFT)
        take_snapshots()
        self.assertEqual(
            ComplianceSnapshot.objects.filter(doctrine=self.doctrine).count(), 1
        )
        snap = ComplianceSnapshot.objects.get(doctrine=self.doctrine)
        self.assertEqual(snap.compliant_count, 1)

    def test_inactive_doctrine_skipped(self):
        self.doctrine.is_active = False
        self.doctrine.save()
        result = take_snapshots()
        self.assertEqual(result["doctrines"], 0)
        self.assertFalse(ComplianceSnapshot.objects.exists())

    def test_stale_submission_does_not_count_as_compliant(self):
        self._submit(self.compliant, COMPLIANT_EFT)
        self.fit.bump_version()
        take_snapshots()
        snap = ComplianceSnapshot.objects.get(doctrine=self.doctrine)
        self.assertEqual(snap.compliant_count, 0)
        self.assertEqual(snap.non_compliant_count, 1)


class PurgeSnapshotsTests(SnapshotTestCase):
    def _row(self, days_ago: int):
        return ComplianceSnapshot.objects.create(
            doctrine=self.doctrine,
            date=timezone.now().date() - dt.timedelta(days=days_ago),
        )

    def test_purge_older_than_keeps_recent_rows(self):
        self._row(0)
        self._row(10)
        old = self._row(400)
        deleted = purge_snapshots(older_than_days=30)
        self.assertEqual(deleted, 1)
        self.assertFalse(ComplianceSnapshot.objects.filter(pk=old.pk).exists())
        self.assertEqual(ComplianceSnapshot.objects.count(), 2)

    def test_purge_all(self):
        self._row(0)
        self._row(400)
        self.assertEqual(purge_snapshots(), 2)
        self.assertFalse(ComplianceSnapshot.objects.exists())

    def test_task_prunes_by_retention_setting(self):
        self._row(400)
        with mock.patch(
            "fitcheck.app_settings.FITCHECK_SNAPSHOT_RETENTION_DAYS", 30
        ):
            result = tasks.take_compliance_snapshots()
        self.assertEqual(result["pruned"], 1)
        # Today's row (written by the run itself) survives the prune.
        self.assertTrue(
            ComplianceSnapshot.objects.filter(date=timezone.now().date()).exists()
        )

    def test_task_keeps_forever_when_retention_zero(self):
        self._row(400)
        with mock.patch(
            "fitcheck.app_settings.FITCHECK_SNAPSHOT_RETENTION_DAYS", 0
        ):
            result = tasks.take_compliance_snapshots()
        self.assertNotIn("pruned", result)
        self.assertEqual(ComplianceSnapshot.objects.exclude(
            date=timezone.now().date()).count(), 1)


class SnapshotCheckTests(SnapshotTestCase):
    """fitcheck.W003 — quiet in test runs by design, so patch argv."""

    def _run_check(self):
        with mock.patch.object(checks, "sys") as fake_sys:
            fake_sys.argv = ["manage.py", "check"]
            return checks.snapshot_task_check(None)

    def test_quiet_when_not_in_use(self):
        # Active doctrine but no submissions yet - nothing to report on.
        self.assertEqual(self._run_check(), [])

    def test_warns_when_in_use_but_never_run(self):
        self._submit(self.compliant, COMPLIANT_EFT)
        warnings = self._run_check()
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].id, "fitcheck.W003")
        self.assertIn("never run", warnings[0].msg)

    def test_warns_when_stalled(self):
        self._submit(self.compliant, COMPLIANT_EFT)
        ComplianceSnapshot.objects.create(
            doctrine=self.doctrine,
            date=timezone.now().date() - dt.timedelta(days=10),
        )
        warnings = self._run_check()
        self.assertEqual(len(warnings), 1)
        self.assertIn("not run since", warnings[0].msg)

    def test_quiet_when_recent_snapshot_exists(self):
        self._submit(self.compliant, COMPLIANT_EFT)
        ComplianceSnapshot.objects.create(
            doctrine=self.doctrine, date=timezone.now().date()
        )
        self.assertEqual(self._run_check(), [])


class DiagnosticsPanelTests(SnapshotTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_user(
            "snap-admin", permissions=("basic_access", "manage_policies")
        )

    def test_health_summary_includes_snapshot_stats(self):
        from ..services import diagnostics

        ComplianceSnapshot.objects.create(
            doctrine=self.doctrine, date=timezone.now().date()
        )
        h = diagnostics.health_summary()
        self.assertEqual(h["snapshot_total"], 1)
        self.assertEqual(h["snapshot_doctrines"], 1)
        self.assertEqual(h["snapshot_newest"], timezone.now().date())
        self.assertIn("snapshot_retention_days", h)
        self.assertIn("snapshot_task_scheduled", h)

    def test_diagnostics_page_renders_panel_and_controls(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("fitcheck:diagnostics"))
        self.assertContains(resp, "Reporting data (snapshots)")
        self.assertContains(resp, "Reporting data controls")
        self.assertContains(resp, "Take snapshot now")
        self.assertContains(resp, "Purge all")

    def test_run_now_queues_task_and_redirects(self):
        self.client.force_login(self.admin)
        with mock.patch.object(tasks.take_compliance_snapshots, "delay") as delay:
            resp = self.client.post(reverse("fitcheck:snapshot_run_now"))
        delay.assert_called_once_with()
        self.assertRedirects(resp, reverse("fitcheck:diagnostics"))

    def test_run_now_rejects_get(self):
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(reverse("fitcheck:snapshot_run_now")).status_code, 405
        )

    def test_controls_require_manage_policies(self):
        self.client.force_login(self.compliant)  # basic_access only
        for name in ("snapshot_run_now", "snapshot_purge"):
            resp = self.client.post(reverse(f"fitcheck:{name}"))
            self.assertEqual(resp.status_code, 302)
            self.assertIn("login", resp["Location"])

    def test_purge_older_than(self):
        ComplianceSnapshot.objects.create(
            doctrine=self.doctrine,
            date=timezone.now().date() - dt.timedelta(days=400),
        )
        ComplianceSnapshot.objects.create(
            doctrine=self.doctrine, date=timezone.now().date()
        )
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("fitcheck:snapshot_purge"), {"keep_days": "30"}
        )
        self.assertRedirects(resp, reverse("fitcheck:diagnostics"))
        self.assertEqual(ComplianceSnapshot.objects.count(), 1)

    def test_purge_all_on_blank_days(self):
        ComplianceSnapshot.objects.create(
            doctrine=self.doctrine, date=timezone.now().date()
        )
        self.client.force_login(self.admin)
        self.client.post(reverse("fitcheck:snapshot_purge"), {"keep_days": ""})
        self.assertFalse(ComplianceSnapshot.objects.exists())

    def test_purge_rejects_non_numeric_days(self):
        ComplianceSnapshot.objects.create(
            doctrine=self.doctrine, date=timezone.now().date()
        )
        self.client.force_login(self.admin)
        self.client.post(reverse("fitcheck:snapshot_purge"), {"keep_days": "soon"})
        self.assertTrue(ComplianceSnapshot.objects.exists())

    def test_doctrine_delete_cascades_snapshots(self):
        ComplianceSnapshot.objects.create(
            doctrine=self.doctrine, date=timezone.now().date()
        )
        self.doctrine.delete()
        self.assertFalse(ComplianceSnapshot.objects.exists())
