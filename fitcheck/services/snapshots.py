"""Compliance snapshot collection and lifecycle.

``take_snapshots`` writes one ``ComplianceSnapshot`` row per active doctrine per
day, aggregating the doctrine's **target audience** — holders of
``fitcheck.basic_access`` admitted by the doctrine's categories (an
uncategorised doctrine targets every basic-access holder; the manager/reviewer
visibility bypass deliberately does not widen an audience). Trends cannot be
backfilled, so this runs from a daily beat task.

``purge_snapshots`` is the operator lifecycle control: it backs the Diagnostics
page's purge buttons and the retention auto-prune, so an admin never needs
database access to manage the collected rows.

Everything here reads the local DB only — no ESI.
"""

from __future__ import annotations

import datetime as dt
import logging

from django.utils import timezone

from ..managers import category_admits
from ..models import ComplianceSnapshot, Doctrine, FitSubmission
from . import api

logger = logging.getLogger(__name__)


def basic_access_users():
    """Active users holding fitcheck.basic_access (directly, via group, or via
    state), with their group ids prefetched for the admission test. Shared by
    the snapshot task and the Reports drill-down."""
    from django.contrib.auth.models import Permission

    from .permissions import users_with_permission

    permission = Permission.objects.get(
        content_type__app_label="fitcheck", codename="basic_access"
    )
    return (
        users_with_permission(permission)
        .filter(is_active=True)
        .prefetch_related("groups")
    )


def audience_for(doctrine: Doctrine, users, group_ids_by_user: dict) -> list:
    """The subset of ``users`` the doctrine's categories admit. No categories =
    everyone (public). Shared by the snapshot task and the Reports drill-down."""
    categories = list(doctrine.categories.all())
    if not categories:
        return list(users)
    rules = [
        (
            {g.id for g in cat.selected_groups.all()},
            {g.id for g in cat.required_groups.all()},
        )
        for cat in categories
    ]
    return [
        user
        for user in users
        if any(
            category_admits(sel, req, group_ids_by_user[user.pk]) for sel, req in rules
        )
    ]


def take_snapshots(snapshot_date: dt.date | None = None) -> dict:
    """Write/refresh one snapshot row per active doctrine for ``snapshot_date``
    (default today). Re-running on the same day updates rows in place, so the
    ad-hoc "Run now" control never duplicates or errors. Returns a summary dict."""
    snapshot_date = snapshot_date or timezone.now().date()
    users = list(basic_access_users())
    group_ids_by_user = {u.pk: {g.id for g in u.groups.all()} for u in users}

    doctrines = Doctrine.objects.filter(is_active=True).prefetch_related(
        "categories__selected_groups", "categories__required_groups"
    )
    written = 0
    for doctrine in doctrines:
        audience = audience_for(doctrine, users, group_ids_by_user)
        audience_ids = {u.pk for u in audience}
        counts = {"compliant": 0, "compliant_subs": 0}
        for result in api.iter_user_compliance(audience, doctrine=doctrine):
            if result.verdict == FitSubmission.Verdict.COMPLIANT:
                counts["compliant"] += 1
            elif result.verdict == FitSubmission.Verdict.COMPLIANT_SUBS:
                counts["compliant_subs"] += 1
        # Split the rest into "submitted but failing" vs "never submitted".
        has_any = set(
            FitSubmission.objects.filter(
                doctrine=doctrine, user_id__in=audience_ids
            ).values_list("user_id", flat=True)
        )
        passing = counts["compliant"] + counts["compliant_subs"]
        ComplianceSnapshot.objects.update_or_create(
            doctrine=doctrine,
            date=snapshot_date,
            defaults={
                "audience_count": len(audience_ids),
                "compliant_count": counts["compliant"],
                "compliant_subs_count": counts["compliant_subs"],
                "non_compliant_count": max(len(has_any) - passing, 0),
                "no_submission_count": len(audience_ids - has_any),
            },
        )
        written += 1
    logger.info(
        "Compliance snapshots: wrote %d doctrine row(s) for %s (%d basic-access users)",
        written,
        snapshot_date,
        len(users),
    )
    return {"date": str(snapshot_date), "doctrines": written, "users": len(users)}


def purge_snapshots(older_than_days: int | None = None) -> int:
    """Delete snapshot rows and return the count. ``older_than_days=N`` keeps
    the most recent N days; ``None`` truncates the table."""
    qs = ComplianceSnapshot.objects.all()
    if older_than_days is not None:
        cutoff = timezone.now().date() - dt.timedelta(days=older_than_days)
        qs = qs.filter(date__lt=cutoff)
    deleted, _ = qs.delete()
    if deleted:
        logger.info(
            "Compliance snapshots: purged %d row(s) (%s)",
            deleted,
            "all" if older_than_days is None else f"older than {older_than_days}d",
        )
    return deleted
