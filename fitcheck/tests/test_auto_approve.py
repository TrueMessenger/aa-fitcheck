"""Per-doctrine auto-approve of inventory-verified compliant submissions.

A doctrine can opt in to approving, with no human reviewer, submissions that
(a) came from ESI inventory validation (never a pasted fit, which is
unverifiable text), (b) were graded against that doctrine, and (c) reached the
verdict tier the doctrine configured. Auto-approvals keep ``reviewed_by`` None
(the "by rule" marker), log an AUTO_APPROVED action, never ping reviewers, and
notify the pilot.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from allianceauth.notifications.models import Notification

from ..constants import Section
from ..models import Doctrine, FitSubmission, SubmissionActionLog
from ..services.check_runner import review_submission, submit_fit
from ..services.eft_parser import parse_eft
from ..signals import compliance_changed
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata

# 3x Heat Sink II == the doctrine requirement -> Compliant.
COMPLIANT_EFT = "[Harbinger, X]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"
# 3x a faction variant in the same family -> Compliant with substitutions.
SUBS_EFT = (
    "[Harbinger, X]\n"
    "Imperial Navy Heat Sink\nImperial Navy Heat Sink\nImperial Navy Heat Sink\n"
)
# 1 of the 3 required -> quantity short -> Non-compliant.
SHORT_EFT = "[Harbinger, X]\nHeat Sink II\n"


class TestAutoApprovesTier(TestCase):
    """Doctrine.auto_approves() - the pure verdict-tier gate, over every
    (mode, verdict) pair. Source/doctrine gating is the caller's job, not this
    method's, so it is exercised in the submit_fit tests below."""

    def test_matrix(self):
        V = FitSubmission.Verdict
        A = Doctrine.AutoApprove
        expected = {
            A.OFF: {
                V.COMPLIANT: False, V.COMPLIANT_SUBS: False,
                V.NON_COMPLIANT: False, V.ERROR: False,
            },
            A.COMPLIANT: {
                V.COMPLIANT: True, V.COMPLIANT_SUBS: False,
                V.NON_COMPLIANT: False, V.ERROR: False,
            },
            A.COMPLIANT_SUBS: {
                V.COMPLIANT: True, V.COMPLIANT_SUBS: True,
                V.NON_COMPLIANT: False, V.ERROR: False,
            },
        }
        for mode, verdicts in expected.items():
            doctrine = Doctrine(name=f"D-{mode}", auto_approve=mode)
            for verdict, want in verdicts.items():
                self.assertEqual(
                    doctrine.auto_approves(verdict),
                    want,
                    msg=f"mode={mode} verdict={verdict}",
                )


class AutoApproveBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Armor Brawl")
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        cls.member = create_user("member")
        cls.reviewer = create_user(
            "reviewer", permissions=["basic_access", "review_submissions"]
        )

    def setUp(self):
        cache.clear()

    _UNSET = object()

    def _submit(self, eft, *, source=FitSubmission.Source.ESI, doctrine=_UNSET):
        if doctrine is self._UNSET:
            doctrine = self.doctrine
        return submit_fit(
            self.member,
            self.fit,
            parse_eft(eft),
            source=source,
            doctrine=doctrine,
        )

    def _set_mode(self, mode):
        self.doctrine.auto_approve = mode
        self.doctrine.save(update_fields=["auto_approve"])


class TestAutoApproveOnSubmit(AutoApproveBase):
    def test_esi_compliant_under_compliant_mode_is_approved_by_rule(self):
        self._set_mode(Doctrine.AutoApprove.COMPLIANT)
        sub = self._submit(COMPLIANT_EFT)
        self.assertEqual(sub.verdict, FitSubmission.Verdict.COMPLIANT)
        self.assertEqual(sub.status, FitSubmission.Status.APPROVED)
        self.assertIsNone(sub.reviewed_by)
        self.assertIsNotNone(sub.reviewed_at)
        self.assertTrue(
            sub.log.filter(
                action=SubmissionActionLog.Action.AUTO_APPROVED, actor__isnull=True
            ).exists()
        )

    def test_esi_subs_under_compliant_mode_stays_pending(self):
        self._set_mode(Doctrine.AutoApprove.COMPLIANT)
        sub = self._submit(SUBS_EFT)
        self.assertEqual(sub.verdict, FitSubmission.Verdict.COMPLIANT_SUBS)
        self.assertEqual(sub.status, FitSubmission.Status.PENDING)
        self.assertFalse(
            sub.log.filter(action=SubmissionActionLog.Action.AUTO_APPROVED).exists()
        )

    def test_esi_subs_under_subs_mode_is_approved(self):
        self._set_mode(Doctrine.AutoApprove.COMPLIANT_SUBS)
        sub = self._submit(SUBS_EFT)
        self.assertEqual(sub.verdict, FitSubmission.Verdict.COMPLIANT_SUBS)
        self.assertEqual(sub.status, FitSubmission.Status.APPROVED)
        self.assertIsNone(sub.reviewed_by)

    def test_esi_compliant_under_subs_mode_is_approved(self):
        self._set_mode(Doctrine.AutoApprove.COMPLIANT_SUBS)
        sub = self._submit(COMPLIANT_EFT)
        self.assertEqual(sub.status, FitSubmission.Status.APPROVED)

    def test_esi_non_compliant_is_never_approved(self):
        self._set_mode(Doctrine.AutoApprove.COMPLIANT_SUBS)
        sub = self._submit(SHORT_EFT)
        self.assertEqual(sub.verdict, FitSubmission.Verdict.NON_COMPLIANT)
        self.assertEqual(sub.status, FitSubmission.Status.PENDING)

    def test_off_mode_never_approves(self):
        self._set_mode(Doctrine.AutoApprove.OFF)
        sub = self._submit(COMPLIANT_EFT)
        self.assertEqual(sub.status, FitSubmission.Status.PENDING)
        self.assertIsNone(sub.reviewed_at)

    def test_eft_source_is_never_auto_approved(self):
        # Pasted text can't be tied to a real hull - never auto-approve it, even
        # a Compliant verdict under the most permissive tier.
        self._set_mode(Doctrine.AutoApprove.COMPLIANT_SUBS)
        sub = self._submit(COMPLIANT_EFT, source=FitSubmission.Source.EFT)
        self.assertEqual(sub.verdict, FitSubmission.Verdict.COMPLIANT)
        self.assertEqual(sub.status, FitSubmission.Status.PENDING)

    def test_no_doctrine_is_never_auto_approved(self):
        # Graded against the fit's source defaults (doctrine=None, e.g. a
        # standalone fit or a reviewer's proactive audit): auto-approve never
        # applies, even though this fit's doctrine has it switched on.
        self._set_mode(Doctrine.AutoApprove.COMPLIANT_SUBS)
        sub = self._submit(COMPLIANT_EFT, doctrine=None)
        self.assertIsNone(sub.doctrine)
        self.assertEqual(sub.verdict, FitSubmission.Verdict.COMPLIANT)
        self.assertEqual(sub.status, FitSubmission.Status.PENDING)


class TestAutoApproveSignal(AutoApproveBase):
    def test_signal_emitted_once_with_final_approved_state(self):
        self._set_mode(Doctrine.AutoApprove.COMPLIANT)
        received = []

        def _record(sender, **kwargs):
            received.append(kwargs)

        compliance_changed.connect(_record)
        self.addCleanup(compliance_changed.disconnect, _record)

        sub = self._submit(COMPLIANT_EFT)

        # Exactly one emission, already carrying the final APPROVED state - not a
        # PENDING emission followed by an APPROVED one.
        self.assertEqual(len(received), 1)
        payload = received[0]
        self.assertIsNone(payload["old_status"])
        self.assertEqual(payload["new_status"], FitSubmission.Status.APPROVED)
        self.assertEqual(payload["new_verdict"], FitSubmission.Verdict.COMPLIANT)
        self.assertEqual(payload["submission"], sub)


class TestReviewerNotificationSuppression(AutoApproveBase):
    def test_no_reviewer_notification_for_auto_approved(self):
        from ..tasks import notify_reviewers_new_submission

        self._set_mode(Doctrine.AutoApprove.COMPLIANT)
        sub = self._submit(COMPLIANT_EFT)
        self.assertEqual(sub.status, FitSubmission.Status.APPROVED)
        Notification.objects.all().delete()

        notify_reviewers_new_submission(sub.pk)

        self.assertFalse(Notification.objects.filter(user=self.reviewer).exists())

    def test_reviewer_still_notified_for_pending(self):
        from ..tasks import notify_reviewers_new_submission

        self._set_mode(Doctrine.AutoApprove.OFF)
        sub = self._submit(COMPLIANT_EFT)
        self.assertEqual(sub.status, FitSubmission.Status.PENDING)
        Notification.objects.all().delete()

        notify_reviewers_new_submission(sub.pk)

        self.assertTrue(Notification.objects.filter(user=self.reviewer).exists())


class TestPilotNotification(AutoApproveBase):
    def test_auto_approved_notifies_pilot_by_rule(self):
        from ..tasks import notify_member_decision

        self._set_mode(Doctrine.AutoApprove.COMPLIANT)
        sub = self._submit(COMPLIANT_EFT)
        Notification.objects.all().delete()

        notify_member_decision(sub.pk)

        note = Notification.objects.get(user=self.member)
        self.assertIn("by rule", note.title)
        self.assertIn("automatically", note.message)

    def test_reviewerless_pending_submission_still_bails(self):
        from ..tasks import notify_member_decision

        self._set_mode(Doctrine.AutoApprove.OFF)
        sub = self._submit(COMPLIANT_EFT)
        self.assertEqual(sub.status, FitSubmission.Status.PENDING)
        self.assertIsNone(sub.reviewed_by)
        Notification.objects.all().delete()

        notify_member_decision(sub.pk)

        self.assertFalse(Notification.objects.filter(user=self.member).exists())


class TestRecheckViewAutoApproves(AutoApproveBase):
    def test_recheck_replacement_is_auto_approved(self):
        self._set_mode(Doctrine.AutoApprove.COMPLIANT)
        original = self._submit(COMPLIANT_EFT)
        self.client.force_login(self.member)

        with patch("fitcheck.tasks.notify_reviewers_new_submission.delay"), patch(
            "fitcheck.tasks.notify_member_decision.delay"
        ) as pilot_delay:
            response = self.client.post(
                reverse("fitcheck:submission_recheck", args=[original.pk]),
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            FitSubmission.objects.filter(pk=original.pk).exists()
        )  # replaced
        replacement = FitSubmission.objects.get(
            user=self.member, doctrine_fit=self.fit
        )
        self.assertEqual(replacement.status, FitSubmission.Status.APPROVED)
        self.assertIsNone(replacement.reviewed_by)
        self.assertIsNotNone(replacement.reviewed_at)
        # The pilot is told the outcome; the reviewer ping is a no-op for it.
        pilot_delay.assert_called_once_with(replacement.pk)


class TestHumanReviewUnaffected(AutoApproveBase):
    def test_human_approve_still_records_reviewer(self):
        self._set_mode(Doctrine.AutoApprove.OFF)
        sub = self._submit(COMPLIANT_EFT)
        self.assertEqual(sub.status, FitSubmission.Status.PENDING)

        review_submission(sub, self.reviewer, approve=True)

        sub.refresh_from_db()
        self.assertEqual(sub.status, FitSubmission.Status.APPROVED)
        self.assertEqual(sub.reviewed_by, self.reviewer)
        self.assertTrue(
            sub.log.filter(
                action=SubmissionActionLog.Action.APPROVED, actor=self.reviewer
            ).exists()
        )
        self.assertFalse(
            sub.log.filter(action=SubmissionActionLog.Action.AUTO_APPROVED).exists()
        )
