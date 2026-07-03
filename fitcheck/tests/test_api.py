"""Tests for the public compliance API (`fitcheck.services.api`)."""

from django.test import TestCase

from ..constants import Section
from ..models import FitSubmission
from ..services import api
from ..services.assignments import attach_fit_to_doctrine
from ..services.check_runner import review_submission, submit_fit
from ..services.eft_parser import parse_eft
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata

# 3x Heat Sink II in the low slots — matches the doctrine fit exactly.
COMPLIANT_EFT = "[Harbinger, Mine]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"
# Only one — short of the required three → QTY_SHORT → NON_COMPLIANT.
SHORT_EFT = "[Harbinger, Mine]\nHeat Sink II\n"


class ApiTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(None, T.HARBINGER, name="Armor Brawl")
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        # Proper attach so a doctrine-graded submission reads a real snapshot.
        attach_fit_to_doctrine(cls.fit, cls.doctrine)
        cls.pilot = create_user("pilot")
        cls.other = create_user("other")
        cls.reviewer = create_user(
            "reviewer", permissions=["basic_access", "review_submissions"]
        )

    def _submit(self, user, eft=COMPLIANT_EFT, *, doctrine=True):
        return submit_fit(
            user,
            self.fit,
            parse_eft(eft),
            eft_text=eft,
            doctrine=self.doctrine if doctrine else None,
        )


class TargetTests(ApiTestCase):
    def test_requires_a_target(self):
        with self.assertRaises(ValueError):
            api.get_qualifying_submission(self.pilot)
        with self.assertRaises(ValueError):
            list(api.iter_user_compliance([self.pilot]))

    def test_compliant_qualifies_by_doctrine(self):
        sub = self._submit(self.pilot)
        self.assertIn(sub.verdict, api.PASSING_VERDICTS)
        self.assertTrue(api.is_user_compliant(self.pilot, doctrine=self.doctrine))
        self.assertEqual(
            api.get_qualifying_submission(self.pilot, doctrine=self.doctrine), sub
        )

    def test_compliant_qualifies_by_fit(self):
        self._submit(self.pilot)
        self.assertTrue(api.is_user_compliant(self.pilot, fit=self.fit))

    def test_non_compliant_does_not_qualify(self):
        sub = self._submit(self.pilot, SHORT_EFT)
        self.assertEqual(sub.verdict, FitSubmission.Verdict.NON_COMPLIANT)
        self.assertFalse(api.is_user_compliant(self.pilot, doctrine=self.doctrine))
        self.assertIsNone(api.get_qualifying_submission(self.pilot, doctrine=self.doctrine))

    def test_other_users_compliance_is_independent(self):
        self._submit(self.pilot)
        self.assertTrue(api.is_user_compliant(self.pilot, doctrine=self.doctrine))
        self.assertFalse(api.is_user_compliant(self.other, doctrine=self.doctrine))

    def test_source_default_submission_excluded_from_doctrine_target(self):
        # Graded with doctrine=None (source defaults): counts for the fit, but
        # not as proof of compliance with the doctrine specifically.
        self._submit(self.pilot, doctrine=False)
        self.assertTrue(api.is_user_compliant(self.pilot, fit=self.fit))
        self.assertFalse(api.is_user_compliant(self.pilot, doctrine=self.doctrine))


class StalenessTests(ApiTestCase):
    def test_stale_excluded_by_default_included_when_not_required(self):
        self._submit(self.pilot)
        self.fit.bump_version()  # submission now graded against an older version
        self.assertFalse(api.is_user_compliant(self.pilot, doctrine=self.doctrine))
        self.assertTrue(
            api.is_user_compliant(
                self.pilot, doctrine=self.doctrine, require_current=False
            )
        )


class ReviewTests(ApiTestCase):
    def test_rejected_excluded_even_when_verdict_passes(self):
        sub = self._submit(self.pilot)
        review_submission(sub, self.reviewer, approve=False, comment="refit please")
        self.assertFalse(api.is_user_compliant(self.pilot, doctrine=self.doctrine))

    def test_require_approved_needs_reviewer_approval(self):
        sub = self._submit(self.pilot)
        # Compliant verdict, but pending review.
        self.assertTrue(api.is_user_compliant(self.pilot, doctrine=self.doctrine))
        self.assertFalse(
            api.is_user_compliant(
                self.pilot, doctrine=self.doctrine, require_approved=True
            )
        )
        review_submission(sub, self.reviewer, approve=True)
        self.assertTrue(
            api.is_user_compliant(
                self.pilot, doctrine=self.doctrine, require_approved=True
            )
        )


class IterComplianceTests(ApiTestCase):
    def test_bulk_results_track_input_order_and_membership(self):
        self._submit(self.pilot)  # compliant
        self._submit(self.other, SHORT_EFT)  # non-compliant
        results = list(
            api.iter_user_compliance(
                [self.pilot, self.other], doctrine=self.doctrine
            )
        )
        self.assertEqual([r.user_id for r in results], [self.pilot.pk, self.other.pk])
        self.assertEqual([r.is_compliant for r in results], [True, False])
        self.assertEqual(results[0].verdict, results[0].submission.verdict)
        self.assertIsNone(results[1].submission)

    def test_bulk_single_query(self):
        from ..models import EnforcementSettings

        self._submit(self.pilot)
        self._submit(self.other)
        EnforcementSettings.current()  # settings row exists before counting
        # 1 enforcement-settings read (staleness grace window) + 1 bulk
        # submissions query - still constant regardless of user count.
        with self.assertNumQueries(2):
            list(
                api.iter_user_compliance(
                    [self.pilot, self.other], doctrine=self.doctrine
                )
            )
