"""Per-category scoped review authority (``DoctrineCategory.reviewer_groups``).

A category's ``reviewer_groups`` scope who may review submissions for its
doctrines: a reviewer must hold a review permission AND be in one of the
groups (or the category must be unscoped). Group MEMBERSHIP (visibility) stays
distinct from REVIEW AUTHORITY. These tests pin, in order:

1. the unscoped-install invariants (no reviewer_groups anywhere = today's
   behaviour for the queue, decisions, notifications and digest);
2. scoped authority (queue / decide / bulk / detail / notify / digest);
3. the multi-category and doctrine-less openings;
4. the reviewer visibility narrowing (authority grants sight; a plain review
   permission no longer reveals everything).
"""

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from allianceauth.notifications.models import Notification

from ..constants import Section
from ..managers import can_review_submission, reviewable_category_ids
from ..models import (
    Doctrine,
    DoctrineCategory,
    DoctrineFit,
    FitSubmission,
    NotificationSettings,
)
from ..tasks import notify_reviewers_new_submission, send_review_digest
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


def _submission(
    user,
    fit,
    doctrine,
    *,
    verdict=FitSubmission.Verdict.COMPLIANT,
    status=FitSubmission.Status.PENDING,
) -> FitSubmission:
    return FitSubmission.objects.create(
        user=user,
        doctrine_fit=fit,
        doctrine=doctrine,
        fit_version=fit.version,
        source=FitSubmission.Source.EFT,
        verdict=verdict,
        status=status,
    )


def _decide(client, submission, decision="approve", comment="ok"):
    return client.post(
        reverse("fitcheck:review_decide", args=[submission.pk]),
        {"decision": decision, "comment": comment},
    )


class UnscopedInstallInvariantTests(TestCase):
    """With NO reviewer_groups configured anywhere, a plain reviewer's queue,
    decisions, notifications and digest are byte-for-byte the old behaviour, and
    they still see public content. (Visibility of group-gated content is
    intentionally narrowed - pinned separately in the narrowing tests below.)"""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        # Public doctrine (no categories) and a group-gated doctrine whose
        # category carries NO reviewer_groups (unscoped review authority).
        cls.public_doctrine = create_doctrine(name="Public Doctrine")
        cls.public_fit = create_fit(cls.public_doctrine, T.HARBINGER, name="Public Fit")
        add_item(cls.public_fit, Section.LOW, T.HEAT_SINK_II, 1)

        cls.gate_group = Group.objects.create(name="Gate")
        cls.gated_cat = DoctrineCategory.objects.create(name="Gated Cat")
        cls.gated_cat.selected_groups.add(cls.gate_group)  # visibility only
        cls.gated_doctrine = create_doctrine(name="Gated Doctrine")
        cls.gated_doctrine.categories.add(cls.gated_cat)
        cls.gated_fit = create_fit(cls.gated_doctrine, T.HARBINGER, name="Gated Fit")

        cls.member = create_user("member")
        cls.reviewer = create_user(
            "reviewer", permissions=["basic_access", "review_submissions"]
        )

        cls.sub_public = _submission(cls.member, cls.public_fit, cls.public_doctrine)
        cls.sub_gated = _submission(cls.member, cls.gated_fit, cls.gated_doctrine)

    def test_queue_shows_every_pending_submission(self):
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse("fitcheck:review_queue"))
        submissions = list(response.context["submissions"])
        self.assertIn(self.sub_public, submissions)
        self.assertIn(self.sub_gated, submissions)

    def test_reviewable_by_returns_the_whole_queue(self):
        reviewable = set(FitSubmission.objects.reviewable_by(self.reviewer))
        self.assertEqual(reviewable, {self.sub_public, self.sub_gated})

    def test_reviewer_can_decide_any_submission(self):
        # Even a submission for a doctrine the reviewer cannot SEE is decidable
        # while its category is unscoped - authority is not visibility.
        self.client.force_login(self.reviewer)
        response = _decide(self.client, self.sub_gated)
        self.assertEqual(response.status_code, 302)
        self.sub_gated.refresh_from_db()
        self.assertEqual(self.sub_gated.status, FitSubmission.Status.APPROVED)

    def test_reviewer_still_sees_public_content(self):
        self.assertIn(self.public_doctrine, Doctrine.objects.visible_to(self.reviewer))
        self.assertIn(self.public_fit, DoctrineFit.objects.visible_to(self.reviewer))

    def test_new_submission_notifies_the_reviewer(self):
        Notification.objects.all().delete()
        notify_reviewers_new_submission(self.sub_public.pk)
        self.assertTrue(Notification.objects.filter(user=self.reviewer).exists())

    def test_digest_counts_all_pending(self):
        settings_obj = NotificationSettings.current()
        settings_obj.reviewer_digest = True
        settings_obj.save()
        Notification.objects.all().delete()
        send_review_digest()
        note = Notification.objects.get(user=self.reviewer)
        self.assertIn("2 submissions awaiting review", note.title)


class ScopedReviewAuthorityTests(TestCase):
    """Category T scoped to group G: a reviewer in G may see and decide its
    submissions; a reviewer not in G is excluded from every review surface."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.g = Group.objects.create(name="Super Cap Admin")
        cls.cat = DoctrineCategory.objects.create(name="Titans")
        cls.cat.reviewer_groups.add(cls.g)  # authority only; publicly visible
        cls.doctrine = create_doctrine(name="Titan Doctrine")
        cls.doctrine.categories.add(cls.cat)
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Titan Fit")
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 1)

        cls.member = create_user("member")
        cls.reviewer_in = create_user(
            "rin", permissions=["basic_access", "review_submissions"]
        )
        cls.reviewer_in.groups.add(cls.g)
        cls.reviewer_out = create_user(
            "rout", permissions=["basic_access", "review_submissions"]
        )

    def setUp(self):
        self.sub = _submission(self.member, self.fit, self.doctrine)

    # --- queue ---------------------------------------------------------------
    def test_in_scope_queue_includes(self):
        self.client.force_login(self.reviewer_in)
        response = self.client.get(reverse("fitcheck:review_queue"))
        self.assertIn(self.sub, list(response.context["submissions"]))

    def test_out_of_scope_queue_excludes(self):
        self.client.force_login(self.reviewer_out)
        response = self.client.get(reverse("fitcheck:review_queue"))
        self.assertNotIn(self.sub, list(response.context["submissions"]))

    # --- decide --------------------------------------------------------------
    def test_in_scope_can_decide(self):
        self.client.force_login(self.reviewer_in)
        response = _decide(self.client, self.sub)
        self.assertEqual(response.status_code, 302)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, FitSubmission.Status.APPROVED)

    def test_out_of_scope_decide_403(self):
        self.client.force_login(self.reviewer_out)
        response = _decide(self.client, self.sub)
        self.assertEqual(response.status_code, 403)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, FitSubmission.Status.PENDING)

    # --- bulk delete ---------------------------------------------------------
    def test_in_scope_bulk_delete_removes(self):
        self.client.force_login(self.reviewer_in)
        self.client.post(
            reverse("fitcheck:review_submissions_delete_bulk"),
            {"submission_pks": [str(self.sub.pk)]},
        )
        self.assertFalse(FitSubmission.objects.filter(pk=self.sub.pk).exists())

    def test_out_of_scope_bulk_delete_skips(self):
        self.client.force_login(self.reviewer_out)
        self.client.post(
            reverse("fitcheck:review_submissions_delete_bulk"),
            {"submission_pks": [str(self.sub.pk)]},
        )
        self.assertTrue(FitSubmission.objects.filter(pk=self.sub.pk).exists())

    # --- bulk approve --------------------------------------------------------
    def test_in_scope_bulk_approve_approves(self):
        self.client.force_login(self.reviewer_in)
        self.client.post(
            reverse("fitcheck:review_submissions_approve_bulk"),
            {"submission_pks": [str(self.sub.pk)]},
        )
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, FitSubmission.Status.APPROVED)

    def test_out_of_scope_bulk_approve_skips(self):
        self.client.force_login(self.reviewer_out)
        self.client.post(
            reverse("fitcheck:review_submissions_approve_bulk"),
            {"submission_pks": [str(self.sub.pk)]},
        )
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, FitSubmission.Status.PENDING)

    # --- submission detail ---------------------------------------------------
    def test_in_scope_detail_200_with_review_panel(self):
        self.client.force_login(self.reviewer_in)
        response = self.client.get(
            reverse("fitcheck:submission_detail", args=[self.sub.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_review"])

    def test_out_of_scope_detail_403(self):
        self.client.force_login(self.reviewer_out)
        response = self.client.get(
            reverse("fitcheck:submission_detail", args=[self.sub.pk])
        )
        self.assertEqual(response.status_code, 403)

    # --- notifications -------------------------------------------------------
    def test_new_submission_notifies_only_in_scope_reviewer(self):
        Notification.objects.all().delete()
        notify_reviewers_new_submission(self.sub.pk)
        self.assertTrue(Notification.objects.filter(user=self.reviewer_in).exists())
        self.assertFalse(Notification.objects.filter(user=self.reviewer_out).exists())

    def test_digest_counts_only_in_scope(self):
        settings_obj = NotificationSettings.current()
        settings_obj.reviewer_digest = True
        settings_obj.save()
        Notification.objects.all().delete()
        send_review_digest()
        self.assertTrue(Notification.objects.filter(user=self.reviewer_in).exists())
        self.assertFalse(Notification.objects.filter(user=self.reviewer_out).exists())


class MultiCategoryScopingTests(TestCase):
    """A doctrine in two categories - one scoped to G, one unscoped - is opened
    to every reviewer by the unscoped category (OR across categories)."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.g = Group.objects.create(name="Scoped Group")
        cls.scoped_cat = DoctrineCategory.objects.create(name="Scoped Cat")
        cls.scoped_cat.reviewer_groups.add(cls.g)
        cls.open_cat = DoctrineCategory.objects.create(name="Open Cat")  # unscoped
        cls.doctrine = create_doctrine(name="Dual Doctrine")
        cls.doctrine.categories.add(cls.scoped_cat, cls.open_cat)
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Dual Fit")
        cls.member = create_user("member")
        cls.reviewer_out = create_user(
            "rout", permissions=["basic_access", "review_submissions"]
        )

    def setUp(self):
        self.sub = _submission(self.member, self.fit, self.doctrine)

    def test_unscoped_category_opens_to_all_reviewers(self):
        self.assertTrue(can_review_submission(self.reviewer_out, self.sub))
        self.assertIn(
            self.sub, FitSubmission.objects.reviewable_by(self.reviewer_out)
        )

    def test_out_of_scope_reviewer_can_decide_via_open_category(self):
        self.client.force_login(self.reviewer_out)
        response = _decide(self.client, self.sub)
        self.assertEqual(response.status_code, 302)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, FitSubmission.Status.APPROVED)


class DoctrinelessSubmissionScopingTests(TestCase):
    """A submission with no doctrine (source-default grading) is reviewable by
    every reviewer - there is no category to scope it."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.fit = create_fit(None, T.HARBINGER, name="Standalone Fit")
        cls.member = create_user("member")
        cls.reviewer = create_user(
            "reviewer", permissions=["basic_access", "review_submissions"]
        )

    def setUp(self):
        self.sub = _submission(self.member, self.fit, None)

    def test_no_doctrine_is_reviewable_by_all(self):
        self.assertTrue(can_review_submission(self.reviewer, self.sub))
        self.assertIn(self.sub, FitSubmission.objects.reviewable_by(self.reviewer))

    def test_reviewer_can_decide_doctrineless_submission(self):
        self.client.force_login(self.reviewer)
        response = _decide(self.client, self.sub)
        self.assertEqual(response.status_code, 302)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, FitSubmission.Status.APPROVED)


class ReviewerVisibilityNarrowingTests(TestCase):
    """Category C is visibility-restricted (a group the reviewer lacks) AND
    scoped to reviewer group G. A reviewer in G SEES its doctrines/fits/chips
    (authority grants sight); a plain reviewer not in G and not admitted does
    NOT; a doctrine manager sees everything."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.vis_group = Group.objects.create(name="Titan Pilots")  # visibility
        cls.rev_group = Group.objects.create(name="Titan Reviewers")  # authority
        cls.cat = DoctrineCategory.objects.create(name="Restricted Titans")
        cls.cat.selected_groups.add(cls.vis_group)
        cls.cat.reviewer_groups.add(cls.rev_group)
        cls.doctrine = create_doctrine(name="Restricted Titan Doctrine")
        cls.doctrine.categories.add(cls.cat)
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Restricted Titan Fit")

        cls.reviewer_in = create_user(
            "rin", permissions=["basic_access", "review_submissions"]
        )
        cls.reviewer_in.groups.add(cls.rev_group)  # NOT in vis_group
        cls.reviewer_out = create_user(
            "rout", permissions=["basic_access", "review_submissions"]
        )
        cls.manager = create_user(
            "mgr", permissions=["basic_access", "manage_doctrines"]
        )
        cls.plain_member = create_user("plain")

    def test_in_scope_reviewer_sees_doctrine_and_fit(self):
        self.assertIn(self.doctrine, Doctrine.objects.visible_to(self.reviewer_in))
        self.assertIn(self.fit, DoctrineFit.objects.visible_to(self.reviewer_in))

    def test_in_scope_reviewer_sees_category_chip_on_fit_detail(self):
        self.client.force_login(self.reviewer_in)
        response = self.client.get(reverse("fitcheck:fit_detail", args=[self.fit.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Restricted Titans")

    def test_out_of_scope_reviewer_does_not_see(self):
        self.assertNotIn(
            self.doctrine, Doctrine.objects.visible_to(self.reviewer_out)
        )
        self.assertNotIn(self.fit, DoctrineFit.objects.visible_to(self.reviewer_out))
        self.client.force_login(self.reviewer_out)
        response = self.client.get(reverse("fitcheck:fit_detail", args=[self.fit.pk]))
        self.assertEqual(response.status_code, 403)

    def test_plain_member_not_admitted_does_not_see(self):
        self.assertNotIn(
            self.doctrine, Doctrine.objects.visible_to(self.plain_member)
        )
        self.client.force_login(self.plain_member)
        response = self.client.get(reverse("fitcheck:fit_detail", args=[self.fit.pk]))
        self.assertEqual(response.status_code, 403)

    def test_manager_sees_everything(self):
        self.assertIn(self.doctrine, Doctrine.objects.visible_to(self.manager))
        self.assertIn(self.fit, DoctrineFit.objects.visible_to(self.manager))
        self.client.force_login(self.manager)
        response = self.client.get(reverse("fitcheck:fit_detail", args=[self.fit.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Restricted Titans")

    def test_reviewable_category_ids_scopes_by_group(self):
        self.assertIn(self.cat.pk, reviewable_category_ids(self.reviewer_in))
        self.assertNotIn(self.cat.pk, reviewable_category_ids(self.reviewer_out))
        self.assertIn(self.cat.pk, reviewable_category_ids(self.manager))


class PilotSelfViewScopingTests(TestCase):
    """A pilot always sees their own submission's detail, regardless of any
    category scoping they fall outside of."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.g = Group.objects.create(name="Reviewers Only")
        cls.cat = DoctrineCategory.objects.create(name="Scoped Cat")
        cls.cat.reviewer_groups.add(cls.g)
        cls.doctrine = create_doctrine(name="Scoped Doctrine")
        cls.doctrine.categories.add(cls.cat)
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Scoped Fit")
        cls.member = create_user("member")

    def test_owner_sees_own_submission_detail(self):
        sub = _submission(self.member, self.fit, self.doctrine)
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:submission_detail", args=[sub.pk])
        )
        self.assertEqual(response.status_code, 200)
        # The owner is not a reviewer, so no decision panel is offered.
        self.assertFalse(response.context["can_review"])
