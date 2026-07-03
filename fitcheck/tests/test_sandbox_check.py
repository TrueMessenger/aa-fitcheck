from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from ..constants import Section
from ..models import ComplianceFinding, FitSubmission, SubmissionActionLog
from ..services.check_runner import build_finding_rows, submit_fit
from ..services.compliance import check_fit
from ..services.eft_parser import parse_eft
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata

EFT_GOOD = "[Harbinger, Mine]\nHeat Sink II\nHeat Sink II\nImperial Navy Heat Sink\n"


class SandboxCheckTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Armor Brawl")
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        cls.manager = create_user("manager", permissions=["basic_access", "manage_doctrines"])
        cls.member = create_user("member")


class TestSandboxCheckOnly(SandboxCheckTestCase):
    def test_check_only_creates_nothing(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:submit_eft", args=[self.fit.pk]),
            {"eft_text": EFT_GOOD, "mode": "check_only"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fitcheck/sandbox_results.html")
        self.assertContains(response, "Sandbox check - nothing was saved.")
        self.assertContains(response, "Compliant with substitutions")
        self.assertEqual(FitSubmission.objects.count(), 0)
        self.assertEqual(ComplianceFinding.objects.count(), 0)
        self.assertEqual(SubmissionActionLog.objects.count(), 0)

    def test_findings_render_from_unsaved_rows(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:submit_eft", args=[self.fit.pk]),
            {"eft_text": EFT_GOOD, "mode": "check_only"},
        )
        self.assertContains(response, "Heat Sink II")
        self.assertContains(response, "Imperial Navy Heat Sink")
        self.assertContains(response, "Allowed substitute")

    def test_doctrine_fan_out_shows_chip_and_stays_unsaved(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:submit_eft", args=[self.fit.pk]),
            {
                "eft_text": EFT_GOOD,
                "mode": "check_only",
                "doctrines": [str(self.doctrine.pk)],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.doctrine.name)
        self.assertEqual(FitSubmission.objects.count(), 0)
        self.assertEqual(ComplianceFinding.objects.count(), 0)
        self.assertEqual(SubmissionActionLog.objects.count(), 0)

    def test_normal_submit_unchanged(self):
        expected_findings = len(check_fit(parse_eft(EFT_GOOD), self.fit).findings)

        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:submit_eft", args=[self.fit.pk]),
            {"eft_text": EFT_GOOD},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        submission = FitSubmission.objects.get(user=self.manager)
        self.assertEqual(submission.verdict, FitSubmission.Verdict.COMPLIANT_SUBS)
        self.assertEqual(
            ComplianceFinding.objects.filter(submission=submission).count(),
            expected_findings,
        )
        self.assertEqual(
            SubmissionActionLog.objects.filter(submission=submission).count(), 2
        )
        self.assertRedirects(
            response,
            reverse("fitcheck:submission_detail", args=[submission.pk]),
        )

    def test_reviewer_notification_not_queued_in_sandbox(self):
        self.client.force_login(self.manager)
        with patch(
            "fitcheck.tasks.notify_reviewers_new_submission.delay"
        ) as mock_delay:
            response = self.client.post(
                reverse("fitcheck:submit_eft", args=[self.fit.pk]),
                {"eft_text": EFT_GOOD, "mode": "check_only"},
            )
            self.assertEqual(response.status_code, 200)
            mock_delay.assert_not_called()

            self.client.post(
                reverse("fitcheck:submit_eft", args=[self.fit.pk]),
                {"eft_text": EFT_GOOD},
            )
            mock_delay.assert_called_once()


class TestBuildFindingRowsParity(SandboxCheckTestCase):
    def test_unsaved_rows_match_persisted_findings(self):
        parsed = parse_eft(EFT_GOOD)
        result = check_fit(parsed, self.fit)

        rows = build_finding_rows(result)
        self.assertEqual(len(rows), len(result.findings))
        for row in rows:
            self.assertIsNone(row.pk)

        first_row = rows[0]
        first_finding = result.findings[0]
        self.assertEqual(first_row.code, first_finding.code)
        self.assertEqual(first_row.section, first_finding.section)
        self.assertEqual(first_row.message, first_finding.message[:500])

        submission = submit_fit(self.member, self.fit, parsed, eft_text=EFT_GOOD)
        persisted_sequence = list(
            submission.findings.order_by("sort_order", "pk").values_list(
                "code", "section", "sort_order"
            )
        )
        sandbox_sequence = [
            (row.code, row.section, row.sort_order) for row in rows
        ]
        self.assertEqual(persisted_sequence, sandbox_sequence)
