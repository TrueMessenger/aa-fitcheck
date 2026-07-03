"""fitcheck.signals.compliance_changed - fired on submit, re-check, and review.

Covers services.check_runner's three emission points: submit_fit (first
grading), recheck_submission (verdict/status carried across a re-grade), and
review_submission (reviewer decision).
"""

from django.test import TestCase

from ..constants import Section
from ..models import FitSubmission
from ..services.check_runner import recheck_submission, review_submission, submit_fit
from ..services.eft_parser import parse_eft
from ..signals import compliance_changed
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


class ComplianceChangedSignalCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Armor Brawl")
        cls.low_item = add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        cls.manager = create_user("manager", permissions=["basic_access", "manage_doctrines"])
        cls.member = create_user("member")

    def setUp(self):
        self.received = []

        def _record(sender, **kwargs):
            self.received.append(kwargs)

        self._receiver = _record
        compliance_changed.connect(self._receiver)
        self.addCleanup(compliance_changed.disconnect, self._receiver)

    def _submit_compliant(self):
        return submit_fit(
            self.member,
            self.fit,
            parse_eft("[Harbinger, X]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"),
        )

    def test_submit_fit_emits_first_grading(self):
        submission = self._submit_compliant()

        self.assertEqual(len(self.received), 1)
        payload = self.received[0]
        self.assertIsNone(payload["old_verdict"])
        self.assertIsNone(payload["old_status"])
        self.assertEqual(payload["new_status"], FitSubmission.Status.PENDING)
        self.assertEqual(payload["new_verdict"], submission.verdict)
        self.assertEqual(payload["new_verdict"], FitSubmission.Verdict.COMPLIANT)
        self.assertEqual(payload["actor"], self.member)
        self.assertEqual(payload["submission"], submission)
        self.assertEqual(payload["user"], self.member)
        self.assertEqual(payload["fit"], self.fit)
        self.assertIsNone(payload["doctrine"])

    def test_submit_fit_with_doctrine_carries_it_in_payload(self):
        submission = submit_fit(
            self.member,
            self.fit,
            parse_eft("[Harbinger, X]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"),
            doctrine=self.doctrine,
        )

        self.assertEqual(len(self.received), 1)
        payload = self.received[0]
        self.assertEqual(payload["doctrine"], self.doctrine)
        self.assertEqual(payload["submission"], submission)

    def test_recheck_emits_with_old_and_new_verdict(self):
        submission = self._submit_compliant()
        self.assertEqual(submission.verdict, FitSubmission.Verdict.COMPLIANT)
        self.received.clear()

        # Raise the required quantity so the same loadout now falls short.
        self.low_item.quantity = 5
        self.low_item.save(update_fields=["quantity"])
        self.fit.bump_version()

        recheck_submission(submission, actor=self.manager)

        self.assertEqual(len(self.received), 1)
        payload = self.received[0]
        self.assertEqual(payload["old_verdict"], FitSubmission.Verdict.COMPLIANT)
        self.assertEqual(payload["new_verdict"], FitSubmission.Verdict.NON_COMPLIANT)
        self.assertEqual(payload["old_status"], FitSubmission.Status.PENDING)
        self.assertEqual(payload["new_status"], FitSubmission.Status.PENDING)
        self.assertEqual(payload["actor"], self.manager)
        self.assertEqual(payload["submission"], submission)

    def test_recheck_actor_may_be_none_for_automated_rechecks(self):
        submission = self._submit_compliant()
        self.received.clear()

        recheck_submission(submission)

        self.assertEqual(len(self.received), 1)
        self.assertIsNone(self.received[0]["actor"])

    def test_review_approve_emits_status_change_with_unchanged_verdict(self):
        submission = self._submit_compliant()
        self.received.clear()

        review_submission(submission, self.manager, approve=True)

        self.assertEqual(len(self.received), 1)
        payload = self.received[0]
        self.assertEqual(payload["old_status"], FitSubmission.Status.PENDING)
        self.assertEqual(payload["new_status"], FitSubmission.Status.APPROVED)
        self.assertEqual(payload["old_verdict"], payload["new_verdict"])
        self.assertEqual(payload["actor"], self.manager)

    def test_review_reject_emits_rejected_status(self):
        submission = self._submit_compliant()
        self.received.clear()

        review_submission(submission, self.manager, approve=False, comment="Needs a refit")

        self.assertEqual(len(self.received), 1)
        payload = self.received[0]
        self.assertEqual(payload["old_status"], FitSubmission.Status.PENDING)
        self.assertEqual(payload["new_status"], FitSubmission.Status.REJECTED)
        self.assertEqual(payload["actor"], self.manager)
