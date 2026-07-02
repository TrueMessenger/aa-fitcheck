"""Manual-only recheck of stale pending submissions (requirement 2).

Auto-recheck was removed from every policy/override save site to avoid
saturating the Celery queue during policy iteration. Managers now flush
stale submissions via the per-fit button or the bulk Recheck Stale page.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from ..constants import Section
from ..services.check_runner import submit_fit
from ..services.eft_parser import parse_eft
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


class ManualRecheckCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Armor Brawl")
        cls.low_item = add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        cls.manager = create_user("manager", permissions=["basic_access", "manage_doctrines"])
        cls.member = create_user("member")

    def setUp(self):
        # Per-fit cooldown lives in cache; isolate each test from siblings.
        cache.clear()

    def _formset_data(self, changes=None):
        """Minimal valid formset POST for the fit_items editor."""
        from ..models import DoctrineFitItem

        changes = changes or {}
        items = list(DoctrineFitItem.objects.filter(fit=self.fit).order_by("pk"))
        data = {
            "form-TOTAL_FORMS": str(len(items)),
            "form-INITIAL_FORMS": str(len(items)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        for index, item in enumerate(items):
            prefix = f"form-{index}"
            values = {
                "id": str(item.pk),
                "policy": item.policy,
                "allow_mutated": "on" if item.allow_mutated else "",
                "min_quantity_pct": str(item.min_quantity_pct),
                "notes": item.notes,
            }
            values.update(changes.get(item.pk, {}))
            for key, value in values.items():
                if key == "allow_mutated" and not value:
                    continue
                data[f"{prefix}-{key}"] = value
        return data


class TestAutoRecheckRemoved(ManualRecheckCase):
    """Saving the fit/policy/override no longer enqueues recheck_pending_submissions."""

    def test_policy_save_bumps_version_but_does_not_enqueue_recheck(self):
        self.client.force_login(self.manager)
        from ..models.doctrine import SubstitutionPolicy

        old_version = self.fit.version
        with patch("fitcheck.tasks.recheck_pending_submissions.delay") as delay:
            self.client.post(
                reverse("fitcheck:manage_fit_items", args=[self.fit.pk]),
                self._formset_data(
                    {self.low_item.pk: {"policy": SubstitutionPolicy.EXACT}}
                ),
                follow=True,
            )
        delay.assert_not_called()
        self.fit.refresh_from_db()
        self.assertEqual(self.fit.version, old_version + 1)

    def test_override_add_does_not_enqueue_recheck(self):
        self.client.force_login(self.manager)
        with patch("fitcheck.tasks.recheck_pending_submissions.delay") as delay:
            self.client.post(
                reverse("fitcheck:override_add", args=[self.low_item.pk]),
                {"type_name": "Imperial Navy Heat Sink", "mode": "I"},
                follow=True,
            )
        delay.assert_not_called()

    def test_fit_settings_save_does_not_enqueue_recheck(self):
        self.client.force_login(self.manager)
        with patch("fitcheck.tasks.recheck_pending_submissions.delay") as delay:
            self.client.post(
                reverse("fitcheck:manage_fit_settings", args=[self.fit.pk]),
                {
                    "name": self.fit.name,
                    "description": "tweak",
                    "is_active": "on",
                    "default_policy": self.fit.default_policy,
                },
                follow=True,
            )
        delay.assert_not_called()

    def test_stale_badge_still_appears_after_policy_change(self):
        """The stale-badge mechanism depends on version bumps, not on recheck."""
        submit_fit(
            self.member, self.fit, parse_eft("[Harbinger, X]\nHeat Sink II\n")
        )
        self.client.force_login(self.manager)
        self.client.post(
            reverse("fitcheck:manage_fit_items", args=[self.fit.pk]),
            self._formset_data(
                {self.low_item.pk: {"min_quantity_pct": "50"}}
            ),
        )
        from ..models import FitSubmission

        submission = FitSubmission.objects.get()
        self.assertTrue(submission.is_stale)


class TestPerFitRecheckButton(ManualRecheckCase):
    def _make_stale_submission(self):
        submission = submit_fit(
            self.member, self.fit, parse_eft("[Harbinger, X]\nHeat Sink II\n")
        )
        self.fit.bump_version()  # makes the submission stale
        return submission

    def test_manual_fit_recheck_button_enqueues_task(self):
        self._make_stale_submission()
        self.client.force_login(self.manager)
        with patch("fitcheck.tasks.recheck_pending_submissions.delay") as delay:
            response = self.client.post(
                reverse("fitcheck:fit_recheck_stale", args=[self.fit.pk]),
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        delay.assert_called_once_with(self.fit.pk)

    def test_fit_recheck_no_op_when_nothing_stale(self):
        self.client.force_login(self.manager)
        with patch("fitcheck.tasks.recheck_pending_submissions.delay") as delay:
            self.client.post(
                reverse("fitcheck:fit_recheck_stale", args=[self.fit.pk]),
                follow=True,
            )
        delay.assert_not_called()

    def test_recheck_within_cooldown_returns_warning_no_extra_enqueue(self):
        self._make_stale_submission()
        self.client.force_login(self.manager)
        url = reverse("fitcheck:fit_recheck_stale", args=[self.fit.pk])
        with patch("fitcheck.tasks.recheck_pending_submissions.delay") as delay:
            self.client.post(url, follow=True)
            self.client.post(url, follow=True)
        # Second call is in the cooldown window and must not queue another task.
        self.assertEqual(delay.call_count, 1)


class TestStaleRecheckPage(ManualRecheckCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_fit = create_fit(cls.doctrine, T.HARBINGER, name="Shield Brawl")
        add_item(cls.other_fit, Section.MED, T.CAP_RECHARGER_II, 1)

    def _bump_and_submit(self, fit):
        submit_fit(self.member, fit, parse_eft("[Harbinger, X]\nHeat Sink II\n"))
        fit.bump_version()

    def test_page_requires_manage_doctrines_permission(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("fitcheck:stale_recheck_page"))
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.manager)
        self.assertEqual(
            self.client.get(reverse("fitcheck:stale_recheck_page")).status_code, 200
        )

    def test_page_lists_only_fits_with_stale_pending(self):
        # self.fit has a stale pending, other_fit does not.
        self._bump_and_submit(self.fit)
        self.client.force_login(self.manager)
        response = self.client.get(reverse("fitcheck:stale_recheck_page"))
        self.assertContains(response, "Armor Brawl")
        self.assertNotContains(response, "Shield Brawl")

    def test_empty_state_when_nothing_stale(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("fitcheck:stale_recheck_page"))
        self.assertContains(response, "Nothing stale")

    def test_recheck_selected_fits_queues_per_selection(self):
        self._bump_and_submit(self.fit)
        self._bump_and_submit(self.other_fit)
        self.client.force_login(self.manager)
        with patch("fitcheck.tasks.recheck_pending_submissions.delay") as delay:
            self.client.post(
                reverse("fitcheck:stale_recheck_page"),
                {"action": "selected", "fits": [str(self.fit.pk)]},
                follow=True,
            )
        delay.assert_called_once_with(self.fit.pk)

    def test_recheck_all_stale_queues_task_per_affected_fit(self):
        self._bump_and_submit(self.fit)
        self._bump_and_submit(self.other_fit)
        self.client.force_login(self.manager)
        with patch("fitcheck.tasks.recheck_pending_submissions.delay") as delay:
            self.client.post(
                reverse("fitcheck:stale_recheck_page"),
                {"action": "all"},
                follow=True,
            )
        queued_pks = sorted(call.args[0] for call in delay.call_args_list)
        self.assertEqual(queued_pks, sorted([self.fit.pk, self.other_fit.pk]))

    def test_no_selection_shows_message_and_does_not_queue(self):
        self._bump_and_submit(self.fit)
        self.client.force_login(self.manager)
        with patch("fitcheck.tasks.recheck_pending_submissions.delay") as delay:
            self.client.post(
                reverse("fitcheck:stale_recheck_page"),
                {"action": "selected"},
                follow=True,
            )
        delay.assert_not_called()
