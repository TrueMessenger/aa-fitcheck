from django.test import TestCase

from ..constants import Section
from ..models import FitSubmission
from ..services.check_runner import recheck_submission, review_submission, submit_fit
from ..services.doctrine_import import DoctrineImportError, import_fit
from ..services.eft_parser import parse_eft
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata

EFT_DOCTRINE = (
    "[Harbinger, Armor Brawl]\n"
    "Heat Sink II\n"
    "Heat Sink II\n"
    "Heat Sink II\n"
    "\n"
    "Cap Recharger II\n"
    "\n"
    "Focused Medium Pulse Laser II, Multifrequency L\n"
    "\n"
    "Hobgoblin II x5\n"
    "\n"
    "Multifrequency L x4\n"
)


class TestDoctrineImport(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.user = create_user(permissions=["basic_access", "manage_doctrines"])

    def test_import_creates_fit_with_items(self):
        fit = import_fit(EFT_DOCTRINE, self.user, doctrine=self.doctrine)
        self.assertIn(self.doctrine, fit.doctrines.all())
        self.assertEqual(fit.name, "Armor Brawl")
        self.assertEqual(fit.ship_type_id, T.HARBINGER)
        self.assertEqual(fit.eft_source, EFT_DOCTRINE)

        by_section = {}
        for item in fit.items.all():
            by_section.setdefault(item.section, {})[item.module_type_id] = item
        self.assertEqual(by_section[Section.LOW][T.HEAT_SINK_II].quantity, 3)
        self.assertEqual(by_section[Section.HIGH][T.PULSE_LASER_II].charge_type_id, T.MULTIFREQ_L)
        self.assertEqual(by_section[Section.DRONE_BAY][T.HOBGOBLIN_II].quantity, 5)
        self.assertEqual(by_section[Section.CARGO][T.MULTIFREQ_L].quantity, 4)

    def test_import_rejects_unparsable_fit(self):
        with self.assertRaises(DoctrineImportError) as ctx:
            import_fit("[Harbinger, X]\nNot A Real Module\n", self.user, doctrine=self.doctrine)
        self.assertTrue(ctx.exception.errors)


class TestCheckRunner(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.user = create_user("member")
        cls.reviewer = create_user("reviewer", permissions=["basic_access", "review_submissions"])
        cls.fit = create_fit(cls.doctrine, T.HARBINGER)
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)

    def _submit(self, eft):
        return submit_fit(
            self.user, self.fit, parse_eft(eft), source=FitSubmission.Source.EFT, eft_text=eft
        )

    def test_submit_persists_verdict_findings_and_log(self):
        submission = self._submit(
            "[Harbinger, Mine]\nHeat Sink II\nHeat Sink II\nImperial Navy Heat Sink\n"
        )
        self.assertEqual(submission.verdict, FitSubmission.Verdict.COMPLIANT_SUBS)
        self.assertEqual(submission.status, FitSubmission.Status.PENDING)
        self.assertTrue(submission.findings.exists())
        self.assertTrue(submission.items.exists())
        actions = list(submission.log.values_list("action", flat=True))
        self.assertIn("SUB", actions)
        self.assertIn("CHK", actions)

    def test_recheck_after_doctrine_change(self):
        submission = self._submit("[Harbinger, Mine]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n")
        self.assertEqual(submission.verdict, FitSubmission.Verdict.COMPLIANT)

        add_item(self.fit, Section.MED, T.CAP_RECHARGER_II, 1)
        self.fit.bump_version()
        self.assertTrue(FitSubmission.objects.get(pk=submission.pk).is_stale)

        recheck_submission(submission)
        self.assertEqual(submission.verdict, FitSubmission.Verdict.NON_COMPLIANT)
        self.assertFalse(FitSubmission.objects.get(pk=submission.pk).is_stale)

    def test_review_approve(self):
        submission = self._submit("[Harbinger, Mine]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n")
        review_submission(submission, self.reviewer, approve=True, comment="nice")
        self.assertEqual(submission.status, FitSubmission.Status.APPROVED)
        self.assertEqual(submission.reviewed_by, self.reviewer)

    def test_approve_never_requires_comment(self):
        """Approving never needs a comment - even if it contradicts the auto
        verdict. Releasing the pilot into the doctrine doesn't need text."""
        submission = self._submit("[Harbinger, Mine]\nHeat Sink II\n")  # missing 2 -> non-compliant
        self.assertEqual(submission.verdict, FitSubmission.Verdict.NON_COMPLIANT)
        review_submission(submission, self.reviewer, approve=True, comment="")
        self.assertEqual(submission.status, FitSubmission.Status.APPROVED)

    def test_reject_always_requires_comment(self):
        """Rejecting always needs a comment so the pilot knows what to fix.
        Even rejecting a COMPLIANT auto-verdict (rare but possible) needs text."""
        submission = self._submit("[Harbinger, Mine]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n")
        self.assertEqual(submission.verdict, FitSubmission.Verdict.COMPLIANT)
        with self.assertRaises(ValueError):
            review_submission(submission, self.reviewer, approve=False, comment="")
        with self.assertRaises(ValueError):
            review_submission(submission, self.reviewer, approve=False, comment="   ")  # whitespace
        review_submission(
            submission, self.reviewer, approve=False, comment="Refit your guns"
        )
        self.assertEqual(submission.status, FitSubmission.Status.REJECTED)
