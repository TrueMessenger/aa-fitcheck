from django.db import models
from django.db.models import Q


def _is_manager(user) -> bool:
    """Managers / reviewers / secure-group managers see everything."""
    return (
        user.has_perm("fitcheck.manage_doctrines")
        or user.has_perm("fitcheck.review_submissions")
        or user.has_perm("fitcheck.secure_group_management")
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


def visible_category_ids(user) -> list[int]:
    """Ids of DoctrineCategory rows that admit `user` by the Selected (OR) /
    Required (AND) group rules. Computed in Python - the category set is small
    and the "has ALL required groups" test is awkward to express in SQL."""
    from .models import DoctrineCategory

    group_ids = set(user.groups.values_list("id", flat=True))
    out: list[int] = []
    cats = DoctrineCategory.objects.prefetch_related("selected_groups", "required_groups")
    for cat in cats:
        sel = {g.id for g in cat.selected_groups.all()}
        req = {g.id for g in cat.required_groups.all()}
        if category_admits(sel, req, group_ids):
            out.append(cat.pk)
    return out


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
