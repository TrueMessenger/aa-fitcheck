"""Scoped staleness (issue #13): three independent version ladders (global,
source-policy, per-assignment) plus the admin-set compliance grace window.

Covers the isolation matrix on FitSubmission.is_stale, the assignment-editor
bump routing, API currency/grace behavior, scoped stale-pending notifications,
and with_staleness() annotation parity.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..constants import Section
from ..models import DoctrineFit, EnforcementSettings, FitAssignment, FitSubmission
from ..models.doctrine import SubstitutionPolicy
from ..services import api
from ..services.assignments import attach_fit_to_doctrine, detach_fit_from_doctrine
from ..services.check_runner import recheck_submission, submit_fit
from ..services.eft_parser import parse_eft
from ..views.manage import _stale_pending_count
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata

COMPLIANT_EFT = "[Harbinger, Mine]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"


class ScopedStalenessTestCase(TestCase):
    """Shared fixture: one fit, two doctrines (A/B) attached via the proper
    assignment path, three submissions (source-defaults, graded-under-A,
    graded-under-B)."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.manager = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        cls.pilot = create_user("pilot")
        cls.doctrine_a = create_doctrine("Doctrine A")
        cls.doctrine_b = create_doctrine("Doctrine B")
        cls.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        cls.assignment_a = attach_fit_to_doctrine(cls.fit, cls.doctrine_a, user=cls.manager)
        cls.assignment_b = attach_fit_to_doctrine(cls.fit, cls.doctrine_b, user=cls.manager)

    def _submit(self, *, doctrine=None, user=None):
        return submit_fit(
            user or self.pilot,
            self.fit,
            parse_eft(COMPLIANT_EFT),
            eft_text=COMPLIANT_EFT,
            doctrine=doctrine,
        )


class IsolationMatrixTests(ScopedStalenessTestCase):
    def setUp(self):
        super().setUp()
        self.sub_source = self._submit(doctrine=None)
        self.sub_a = self._submit(doctrine=self.doctrine_a)
        self.sub_b = self._submit(doctrine=self.doctrine_b)

    def test_bump_assignment_a_only_stales_a(self):
        self.assignment_a.bump_version()
        self.sub_source.refresh_from_db()
        self.sub_a.refresh_from_db()
        self.sub_b.refresh_from_db()
        self.assertFalse(self.sub_source.is_stale)
        self.assertTrue(self.sub_a.is_stale)
        self.assertFalse(self.sub_b.is_stale)

    def test_bump_source_policy_only_stales_source_defaults(self):
        self.fit.bump_source_policy_version()
        self.sub_source.refresh_from_db()
        self.sub_a.refresh_from_db()
        self.sub_b.refresh_from_db()
        self.assertTrue(self.sub_source.is_stale)
        self.assertFalse(self.sub_a.is_stale)
        self.assertFalse(self.sub_b.is_stale)

    def test_bump_global_ladder_stales_everything(self):
        self.fit.bump_version()
        self.sub_source.refresh_from_db()
        self.sub_a.refresh_from_db()
        self.sub_b.refresh_from_db()
        self.assertTrue(self.sub_source.is_stale)
        self.assertTrue(self.sub_a.is_stale)
        self.assertTrue(self.sub_b.is_stale)

    def test_deleting_assignment_stales_its_submission(self):
        detach_fit_from_doctrine(self.fit, self.doctrine_a)
        self.sub_source.refresh_from_db()
        self.sub_a.refresh_from_db()
        self.sub_b.refresh_from_db()
        self.assertTrue(self.sub_a.is_stale)
        self.assertFalse(self.sub_source.is_stale)
        self.assertFalse(self.sub_b.is_stale)

    def test_recheck_clears_policy_staleness(self):
        self.fit.bump_source_policy_version()
        self.sub_source.refresh_from_db()
        self.assertTrue(self.sub_source.is_stale)
        recheck_submission(self.sub_source)
        self.sub_source.refresh_from_db()
        self.assertEqual(self.sub_source.policy_version, self.fit.source_policy_version)
        self.assertFalse(self.sub_source.is_stale)


class AssignmentEditorBumpRoutingTests(ScopedStalenessTestCase):
    def test_assignment_editor_save_bumps_only_assignment_ladder(self):
        self.client.force_login(self.manager)
        policy = self.assignment_a.item_policies.get()
        url = reverse("fitcheck:manage_assignment_items", args=[self.assignment_a.pk])
        old_fit_version = self.fit.version
        old_source_policy_version = self.fit.source_policy_version
        old_assignment_version = self.assignment_a.version

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
        self.assertIn(response.status_code, (200, 302))

        self.assignment_a.refresh_from_db()
        self.fit.refresh_from_db()
        self.assertEqual(self.assignment_a.version, old_assignment_version + 1)
        self.assertEqual(self.fit.version, old_fit_version)
        self.assertEqual(self.fit.source_policy_version, old_source_policy_version)


class ApiCurrencyAndGraceTests(ScopedStalenessTestCase):
    def test_grace_zero_source_bump_stales_source_only(self):
        # Two pilots so each API query resolves against exactly one submission:
        # a mixed single-pilot set would let the still-current doctrine
        # submission mask the now-stale source one under `fit=`.
        source_pilot = self.pilot
        doctrine_pilot = create_user("doctrine_pilot")
        source_sub = self._submit(doctrine=None, user=source_pilot)
        doctrine_sub = self._submit(doctrine=self.doctrine_a, user=doctrine_pilot)
        self.fit.bump_source_policy_version()

        # Headline #13 behavior: the source-defaults submission goes
        # non-compliant, while the doctrine-graded submission for the same
        # fit is untouched by the source-ladder bump.
        self.assertFalse(api.is_user_compliant(source_pilot, fit=self.fit))
        self.assertTrue(
            api.is_user_compliant(doctrine_pilot, doctrine=self.doctrine_a, fit=self.fit)
        )
        source_sub.refresh_from_db()
        doctrine_sub.refresh_from_db()
        self.assertTrue(source_sub.is_stale)
        self.assertFalse(doctrine_sub.is_stale)

    def test_grace_seven_days_covers_recent_bump_then_expires(self):
        settings = EnforcementSettings.current()
        settings.stale_grace_days = 7
        settings.save(update_fields=["stale_grace_days"])
        sub = self._submit(doctrine=self.doctrine_a)
        self.assignment_a.bump_version()

        self.assertTrue(api.is_user_compliant(self.pilot, doctrine=self.doctrine_a))

        FitAssignment.objects.filter(pk=self.assignment_a.pk).update(
            version_bumped_at=timezone.now() - timedelta(days=8)
        )
        self.assertFalse(api.is_user_compliant(self.pilot, doctrine=self.doctrine_a))

    def test_null_bumped_at_expires_immediately_even_with_grace(self):
        settings = EnforcementSettings.current()
        settings.stale_grace_days = 7
        settings.save(update_fields=["stale_grace_days"])
        self._submit(doctrine=self.doctrine_a)
        # Simulate pre-migration data: move the ladder without the helper, so
        # version_bumped_at stays NULL.
        FitAssignment.objects.filter(pk=self.assignment_a.pk).update(version=999)

        self.assertFalse(api.is_user_compliant(self.pilot, doctrine=self.doctrine_a))

    def test_deleted_assignment_with_grace_expires_immediately(self):
        settings = EnforcementSettings.current()
        settings.stale_grace_days = 7
        settings.save(update_fields=["stale_grace_days"])
        self._submit(doctrine=self.doctrine_a)
        detach_fit_from_doctrine(self.fit, self.doctrine_a)

        self.assertFalse(api.is_user_compliant(self.pilot, doctrine=self.doctrine_a))

    def test_iter_user_compliance_agrees_with_is_user_compliant_mixed_grace(self):
        settings = EnforcementSettings.current()
        settings.stale_grace_days = 7
        settings.save(update_fields=["stale_grace_days"])
        self._submit(doctrine=self.doctrine_a, user=self.pilot)
        other = create_user("other")
        self._submit(doctrine=self.doctrine_a, user=other)

        self.assignment_a.bump_version()
        # Pilot's bump is within grace; backdate it for "other" past the window.
        FitAssignment.objects.filter(pk=self.assignment_a.pk).update(
            version_bumped_at=timezone.now()
        )
        expected_pilot = api.is_user_compliant(self.pilot, doctrine=self.doctrine_a)

        FitAssignment.objects.filter(pk=self.assignment_a.pk).update(
            version_bumped_at=timezone.now() - timedelta(days=8)
        )
        expected_other = api.is_user_compliant(other, doctrine=self.doctrine_a)

        results = {
            r.user_id: r.is_compliant
            for r in api.iter_user_compliance(
                [self.pilot, other], doctrine=self.doctrine_a
            )
        }
        self.assertEqual(results[self.pilot.pk], expected_other)  # same live state
        self.assertEqual(results[other.pk], expected_other)
        self.assertFalse(expected_other)


class ScopedNotificationTests(ScopedStalenessTestCase):
    def setUp(self):
        super().setUp()
        self.pilot_a = create_user("pilot_a")
        self.pilot_b = create_user("pilot_b")

    @patch("fitcheck.tasks.notify")
    def test_bump_assignment_a_notifies_only_pilot_a(self, notify_mock):
        from ..tasks import recheck_pending_submissions

        self._submit(doctrine=self.doctrine_a, user=self.pilot_a)
        self._submit(doctrine=self.doctrine_b, user=self.pilot_b)
        self.assignment_a.bump_version()

        recheck_pending_submissions(self.fit.pk)

        notified_users = {call.args[0] for call in notify_mock.call_args_list}
        self.assertIn(self.pilot_a, notified_users)
        self.assertNotIn(self.pilot_b, notified_users)

    def test_stale_pending_count_scoped_to_bumped_ladder(self):
        self._submit(doctrine=self.doctrine_a, user=self.pilot_a)
        self._submit(doctrine=self.doctrine_b, user=self.pilot_b)
        self.assignment_a.bump_version()

        self.assertEqual(_stale_pending_count(self.fit), 1)


class AnnotationParityTests(ScopedStalenessTestCase):
    def test_with_staleness_matches_unannotated_and_avoids_per_row_queries(self):
        other_pilot = create_user("other_pilot")
        source_sub = self._submit(doctrine=None)
        a_sub = self._submit(doctrine=self.doctrine_a, user=other_pilot)
        b_sub = self._submit(doctrine=self.doctrine_b)

        # Make it a genuinely mixed set: stale source-defaults submission,
        # fresh doctrine-A submission, and a doctrine-B submission whose
        # assignment gets deleted (basis gone -> stale).
        self.fit.bump_source_policy_version()
        detach_fit_from_doctrine(self.fit, self.doctrine_b)

        plain = {
            s.pk: s.is_stale
            for s in FitSubmission.objects.filter(
                pk__in=[source_sub.pk, a_sub.pk, b_sub.pk]
            )
        }
        annotated_qs = (
            FitSubmission.objects.filter(pk__in=[source_sub.pk, a_sub.pk, b_sub.pk])
            .select_related("doctrine_fit")
            .with_staleness()
        )

        with self.assertNumQueries(1):
            annotated = {s.pk: s.is_stale for s in annotated_qs}

        self.assertEqual(plain, annotated)
        self.assertTrue(plain[source_sub.pk])
        self.assertFalse(plain[a_sub.pk])
        self.assertTrue(plain[b_sub.pk])
