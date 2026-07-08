from django.test import TestCase
from django.urls import reverse

from allianceauth.notifications.models import Notification

from ..constants import Section
from ..models import FitSubmission, NotificationSettings
from ..services.eft_parser import parse_eft
from ..services.check_runner import submit_fit
from ..tasks import send_review_digest
from ..models.doctrine import SubstitutionPolicy
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import Attrs, T, create_sde_testdata

ABYSSAL_EFT = "[Harbinger, Abyssal]\nAbyssal Stasis Webifier\n"

PYFA_ABYSSAL_EFT = (
    "[Harbinger, Abyssal]\n"
    "Abyssal Stasis Webifier [1]\n"
    "\n"
    "\n"
    "[1] Stasis Webifier II\n"
    "Gravid Stasis Webifier Mutaplasmid\n"
    "Maximum Velocity Bonus -62.5, Optimal Range 15000\n"
)


class MutatedFlowTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Web Fit")
        add_item(
            cls.fit, Section.MED, T.WEB_II, 1,
            policy=SubstitutionPolicy.MEET_OR_BEAT,
            checked_attributes=[Attrs.WEB_STRENGTH, Attrs.WEB_RANGE],
        )
        cls.member = create_user("member", permissions=["basic_access", "review_submissions"])
        cls.url = reverse("fitcheck:submit_eft", args=[cls.fit.pk])

    def _stat_fields(self, strength, range_):
        return {
            f"mstat-{T.WEB_ABYSSAL}-{Attrs.WEB_STRENGTH}": strength,
            f"mstat-{T.WEB_ABYSSAL}-{Attrs.WEB_RANGE}": range_,
        }


class TestManualMutatedStats(MutatedFlowTestCase):
    def test_ingame_paste_prompts_for_stats(self):
        self.client.force_login(self.member)
        response = self.client.post(self.url, {"eft_text": ABYSSAL_EFT})
        self.assertContains(response, "Maximum Velocity Bonus")
        self.assertContains(response, f"mstat-{T.WEB_ABYSSAL}-{Attrs.WEB_STRENGTH}")
        self.assertFalse(FitSubmission.objects.exists())

    def test_stats_step_with_winning_rolls_passes(self):
        self.client.force_login(self.member)
        response = self.client.post(
            self.url,
            {"eft_text": ABYSSAL_EFT, "stats_step": "1", **self._stat_fields("-62.5", "15000")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fitcheck/sandbox_results.html")
        self.assertContains(response, "Compliant with substitutions")
        self.assertFalse(FitSubmission.objects.exists())

    def test_stats_step_with_losing_roll_fails(self):
        self.client.force_login(self.member)
        response = self.client.post(
            self.url,
            {"eft_text": ABYSSAL_EFT, "stats_step": "1", **self._stat_fields("-55", "15000")},
        )
        self.assertContains(response, "Non-compliant")
        self.assertFalse(FitSubmission.objects.exists())

    def test_invalid_value_rerenders_form_without_submission(self):
        self.client.force_login(self.member)
        response = self.client.post(
            self.url,
            {"eft_text": ABYSSAL_EFT, "stats_step": "1", **self._stat_fields("not-a-number", "")},
        )
        self.assertContains(response, "Maximum Velocity Bonus")
        self.assertFalse(FitSubmission.objects.exists())

    def test_pyfa_export_skips_the_stats_step(self):
        self.client.force_login(self.member)
        response = self.client.post(self.url, {"eft_text": PYFA_ABYSSAL_EFT})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fitcheck/sandbox_results.html")
        self.assertContains(response, "Compliant with substitutions")
        self.assertFalse(FitSubmission.objects.exists())


class TestReviewDigest(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER)
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 1)
        cls.member = create_user("member")
        cls.reviewer = create_user(
            "reviewer", permissions=["basic_access", "review_submissions"]
        )

    def _submit(self):
        return submit_fit(
            self.member, self.fit, parse_eft("[Harbinger, X]\nHeat Sink II\n")
        )

    def _enable_digest(self):
        settings_obj = NotificationSettings.current()
        settings_obj.reviewer_digest = True
        settings_obj.save()

    def test_digest_summarizes_pending_queue(self):
        self._enable_digest()
        self._submit()
        self._submit()
        Notification.objects.all().delete()
        send_review_digest()
        notification = Notification.objects.get(user=self.reviewer)
        self.assertIn("2 submissions awaiting review", notification.title)
        self.assertIn(str(self.fit), notification.message)

    def test_digest_silent_when_queue_empty(self):
        self._enable_digest()
        send_review_digest()
        self.assertFalse(Notification.objects.filter(user=self.reviewer).exists())

    def test_digest_off_sends_nothing(self):
        self._submit()
        Notification.objects.all().delete()
        send_review_digest()
        self.assertFalse(Notification.objects.filter(user=self.reviewer).exists())

    def test_digest_mode_suppresses_immediate_notifications(self):
        from ..tasks import notify_reviewers_new_submission

        self._enable_digest()
        submission = self._submit()
        Notification.objects.all().delete()
        notify_reviewers_new_submission(submission.pk)
        self.assertFalse(Notification.objects.filter(user=self.reviewer).exists())


class TestStaleBadgeInQueue(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER)
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 1)
        cls.member = create_user("member")
        cls.reviewer = create_user(
            "reviewer", permissions=["basic_access", "review_submissions"]
        )

    def test_queue_marks_stale_submissions(self):
        submit_fit(self.member, self.fit, parse_eft("[Harbinger, X]\nHeat Sink II\n"))
        self.client.force_login(self.reviewer)
        url = reverse("fitcheck:review_queue")
        self.assertNotContains(self.client.get(url), "stale")
        self.fit.bump_version()
        self.assertContains(self.client.get(url), "stale")
