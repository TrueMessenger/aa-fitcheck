from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from ..constants import Section
from ..models import FitSubmission, SubmissionActionLog
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


class ReviewBulkTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Armor Brawl")
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 1)
        cls.member = create_user("member")
        cls.other_member = create_user("other")
        cls.reviewer = create_user(
            "reviewer", permissions=["basic_access", "review_submissions"]
        )

    def _make(
        self,
        *,
        user=None,
        status=FitSubmission.Status.PENDING,
        verdict=FitSubmission.Verdict.COMPLIANT,
        reviewed_by=None,
    ) -> FitSubmission:
        return FitSubmission.objects.create(
            user=user or self.member,
            doctrine_fit=self.fit,
            fit_version=self.fit.version,
            source=FitSubmission.Source.EFT,
            verdict=verdict,
            status=status,
            reviewed_by=reviewed_by,
        )


class TestBulkApprove(ReviewBulkTestCase):
    def test_mixed_selection_only_approves_pending_compliant_verdicts(self):
        pending_compliant = self._make(verdict=FitSubmission.Verdict.COMPLIANT)
        pending_compliant_subs = self._make(verdict=FitSubmission.Verdict.COMPLIANT_SUBS)
        pending_non_compliant = self._make(verdict=FitSubmission.Verdict.NON_COMPLIANT)
        pending_error = self._make(verdict=FitSubmission.Verdict.ERROR)
        already_approved = self._make(
            verdict=FitSubmission.Verdict.COMPLIANT,
            status=FitSubmission.Status.APPROVED,
            reviewed_by=self.reviewer,
        )
        others_rejected = self._make(
            user=self.other_member,
            verdict=FitSubmission.Verdict.COMPLIANT,
            status=FitSubmission.Status.REJECTED,
            reviewed_by=self.reviewer,
        )
        selected = [
            pending_compliant,
            pending_compliant_subs,
            pending_non_compliant,
            pending_error,
            already_approved,
            others_rejected,
        ]

        self.client.force_login(self.reviewer)
        with patch("fitcheck.tasks.notify_member_decision.delay") as mock_delay:
            response = self.client.post(
                reverse("fitcheck:review_submissions_approve_bulk"),
                {"submission_pks": [str(sub.pk) for sub in selected]},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Approved 2 submissions.")
        self.assertContains(response, "Skipped 4")

        for sub in (pending_compliant, pending_compliant_subs):
            sub.refresh_from_db()
            self.assertEqual(sub.status, FitSubmission.Status.APPROVED)
            self.assertEqual(sub.reviewed_by, self.reviewer)
            self.assertIsNotNone(sub.reviewed_at)
            self.assertTrue(
                SubmissionActionLog.objects.filter(
                    submission=sub, action=SubmissionActionLog.Action.APPROVED
                ).exists()
            )

        pending_non_compliant.refresh_from_db()
        self.assertEqual(pending_non_compliant.status, FitSubmission.Status.PENDING)
        pending_error.refresh_from_db()
        self.assertEqual(pending_error.status, FitSubmission.Status.PENDING)
        others_rejected.refresh_from_db()
        self.assertEqual(others_rejected.status, FitSubmission.Status.REJECTED)

        self.assertEqual(mock_delay.call_count, 2)
        mock_delay.assert_any_call(pending_compliant.pk)
        mock_delay.assert_any_call(pending_compliant_subs.pk)

    def test_empty_selection_is_a_no_op(self):
        self.client.force_login(self.reviewer)
        response = self.client.post(
            reverse("fitcheck:review_submissions_approve_bulk"), {}, follow=True
        )
        self.assertContains(response, "No submissions selected.")

    def test_all_skipped_selection_reports_none_approved(self):
        non_compliant = self._make(verdict=FitSubmission.Verdict.NON_COMPLIANT)
        self.client.force_login(self.reviewer)
        with patch("fitcheck.tasks.notify_member_decision.delay") as mock_delay:
            response = self.client.post(
                reverse("fitcheck:review_submissions_approve_bulk"),
                {"submission_pks": [str(non_compliant.pk)]},
                follow=True,
            )
        self.assertContains(response, "review them individually")
        mock_delay.assert_not_called()
        non_compliant.refresh_from_db()
        self.assertEqual(non_compliant.status, FitSubmission.Status.PENDING)

    def test_permission_gate_blocks_non_reviewers(self):
        member = create_user("plain")
        self.client.force_login(member)
        response = self.client.post(
            reverse("fitcheck:review_submissions_approve_bulk"),
            {"submission_pks": ["1"]},
        )
        self.assertEqual(response.status_code, 403)

    def test_get_is_not_allowed(self):
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse("fitcheck:review_submissions_approve_bulk"))
        self.assertEqual(response.status_code, 405)


class TestBulkDeleteStillWorks(ReviewBulkTestCase):
    """The pre-existing bulk-delete action is untouched by the new approve
    action sharing its form."""

    def test_reviewer_still_deletes_selected(self):
        sub = self._make()
        self.client.force_login(self.reviewer)
        self.client.post(
            reverse("fitcheck:review_submissions_delete_bulk"),
            {"submission_pks": [str(sub.pk)]},
        )
        self.assertFalse(FitSubmission.objects.filter(pk=sub.pk).exists())

    def test_member_still_cannot_bulk_delete(self):
        member = create_user("plain")
        self.client.force_login(member)
        response = self.client.post(
            reverse("fitcheck:review_submissions_delete_bulk"),
            {"submission_pks": ["1"]},
        )
        self.assertEqual(response.status_code, 403)
