"""Pilot-side actions on their own submissions (requirement 1):

- Delete a pending submission they own; soft-hide a rejected one instead.
- Re-check a submission to re-grade against the current doctrine version.
- Spam-prevention: 30-second cooldown per (user, doctrine_fit) on re-check.
- Filter the pilot's own validation history (status, verdict, doctrine,
  character, ship).
"""

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from eveuniverse.models import EveType

from allianceauth.eveonline.models import EveCharacter

from ..constants import Section
from ..models import FitSubmission
from ..services.check_runner import submit_fit
from ..services.eft_parser import parse_eft
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


class PilotActionsCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Armor Brawl")
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        cls.member = create_user("member")
        cls.other_member = create_user("other")
        cls.reviewer = create_user(
            "reviewer", permissions=["basic_access", "review_submissions"]
        )

    def setUp(self):
        cache.clear()

    def _submit(self, user=None, source=None, doctrine=None):
        return submit_fit(
            user or self.member,
            self.fit,
            parse_eft("[Harbinger, X]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"),
            source=source or FitSubmission.Source.ESI,
            doctrine=doctrine,
        )


class TestSubmissionDelete(PilotActionsCase):
    def test_pilot_can_delete_own_pending_submission(self):
        submission = self._submit()
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("fitcheck:submission_delete", args=[submission.pk]),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            FitSubmission.objects.filter(pk=submission.pk).exists()
        )
        # Lands on the pilot fittings index after delete.
        self.assertContains(response, "Pilot Fittings")

    def test_pilot_cannot_delete_approved_submission(self):
        submission = self._submit()
        submission.status = FitSubmission.Status.APPROVED
        submission.save(update_fields=["status"])
        self.client.force_login(self.member)
        self.client.post(
            reverse("fitcheck:submission_delete", args=[submission.pk]),
            follow=True,
        )
        # Row survives.
        self.assertTrue(FitSubmission.objects.filter(pk=submission.pk).exists())

    def test_pilot_removing_rejected_submission_hides_it_instead_of_deleting(self):
        submission = self._submit()
        submission.status = FitSubmission.Status.REJECTED
        submission.save(update_fields=["status"])
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("fitcheck:submission_delete", args=[submission.pk]),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        submission.refresh_from_db()
        # Still in the database - only hidden from the owner's history view.
        self.assertIsNotNone(submission.hidden_at)

    def test_pilot_cannot_delete_other_users_submission(self):
        submission = self._submit()
        self.client.force_login(self.other_member)
        response = self.client.post(
            reverse("fitcheck:submission_delete", args=[submission.pk])
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(FitSubmission.objects.filter(pk=submission.pk).exists())

    def test_reviewer_cannot_delete_via_pilot_endpoint(self):
        """Reviewers see submissions but aren't owners; the delete endpoint is for
        owners only. Admins still have Django admin."""
        submission = self._submit()
        self.client.force_login(self.reviewer)
        response = self.client.post(
            reverse("fitcheck:submission_delete", args=[submission.pk])
        )
        self.assertEqual(response.status_code, 403)

    def test_delete_requires_post(self):
        submission = self._submit()
        self.client.force_login(self.member)
        # GET is rejected by @require_POST → 405.
        response = self.client.get(
            reverse("fitcheck:submission_delete", args=[submission.pk])
        )
        self.assertEqual(response.status_code, 405)


class TestSubmissionsDeleteBulk(PilotActionsCase):
    """Pilot can multi-select pending submissions on the Pilot Fittings page and
    delete them in one POST. Same gating: own + pending only."""

    def test_bulk_delete_removes_only_own_pending_submissions(self):
        own_pending_a = self._submit()
        own_pending_b = self._submit()
        own_approved = self._submit()
        own_approved.status = FitSubmission.Status.APPROVED
        own_approved.save(update_fields=["status"])
        other_pending = self._submit(user=self.other_member)

        self.client.force_login(self.member)
        response = self.client.post(
            reverse("fitcheck:submissions_delete_bulk"),
            {
                "submission_pks": [
                    str(own_pending_a.pk),
                    str(own_pending_b.pk),
                    str(own_approved.pk),
                    str(other_pending.pk),
                ]
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        surviving = set(FitSubmission.objects.values_list("pk", flat=True))
        # Only the two own-pending rows are gone.
        self.assertNotIn(own_pending_a.pk, surviving)
        self.assertNotIn(own_pending_b.pk, surviving)
        self.assertIn(own_approved.pk, surviving)
        self.assertIn(other_pending.pk, surviving)

    def test_bulk_delete_empty_selection_is_a_no_op(self):
        submission = self._submit()
        self.client.force_login(self.member)
        self.client.post(
            reverse("fitcheck:submissions_delete_bulk"), {}, follow=True
        )
        self.assertTrue(FitSubmission.objects.filter(pk=submission.pk).exists())

    def test_bulk_delete_returns_to_pilot_fittings_page(self):
        submission = self._submit()
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("fitcheck:submissions_delete_bulk"),
            {"submission_pks": [str(submission.pk)]},
        )
        self.assertRedirects(response, reverse("fitcheck:pilot_fittings"))

    def test_bulk_delete_requires_post(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("fitcheck:submissions_delete_bulk"))
        self.assertEqual(response.status_code, 405)

    def test_pilot_fittings_renders_checkboxes_for_pending_and_rejected_only(self):
        pending = self._submit()
        rejected = self._submit()
        rejected.status = FitSubmission.Status.REJECTED
        rejected.save(update_fields=["status"])
        approved = self._submit()
        approved.status = FitSubmission.Status.APPROVED
        approved.save(update_fields=["status"])

        self.client.force_login(self.member)
        response = self.client.get(reverse("fitcheck:pilot_fittings"))
        body = response.content.decode()
        # Pending and rejected submissions are removable (checkbox present)...
        self.assertIn(f'value="{pending.pk}"', body)
        self.assertIn(f'value="{rejected.pk}"', body)
        # ...approved rows are not - reviewer decisions to approve are immutable.
        self.assertNotIn(f'value="{approved.pk}"', body)


class TestSoftHideRejected(PilotActionsCase):
    """Removing a rejected submission from the pilot's own history hides it
    (hidden_at set) rather than deleting it - the reviewer's decision stays on
    the audit trail for reviewers/reports, but disappears from Pilot Fittings."""

    def test_bulk_remove_hides_own_rejected_row_but_keeps_it_in_the_database(self):
        rejected = self._submit()
        rejected.status = FitSubmission.Status.REJECTED
        rejected.save(update_fields=["status"])

        self.client.force_login(self.member)
        self.client.post(
            reverse("fitcheck:submissions_delete_bulk"),
            {"submission_pks": [str(rejected.pk)]},
            follow=True,
        )
        rejected.refresh_from_db()
        self.assertIsNotNone(rejected.hidden_at)
        # Still present for an unfiltered ("reviewer-style") queryset.
        self.assertTrue(FitSubmission.objects.filter(pk=rejected.pk).exists())

        # And absent from the pilot's own history page.
        response = self.client.get(reverse("fitcheck:pilot_fittings"))
        self.assertNotIn(rejected, response.context["submissions"])

    def test_bulk_remove_leaves_approved_rows_completely_untouched(self):
        approved = self._submit()
        approved.status = FitSubmission.Status.APPROVED
        approved.save(update_fields=["status"])

        self.client.force_login(self.member)
        self.client.post(
            reverse("fitcheck:submissions_delete_bulk"),
            {"submission_pks": [str(approved.pk)]},
            follow=True,
        )
        approved.refresh_from_db()
        self.assertIsNone(approved.hidden_at)
        self.assertEqual(approved.status, FitSubmission.Status.APPROVED)

    def test_bulk_remove_still_hard_deletes_pending_rows(self):
        pending = self._submit()
        self.client.force_login(self.member)
        self.client.post(
            reverse("fitcheck:submissions_delete_bulk"),
            {"submission_pks": [str(pending.pk)]},
            follow=True,
        )
        self.assertFalse(FitSubmission.objects.filter(pk=pending.pk).exists())

    def test_bulk_remove_does_not_touch_another_users_rejected_row(self):
        other_rejected = self._submit(user=self.other_member)
        other_rejected.status = FitSubmission.Status.REJECTED
        other_rejected.save(update_fields=["status"])

        self.client.force_login(self.member)
        self.client.post(
            reverse("fitcheck:submissions_delete_bulk"),
            {"submission_pks": [str(other_rejected.pk)]},
            follow=True,
        )
        other_rejected.refresh_from_db()
        self.assertIsNone(other_rejected.hidden_at)

    def test_bulk_remove_reports_combined_count_in_message(self):
        pending = self._submit()
        rejected = self._submit()
        rejected.status = FitSubmission.Status.REJECTED
        rejected.save(update_fields=["status"])

        self.client.force_login(self.member)
        response = self.client.post(
            reverse("fitcheck:submissions_delete_bulk"),
            {"submission_pks": [str(pending.pk), str(rejected.pk)]},
            follow=True,
        )
        self.assertContains(response, "Removed 2 submissions from your history.")

    def test_single_delete_hides_own_rejected_submission(self):
        rejected = self._submit()
        rejected.status = FitSubmission.Status.REJECTED
        rejected.save(update_fields=["status"])

        self.client.force_login(self.member)
        response = self.client.post(
            reverse("fitcheck:submission_delete", args=[rejected.pk]),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        rejected.refresh_from_db()
        self.assertIsNotNone(rejected.hidden_at)
        self.assertTrue(FitSubmission.objects.filter(pk=rejected.pk).exists())

    def test_single_delete_still_blocks_approved_submission(self):
        approved = self._submit()
        approved.status = FitSubmission.Status.APPROVED
        approved.save(update_fields=["status"])

        self.client.force_login(self.member)
        self.client.post(
            reverse("fitcheck:submission_delete", args=[approved.pk]),
            follow=True,
        )
        approved.refresh_from_db()
        self.assertIsNone(approved.hidden_at)
        self.assertEqual(approved.status, FitSubmission.Status.APPROVED)

    def test_single_delete_cannot_hide_another_users_rejected_row(self):
        other_rejected = self._submit(user=self.other_member)
        other_rejected.status = FitSubmission.Status.REJECTED
        other_rejected.save(update_fields=["status"])

        self.client.force_login(self.member)
        response = self.client.post(
            reverse("fitcheck:submission_delete", args=[other_rejected.pk])
        )
        self.assertEqual(response.status_code, 403)
        other_rejected.refresh_from_db()
        self.assertIsNone(other_rejected.hidden_at)

    def test_hidden_submission_never_appears_in_pilot_fittings_regardless_of_filters(self):
        rejected = self._submit()
        rejected.status = FitSubmission.Status.REJECTED
        rejected.hidden_at = timezone.now()
        rejected.save(update_fields=["status", "hidden_at"])

        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:pilot_fittings"), {"status": FitSubmission.Status.REJECTED}
        )
        self.assertNotIn(rejected, response.context["submissions"])


class TestFrigateEscapeBayPersistence(PilotActionsCase):
    """Persistence + view-side gating of the FEB readout."""

    def test_submit_fit_persists_feb_type_id(self):
        """The submit_fit service writes ParsedFit.frigate_escape_bay_type_id
        into the FitSubmission row."""
        from ..services.check_runner import submit_fit
        from ..services.fit_data import ParsedFit

        parsed = ParsedFit(
            ship_type_id=T.HARBINGER,
            fit_name="Brick Brawler",
            items=[],
            frigate_escape_bay_type_id=T.WEB_II,
        )
        submission = submit_fit(self.member, self.fit, parsed)
        submission.refresh_from_db()
        self.assertEqual(submission.frigate_escape_bay_type_id, T.WEB_II)

    def test_submission_detail_does_not_show_feb_for_non_battleship_hull(self):
        """The Harbinger is a Combat Battlecruiser - no FEB panel."""
        submission = self._submit()
        submission.frigate_escape_bay_type_id = T.WEB_II
        submission.save(update_fields=["frigate_escape_bay_type_id"])
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:submission_detail", args=[submission.pk])
        )
        self.assertNotContains(response, "Frigate Escape Bay")


class TestSubmissionRecheck(PilotActionsCase):
    def test_recheck_replaces_with_fresh_submission_at_current_version(self):
        original = self._submit()
        # Bump the doctrine version so the original is "stale" - the re-check
        # should write a new submission at the current version and drop the old.
        self.fit.bump_version()
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("fitcheck:submission_recheck", args=[original.pk]),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        # Only the newest submission is kept - the original is replaced.
        self.assertFalse(FitSubmission.objects.filter(pk=original.pk).exists())
        new = FitSubmission.objects.get(user=self.member)
        self.assertEqual(new.doctrine_fit, self.fit)
        self.assertEqual(new.fit_version, self.fit.version)

    def test_recheck_preserves_source_and_character(self):
        original = self._submit()
        original.source = FitSubmission.Source.ESI
        original.eft_text = "[Harbinger, X]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"
        original.save(update_fields=["source", "eft_text"])
        self.client.force_login(self.member)
        self.client.post(
            reverse("fitcheck:submission_recheck", args=[original.pk])
        )
        new = FitSubmission.objects.exclude(pk=original.pk).get(user=self.member)
        self.assertEqual(new.source, FitSubmission.Source.ESI)
        self.assertEqual(new.character_id, original.character_id)
        self.assertEqual(new.eft_text, original.eft_text)

    def test_recheck_within_cooldown_returns_warning_no_new_submission(self):
        original = self._submit()
        self.client.force_login(self.member)
        # First re-check: writes a new submission.
        self.client.post(
            reverse("fitcheck:submission_recheck", args=[original.pk])
        )
        new = FitSubmission.objects.exclude(pk=original.pk).get()
        # Second re-check in the cooldown window: should be blocked.
        before = FitSubmission.objects.count()
        self.client.post(
            reverse("fitcheck:submission_recheck", args=[new.pk])
        )
        self.assertEqual(FitSubmission.objects.count(), before)

    def test_recheck_after_cooldown_succeeds(self):
        original = self._submit()
        self.client.force_login(self.member)
        # First re-check.
        self.client.post(
            reverse("fitcheck:submission_recheck", args=[original.pk])
        )
        # Expire the cooldown by clearing the cache.
        cache.clear()
        new = FitSubmission.objects.get(user=self.member)
        self.client.post(
            reverse("fitcheck:submission_recheck", args=[new.pk])
        )
        # Each re-check replaces the previous, so only one submission remains.
        self.assertEqual(
            FitSubmission.objects.filter(user=self.member).count(), 1
        )
        self.assertFalse(FitSubmission.objects.filter(pk=new.pk).exists())

    def test_recheck_other_users_submission_is_forbidden(self):
        original = self._submit()
        self.client.force_login(self.other_member)
        response = self.client.post(
            reverse("fitcheck:submission_recheck", args=[original.pk])
        )
        self.assertEqual(response.status_code, 403)

    def test_recheck_works_on_approved_submission_for_owner(self):
        """The pilot can re-check even after a reviewer decided; the fresh result
        replaces the old one and starts PENDING for re-review."""
        original = self._submit()
        original.status = FitSubmission.Status.APPROVED
        original.save(update_fields=["status"])
        self.client.force_login(self.member)
        self.client.post(
            reverse("fitcheck:submission_recheck", args=[original.pk])
        )
        self.assertFalse(FitSubmission.objects.filter(pk=original.pk).exists())
        new = FitSubmission.objects.get(user=self.member)
        self.assertEqual(new.status, FitSubmission.Status.PENDING)

    def test_delete_does_not_consume_recheck_cooldown(self):
        """Delete is free - it shouldn't burn the (user, fit) cooldown."""
        original = self._submit()
        self.client.force_login(self.member)
        # Delete the pending submission.
        self.client.post(
            reverse("fitcheck:submission_delete", args=[original.pk])
        )
        # Submit a fresh one and try to re-check - must succeed because the
        # cooldown was never set. Recheck replaces, so the row count holds steady
        # but the submission is a different one.
        new = self._submit()
        before = FitSubmission.objects.count()
        self.client.post(
            reverse("fitcheck:submission_recheck", args=[new.pk])
        )
        self.assertEqual(FitSubmission.objects.count(), before)
        self.assertFalse(FitSubmission.objects.filter(pk=new.pk).exists())

    def test_recheck_blocked_for_legacy_eft_submission(self):
        """EFT-paste submissions predate the sandbox-only paste flow and can't
        be re-verified against anything - the row stays as read-only history."""
        original = self._submit(source=FitSubmission.Source.EFT)
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("fitcheck:submission_recheck", args=[original.pk]),
            follow=True,
        )
        self.assertRedirects(
            response, reverse("fitcheck:submission_detail", args=[original.pk])
        )
        self.assertContains(response, "can no longer be re-checked")
        self.assertTrue(FitSubmission.objects.filter(pk=original.pk).exists())
        self.assertEqual(FitSubmission.objects.count(), 1)


class TestPilotFittingsFilters(PilotActionsCase):
    """GET filters on the pilot's own validation history (status, verdict,
    doctrine, character, ship). Rows are built directly rather than through
    submit_fit so each one can carry exactly the field combination a filter
    test needs."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.second_doctrine = create_doctrine(name="Second Doctrine")
        cls.oracle_fit = create_fit(cls.second_doctrine, T.ORACLE, name="Sniper Oracle")
        cls.alt_character = EveCharacter.objects.create(
            character_id=95_000_001,
            character_name="Alt Toon",
            corporation_id=2001,
            corporation_name="Test Corp",
            corporation_ticker="TEST",
            security_status=0,
        )

        def make(**overrides):
            defaults = dict(
                user=cls.member,
                character=cls.member.profile.main_character,
                doctrine_fit=cls.fit,
                fit_version=cls.fit.version,
                doctrine=None,
                source=FitSubmission.Source.ESI,
                verdict=FitSubmission.Verdict.COMPLIANT,
                status=FitSubmission.Status.PENDING,
                ship_type=EveType.objects.get(id=T.HARBINGER),
            )
            defaults.update(overrides)
            return FitSubmission.objects.create(**defaults)

        # Source-defaults grading, own main character, pending, Harbinger.
        cls.pending_source_default = make()
        # Graded against a doctrine, approved, "compliant with substitutions".
        cls.approved_graded = make(
            doctrine=cls.doctrine,
            status=FitSubmission.Status.APPROVED,
            verdict=FitSubmission.Verdict.COMPLIANT_SUBS,
        )
        # Different doctrine + fit + hull, rejected, non-compliant.
        cls.rejected_other_doctrine = make(
            doctrine=cls.second_doctrine,
            doctrine_fit=cls.oracle_fit,
            fit_version=cls.oracle_fit.version,
            status=FitSubmission.Status.REJECTED,
            verdict=FitSubmission.Verdict.NON_COMPLIANT,
            ship_type=EveType.objects.get(id=T.ORACLE),
        )
        # Same as the source-default row, but flown on an alt character.
        cls.alt_character_submission = make(character=cls.alt_character)

    def _pks(self, response):
        return {s.pk for s in response.context["submissions"]}

    def test_status_filter_narrows_to_selected_status(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:pilot_fittings"),
            {"status": FitSubmission.Status.APPROVED},
        )
        self.assertEqual(self._pks(response), {self.approved_graded.pk})

    def test_verdict_filter_narrows_to_selected_verdict(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:pilot_fittings"),
            {"verdict": FitSubmission.Verdict.NON_COMPLIANT},
        )
        self.assertEqual(self._pks(response), {self.rejected_other_doctrine.pk})

    def test_doctrine_filter_by_pk_narrows_to_that_doctrine(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:pilot_fittings"), {"doctrine": self.second_doctrine.pk}
        )
        self.assertEqual(self._pks(response), {self.rejected_other_doctrine.pk})

    def test_doctrine_filter_source_defaults_sentinel_matches_null_doctrine(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:pilot_fittings"), {"doctrine": "none"}
        )
        self.assertEqual(
            self._pks(response),
            {self.pending_source_default.pk, self.alt_character_submission.pk},
        )

    def test_doctrine_filter_options_scoped_to_users_own_submissions(self):
        unrelated_doctrine = create_doctrine(name="Never Submitted Doctrine")
        self.client.force_login(self.member)
        response = self.client.get(reverse("fitcheck:pilot_fittings"))
        self.assertContains(response, self.doctrine.name)
        self.assertContains(response, self.second_doctrine.name)
        self.assertContains(response, "Source defaults")
        self.assertNotContains(response, unrelated_doctrine.name)

    def test_character_filter_narrows_to_selected_character(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:pilot_fittings"), {"character": self.alt_character.pk}
        )
        self.assertEqual(self._pks(response), {self.alt_character_submission.pk})

    def test_ship_filter_matches_partial_name_case_insensitively(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:pilot_fittings"), {"ship": "orac"}
        )
        self.assertEqual(self._pks(response), {self.rejected_other_doctrine.pk})

    def test_filters_compose_with_and_semantics(self):
        self.client.force_login(self.member)
        url = reverse("fitcheck:pilot_fittings")
        response = self.client.get(
            url,
            {"status": FitSubmission.Status.REJECTED, "doctrine": self.second_doctrine.pk},
        )
        self.assertEqual(self._pks(response), {self.rejected_other_doctrine.pk})

        # A contradictory combination yields no rows.
        response = self.client.get(
            url,
            {"status": FitSubmission.Status.APPROVED, "doctrine": self.second_doctrine.pk},
        )
        self.assertEqual(self._pks(response), set())

    def test_unknown_doctrine_and_character_values_are_ignored_not_errors(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:pilot_fittings"),
            {"doctrine": "not-a-number", "character": "not-a-number"},
        )
        self.assertEqual(response.status_code, 200)
        # No filtering applied for either field - every own, visible submission shows.
        self.assertEqual(
            self._pks(response),
            {
                self.pending_source_default.pk,
                self.approved_graded.pk,
                self.rejected_other_doctrine.pk,
                self.alt_character_submission.pk,
            },
        )

    def test_filter_querystring_preserved_in_pagination_context(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:pilot_fittings"),
            {"status": FitSubmission.Status.PENDING, "ship": "Harb"},
        )
        self.assertIn("ship=Harb", response.context["querystring"])
        self.assertIn("status=P", response.context["querystring"])
        self.assertNotIn("page=", response.context["querystring"])
