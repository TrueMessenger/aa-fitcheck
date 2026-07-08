from django.db import models
from django.db.models import Q


def _is_manager(user) -> bool:
    """Full-authority bypass: doctrine managers and superusers see and act on
    everything. NOT plain reviewers - a review permission grants scoped
    authority (see ``can_review_submission``) and scoped visibility (folded
    into ``visible_category_ids``), not a blanket see-everything bypass.
    A superuser satisfies ``has_perm`` for every permission, so this covers
    them without a separate check."""
    return user.has_perm("fitcheck.manage_doctrines")


def _has_review_perm(user) -> bool:
    """Whether the user holds a Fit Check review permission - the two review
    roles are treated identically. This is the single source of truth for
    "is a reviewer"; it grants *access* to the review queue but not authority
    over any given submission (see ``can_review_submission``)."""
    return user.has_perm("fitcheck.review_submissions") or user.has_perm(
        "fitcheck.secure_group_management"
    )


def category_admits(selected_ids: set, required_ids: set, user_group_ids: set) -> bool:
    """The Selected (OR) / Required (AND) admission rule for one category:
    no groups at all = public; else admitted by holding any selected group OR
    all required groups."""
    return (
        (not selected_ids and not required_ids)
        or bool(selected_ids & user_group_ids)
        or (bool(required_ids) and required_ids <= user_group_ids)
    )


def reviewable_category_ids(user) -> list[int]:
    """Ids of DoctrineCategory rows whose ``reviewer_groups`` the user can act
    on. Managers/superusers get every category. A plain reviewer gets the
    categories whose ``reviewer_groups`` intersect their Auth groups - i.e. the
    categories they have been *scoped* to review. Categories with EMPTY
    reviewer_groups are deliberately NOT listed here: an unscoped category
    narrows nobody's authority, so it never needs to widen a reviewer's scope
    or visibility (any reviewer may act on it via ``can_review_submission``'s
    empty-reviewer_groups branch, but it does not grant sight of otherwise
    visibility-gated content)."""
    from .models import DoctrineCategory

    if _is_manager(user):
        return list(DoctrineCategory.objects.values_list("pk", flat=True))
    group_ids = set(user.groups.values_list("id", flat=True))
    if not group_ids:
        return []
    return list(
        DoctrineCategory.objects.filter(reviewer_groups__id__in=group_ids)
        .values_list("pk", flat=True)
        .distinct()
    )


def visible_category_ids(user) -> list[int]:
    """Ids of DoctrineCategory rows `user` may see. Membership admits by the
    Selected (OR) / Required (AND) group rules (computed in Python - the
    category set is small and the "has ALL required groups" test is awkward to
    express in SQL). A user holding a review permission ALSO sees every
    category they are scoped to review, even one their groups don't otherwise
    admit - authority grants sight. Managers never reach here (their callers
    short-circuit on ``_is_manager``)."""
    from .models import DoctrineCategory

    group_ids = set(user.groups.values_list("id", flat=True))
    out: list[int] = []
    cats = DoctrineCategory.objects.prefetch_related("selected_groups", "required_groups")
    for cat in cats:
        sel = {g.id for g in cat.selected_groups.all()}
        req = {g.id for g in cat.required_groups.all()}
        if category_admits(sel, req, group_ids):
            out.append(cat.pk)
    if _has_review_perm(user):
        # Union in the categories this reviewer is scoped to review, without
        # duplicating ids already admitted by membership.
        out = list(dict.fromkeys(out + reviewable_category_ids(user)))
    return out


def visible_categories_for(user):
    """DoctrineCategory rows to offer on the Doctrines page filter chips: for
    a manager (routed through `_is_manager`, so a later narrowing there
    applies here too) every category; for everyone else, only categories
    that admit the user AND carry at least one doctrine the user can
    currently see - this hides both restricted categories and
    admitted-but-empty ones. Ordered by name, matching the chip bar's
    display order."""
    from .models import Doctrine, DoctrineCategory

    if _is_manager(user):
        return DoctrineCategory.objects.order_by("name")
    vis_ids = visible_category_ids(user)
    visible_doctrines = Doctrine.objects.visible_to(user).active()
    return (
        DoctrineCategory.objects.filter(pk__in=vis_ids, doctrines__in=visible_doctrines)
        .distinct()
        .order_by("name")
    )


def can_review_submission(user, submission) -> bool:
    """Whether `user` may decide `submission` under per-category review scoping.

    Managers/superusers may decide anything. Otherwise the user must hold a
    review permission AND the submission must fall in their review scope: a
    submission is in scope if its doctrine is None, its doctrine has no
    categories, at least one of its doctrine's categories has EMPTY
    reviewer_groups (unscoped - any reviewer may act), or the user is in the
    reviewer_groups of at least one of its doctrine's categories. With no
    reviewer_groups configured anywhere this is byte-for-byte the old
    "any reviewer may act on any submission" behaviour."""
    if _is_manager(user):
        return True
    if not _has_review_perm(user):
        return False
    doctrine = submission.doctrine
    if doctrine is None:
        return True
    categories = list(doctrine.categories.all())
    if not categories:
        return True
    group_ids = set(user.groups.values_list("id", flat=True))
    for category in categories:
        reviewer_group_ids = set(category.reviewer_groups.values_list("id", flat=True))
        if not reviewer_group_ids or (reviewer_group_ids & group_ids):
            return True
    return False


def visible_categories_among(user, categories):
    """Filter an already-fetched collection of DoctrineCategory rows (e.g. a
    doctrine's or fit's own `.categories.all()`) down to the ones that admit
    `user`, preserving input order. Managers see every category (routed
    through `_is_manager`, so a later narrowing there applies here too)."""
    if _is_manager(user):
        return list(categories)
    vis_ids = set(visible_category_ids(user))
    return [c for c in categories if c.pk in vis_ids]


class DoctrineQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)

    def visible_to(self, user):
        """Doctrines the user may see. A doctrine with no categories is public;
        a categorised one is visible if any of its categories admits the user.
        Managers/reviewers see everything."""
        if _is_manager(user):
            return self
        vis = visible_category_ids(user)
        return self.filter(Q(categories__isnull=True) | Q(categories__in=vis)).distinct()


class DoctrineFitQuerySet(models.QuerySet):
    def visible_to(self, user):
        """Fits the user may see. A fit's effective categories are its own plus
        those of every doctrine it belongs to. No effective categories = public;
        otherwise visible if any effective category admits the user."""
        if _is_manager(user):
            return self
        vis = visible_category_ids(user)
        # Fits that carry at least one category directly or via a doctrine.
        categorized_pks = set(
            self.filter(
                Q(categories__isnull=False) | Q(doctrines__categories__isnull=False)
            ).values_list("pk", flat=True)
        )
        admitted_pks = set(
            self.filter(
                Q(categories__in=vis) | Q(doctrines__categories__in=vis)
            ).values_list("pk", flat=True)
        )
        # Visible = admitted by a category OR uncategorised (public).
        return self.filter(Q(pk__in=admitted_pks) | ~Q(pk__in=categorized_pks)).distinct()


class FitSubmissionQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(status=self.model.Status.PENDING)

    def for_user(self, user):
        return self.filter(user=user)

    def reviewable_by(self, user):
        """Submissions `user` is scoped to review. Managers/superusers see the
        whole queue; a non-reviewer sees nothing. A plain reviewer sees a
        submission if its doctrine is None, its doctrine has no categories, its
        doctrine has a category with EMPTY reviewer_groups (unscoped), or a
        category whose reviewer_groups include one of the user's groups. This
        is the queryset form of ``can_review_submission``; with no
        reviewer_groups configured anywhere it returns the full queue (the old
        behaviour)."""
        if _is_manager(user):
            return self
        if not _has_review_perm(user):
            return self.none()
        group_ids = list(user.groups.values_list("id", flat=True))
        return self.filter(
            Q(doctrine__isnull=True)
            | Q(doctrine__categories__reviewer_groups__isnull=True)
            | Q(doctrine__categories__reviewer_groups__id__in=group_ids)
        ).distinct()

    def with_staleness(self):
        """Annotate each row's (doctrine, fit) assignment ladder so
        ``is_stale`` / ``live_assignment_version`` need no per-row query on
        list pages. NULL annotations mean "no assignment" (source-defaults
        submissions, or the assignment was deleted)."""
        FitAssignment = self.model._meta.apps.get_model("fitcheck", "FitAssignment")
        assignment = FitAssignment.objects.filter(
            doctrine_id=models.OuterRef("doctrine_id"),
            fit_id=models.OuterRef("doctrine_fit_id"),
        )
        return self.annotate(
            assignment_version=models.Subquery(assignment.values("version")[:1]),
            assignment_bumped_at=models.Subquery(
                assignment.values("version_bumped_at")[:1]
            ),
        )
