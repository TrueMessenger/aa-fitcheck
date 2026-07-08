"""Tests for admin per-type notification toggles (Settings -> Notifications)
and the per-user "mute all" preference.

Covers: the NotificationSettings singleton (incl. legacy-setting seeding on
first access), UserNotificationPreference.is_muted, the settings page, the
pilot-side mute toggle endpoint, and - driving the real tasks - that each
toggle actually gates its producer's Notification rows and that a muted user
is skipped while others in the same loop are unaffected.
"""

from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from allianceauth.notifications.models import Notification

from ..constants import Section
from ..models import (
    Doctrine,
    FitSubmission,
    NotificationSettings,
    UserNotificationPreference,
)
from ..services.check_runner import submit_fit
from ..services.eft_parser import parse_eft
from ..tasks import (
    notify_member_decision,
    notify_reviewers_new_submission,
    recheck_pending_submissions,
    send_review_digest,
)
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata

# 3x Heat Sink II == the doctrine requirement -> Compliant.
COMPLIANT_EFT = "[Harbinger, X]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"


def _grant(user, codename):
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="fitcheck", codename=codename)
    )
    return User.objects.get(pk=user.pk)  # refresh perm cache


class NotificationSettingsModelTests(TestCase):
    def test_current_creates_singleton_with_defaults(self):
        settings_obj = NotificationSettings.current()
        self.assertEqual(settings_obj.pk, 1)
        self.assertTrue(settings_obj.notify_reviewers_new_submission)
        self.assertFalse(settings_obj.reviewer_digest)
        self.assertTrue(settings_obj.notify_member_decision)
        self.assertTrue(settings_obj.notify_pilots_stale)

    def test_save_always_targets_the_single_row(self):
        NotificationSettings(notify_reviewers_new_submission=False).save()
        NotificationSettings(notify_reviewers_new_submission=True).save()
        self.assertEqual(NotificationSettings.objects.count(), 1)
        self.assertTrue(NotificationSettings.current().notify_reviewers_new_submission)

    @patch("fitcheck.app_settings.FITCHECK_NOTIFY_REVIEWERS", False)
    @patch("fitcheck.app_settings.FITCHECK_REVIEWER_DIGEST", True)
    @patch("fitcheck.app_settings.FITCHECK_NOTIFY_PILOTS_STALE", False)
    def test_first_access_seeds_from_legacy_app_settings(self):
        """clean_setting reads Django settings at import time, so app_settings
        module attributes are patched directly (not the Django setting) -
        NotificationSettings.current() re-imports them from fitcheck.app_settings
        on every call, so the patch takes effect."""
        settings_obj = NotificationSettings.current()
        self.assertFalse(settings_obj.notify_reviewers_new_submission)
        self.assertTrue(settings_obj.reviewer_digest)
        self.assertFalse(settings_obj.notify_pilots_stale)
        # No legacy equivalent for this type - always defaults True.
        self.assertTrue(settings_obj.notify_member_decision)

    @patch("fitcheck.app_settings.FITCHECK_NOTIFY_REVIEWERS", False)
    def test_second_access_does_not_reseed(self):
        first = NotificationSettings.current()
        first.notify_reviewers_new_submission = True
        first.save()
        with patch("fitcheck.app_settings.FITCHECK_NOTIFY_REVIEWERS", False):
            second = NotificationSettings.current()
        # The row already existed - the (patched) legacy default is ignored.
        self.assertTrue(second.notify_reviewers_new_submission)


class UserNotificationPreferenceTests(TestCase):
    def test_is_muted_false_when_no_row_exists(self):
        user = create_user("nopref")
        self.assertFalse(UserNotificationPreference.is_muted(user))

    def test_is_muted_false_for_none_or_anonymous(self):
        self.assertFalse(UserNotificationPreference.is_muted(None))

    def test_is_muted_reflects_row(self):
        user = create_user("hasmuted")
        UserNotificationPreference.objects.create(user=user, mute_all=True)
        self.assertTrue(UserNotificationPreference.is_muted(user))
        UserNotificationPreference.objects.filter(user=user).update(mute_all=False)
        self.assertFalse(UserNotificationPreference.is_muted(user))


class NotificationSettingsPageTests(TestCase):
    URL = "fitcheck:notification_settings"

    def test_requires_manage_policies(self):
        user = create_user("notif_nobody")
        self.client.force_login(user)
        response = self.client.get(reverse(self.URL))
        self.assertEqual(response.status_code, 302)  # AA perm decorator redirects

    def test_get_renders_defaults(self):
        user = _grant(create_user("notif_admin"), "manage_policies")
        self.client.force_login(user)
        response = self.client.get(reverse(self.URL))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Notification Settings")
        self.assertTrue(
            response.context["form"].instance.notify_reviewers_new_submission
        )

    def test_post_saves_and_rereads(self):
        user = _grant(create_user("notif_editor"), "manage_policies")
        self.client.force_login(user)
        response = self.client.post(
            reverse(self.URL),
            {
                "notify_reviewers_new_submission": "",
                "reviewer_digest": "on",
                "notify_member_decision": "",
                "notify_pilots_stale": "",
            },
        )
        self.assertRedirects(response, reverse(self.URL))
        settings_obj = NotificationSettings.current()
        self.assertFalse(settings_obj.notify_reviewers_new_submission)
        self.assertTrue(settings_obj.reviewer_digest)
        self.assertFalse(settings_obj.notify_member_decision)
        self.assertFalse(settings_obj.notify_pilots_stale)


class ToggleMuteEndpointTests(TestCase):
    URL = "fitcheck:toggle_notification_mute"

    def test_requires_login(self):
        response = self.client.post(reverse(self.URL), {"mute_all": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(UserNotificationPreference.objects.exists())

    def test_requires_basic_access(self):
        # create_user() without permissions has no fitcheck perms at all.
        user = User.objects.create_user(username="noperm", password="password")
        self.client.force_login(user)
        response = self.client.post(reverse(self.URL), {"mute_all": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(UserNotificationPreference.objects.exists())

    def test_post_mutes_own_row_only(self):
        # Not following the redirect: pilot_fittings triggers an SDE
        # auto-load check, and this test has no SDE fixture loaded.
        user = create_user("mute_me")
        other = create_user("mute_other")
        self.client.force_login(user)
        response = self.client.post(reverse(self.URL), {"mute_all": "on"})
        self.assertRedirects(
            response, reverse("fitcheck:pilot_fittings"), fetch_redirect_response=False
        )
        self.assertTrue(UserNotificationPreference.is_muted(user))
        self.assertFalse(UserNotificationPreference.is_muted(other))

    def test_unchecked_checkbox_unmutes(self):
        user = create_user("unmute_me")
        UserNotificationPreference.objects.create(user=user, mute_all=True)
        self.client.force_login(user)
        # An unchecked HTML checkbox is omitted from the POST body entirely.
        self.client.post(reverse(self.URL), {})
        self.assertFalse(UserNotificationPreference.is_muted(user))

    def test_get_not_allowed(self):
        user = create_user("mute_get")
        self.client.force_login(user)
        response = self.client.get(reverse(self.URL))
        self.assertEqual(response.status_code, 405)


class _NotifyDrivingBase(TestCase):
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
        cls.other_reviewer = create_user(
            "other_reviewer", permissions=["basic_access", "review_submissions"]
        )

    def _submit(self, user=None):
        return submit_fit(
            user or self.member,
            self.fit,
            parse_eft(COMPLIANT_EFT),
            source=FitSubmission.Source.ESI,
            doctrine=self.doctrine,
        )

    def _set(self, **fields):
        settings_obj = NotificationSettings.current()
        for name, value in fields.items():
            setattr(settings_obj, name, value)
        settings_obj.save()
        return settings_obj


class ReviewerPingToggleTests(_NotifyDrivingBase):
    def test_toggle_off_suppresses_reviewer_ping(self):
        self._set(notify_reviewers_new_submission=False)
        submission = self._submit()
        Notification.objects.all().delete()

        notify_reviewers_new_submission(submission.pk)

        self.assertFalse(Notification.objects.filter(user=self.reviewer).exists())

    def test_toggle_on_sends_reviewer_ping(self):
        self._set(notify_reviewers_new_submission=True)
        submission = self._submit()
        Notification.objects.all().delete()

        notify_reviewers_new_submission(submission.pk)

        self.assertTrue(Notification.objects.filter(user=self.reviewer).exists())

    def test_muted_reviewer_excluded_others_still_notified(self):
        self._set(notify_reviewers_new_submission=True)
        UserNotificationPreference.objects.create(user=self.reviewer, mute_all=True)
        submission = self._submit()
        Notification.objects.all().delete()

        notify_reviewers_new_submission(submission.pk)

        self.assertFalse(Notification.objects.filter(user=self.reviewer).exists())
        self.assertTrue(Notification.objects.filter(user=self.other_reviewer).exists())


class ReviewerDigestToggleTests(_NotifyDrivingBase):
    def test_toggle_off_sends_nothing(self):
        self._set(reviewer_digest=False)
        self._submit()
        Notification.objects.all().delete()

        send_review_digest()

        self.assertFalse(Notification.objects.filter(user=self.reviewer).exists())

    def test_toggle_on_sends_summary(self):
        self._set(reviewer_digest=True)
        self._submit()
        Notification.objects.all().delete()

        send_review_digest()

        self.assertTrue(Notification.objects.filter(user=self.reviewer).exists())

    def test_muted_reviewer_excluded_from_digest(self):
        self._set(reviewer_digest=True)
        UserNotificationPreference.objects.create(user=self.reviewer, mute_all=True)
        self._submit()
        Notification.objects.all().delete()

        send_review_digest()

        self.assertFalse(Notification.objects.filter(user=self.reviewer).exists())
        self.assertTrue(Notification.objects.filter(user=self.other_reviewer).exists())


class MemberDecisionToggleTests(_NotifyDrivingBase):
    def _decide(self, submission, approve=True):
        submission.status = (
            FitSubmission.Status.APPROVED if approve else FitSubmission.Status.REJECTED
        )
        submission.reviewed_by = self.reviewer
        submission.reviewed_at = timezone.now()
        submission.save()
        return submission

    def test_toggle_off_suppresses_decision_notice(self):
        self._set(notify_member_decision=False)
        submission = self._decide(self._submit())
        Notification.objects.all().delete()

        notify_member_decision(submission.pk)

        self.assertFalse(Notification.objects.filter(user=self.member).exists())

    def test_toggle_on_sends_decision_notice(self):
        self._set(notify_member_decision=True)
        submission = self._decide(self._submit())
        Notification.objects.all().delete()

        notify_member_decision(submission.pk)

        self.assertTrue(Notification.objects.filter(user=self.member).exists())

    def test_toggle_on_sends_notice_for_rule_approval(self):
        self._set(notify_member_decision=True)
        self.doctrine.auto_approve = Doctrine.AutoApprove.COMPLIANT
        self.doctrine.save(update_fields=["auto_approve"])
        submission = self._submit()
        self.assertIsNone(submission.reviewed_by)
        self.assertIsNotNone(submission.reviewed_at)
        Notification.objects.all().delete()

        notify_member_decision(submission.pk)

        note = Notification.objects.get(user=self.member)
        self.assertIn("by rule", note.title)

    def test_muted_pilot_gets_no_decision_notice_human(self):
        self._set(notify_member_decision=True)
        UserNotificationPreference.objects.create(user=self.member, mute_all=True)
        submission = self._decide(self._submit())
        Notification.objects.all().delete()

        notify_member_decision(submission.pk)

        self.assertFalse(Notification.objects.filter(user=self.member).exists())

    def test_muted_pilot_gets_no_decision_notice_by_rule(self):
        self._set(notify_member_decision=True)
        UserNotificationPreference.objects.create(user=self.member, mute_all=True)
        self.doctrine.auto_approve = Doctrine.AutoApprove.COMPLIANT
        self.doctrine.save(update_fields=["auto_approve"])
        submission = self._submit()
        Notification.objects.all().delete()

        notify_member_decision(submission.pk)

        self.assertFalse(Notification.objects.filter(user=self.member).exists())


class StaleRecheckToggleTests(_NotifyDrivingBase):
    def _archive_current_bom_and_change_it(self):
        """Archive the fit's current BOM at its current version, then mutate
        the live BOM and bump the version - mirrors update_fit_bom without
        pulling in the EFT-import machinery this task doesn't depend on."""
        from ..models import ArchivedFitVersion

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

    def test_toggle_off_suppresses_all_recheck_notifications(self):
        self._set(notify_pilots_stale=False)
        submission = self._submit()
        self._archive_current_bom_and_change_it()
        Notification.objects.all().delete()

        recheck_pending_submissions(self.fit.pk)

        self.assertFalse(Notification.objects.filter(user=self.member).exists())

    def test_toggle_on_sends_recheck_notification(self):
        self._set(notify_pilots_stale=True)
        submission = self._submit()
        self._archive_current_bom_and_change_it()
        Notification.objects.all().delete()

        recheck_pending_submissions(self.fit.pk)

        self.assertTrue(Notification.objects.filter(user=self.member).exists())

    def test_muted_pilot_gets_no_stale_notice(self):
        self._set(notify_pilots_stale=True)
        UserNotificationPreference.objects.create(user=self.member, mute_all=True)
        submission = self._submit()
        self._archive_current_bom_and_change_it()
        Notification.objects.all().delete()

        recheck_pending_submissions(self.fit.pk)

        self.assertFalse(Notification.objects.filter(user=self.member).exists())
