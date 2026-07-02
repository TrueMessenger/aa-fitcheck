"""Read-only queries behind the Reports tab.

The overview reads ONLY ``ComplianceSnapshot`` rows (written by the daily
``take_compliance_snapshots`` task) — it never grades anything at request time.
The per-doctrine drill-down resolves ONE doctrine's audience live, which costs
what the snapshot task already pays per doctrine each day. Everything is local
DB; no ESI, no writes.

Every query here must stay portable across SQLite, MySQL/MariaDB, and
PostgreSQL: plain GROUP BY, ``Max``/``Count`` aggregates, no ``DISTINCT ON``,
no filtered aggregates.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.contrib.auth.models import User
from django.db.models import Count, Max
from django.utils import timezone

from ..models import (
    ComplianceFinding,
    ComplianceSnapshot,
    Doctrine,
    DoctrineCategory,
    FitSubmission,
)
from . import api
from .snapshots import audience_for, basic_access_users

OVERVIEW_TREND_DAYS = 14
ANALYTICS_TOP_N = 15

# Finding codes that name a module the pilot failed on.
FAILURE_CODES = ("MIS", "BAD", "QTY")


@dataclass(frozen=True)
class DoctrineOverviewRow:
    doctrine: Doctrine
    latest: ComplianceSnapshot | None
    ready_pct: float
    trend: list[float]  # ready_pct per snapshot day, oldest -> newest
    delta: float | None  # ready_pct change across the trend window


class MemberState:
    COMPLIANT = "compliant"
    COMPLIANT_SUBS = "subs"
    NON_COMPLIANT = "non_compliant"
    NO_SUBMISSION = "no_submission"

    ALL = (COMPLIANT, COMPLIANT_SUBS, NON_COMPLIANT, NO_SUBMISSION)


@dataclass(frozen=True)
class MemberReportRow:
    user: User
    character_name: str
    state: str  # a MemberState constant
    submission: FitSubmission | None  # qualifying sub, or the latest attempt


def _ready_pct(snapshot: ComplianceSnapshot) -> float:
    if not snapshot.audience_count:
        return 0.0
    passing = snapshot.compliant_count + snapshot.compliant_subs_count
    return round(passing * 100.0 / snapshot.audience_count, 1)


def overview_rows(category: DoctrineCategory | None = None) -> list[DoctrineOverviewRow]:
    """One row per active doctrine from snapshot data alone (2 queries)."""
    doctrines = (
        Doctrine.objects.filter(is_active=True)
        .prefetch_related("categories")
        .order_by("name")
    )
    if category is not None:
        doctrines = doctrines.filter(categories=category)
    doctrines = list(doctrines)

    since = timezone.now().date() - dt.timedelta(days=OVERVIEW_TREND_DAYS)
    history: dict[int, list[ComplianceSnapshot]] = {}
    for snap in ComplianceSnapshot.objects.filter(
        doctrine__in=doctrines, date__gte=since
    ).order_by("doctrine_id", "date"):
        history.setdefault(snap.doctrine_id, []).append(snap)

    rows: list[DoctrineOverviewRow] = []
    for doctrine in doctrines:
        snaps = history.get(doctrine.pk, [])
        trend = [_ready_pct(s) for s in snaps]
        latest = snaps[-1] if snaps else None
        rows.append(
            DoctrineOverviewRow(
                doctrine=doctrine,
                latest=latest,
                ready_pct=trend[-1] if trend else 0.0,
                trend=trend,
                delta=round(trend[-1] - trend[0], 1) if len(trend) >= 2 else None,
            )
        )
    return rows


def doctrine_trend(doctrine: Doctrine, days: int = 90):
    """Ordered snapshot history for one doctrine's trend chart."""
    since = timezone.now().date() - dt.timedelta(days=days)
    return list(
        ComplianceSnapshot.objects.filter(doctrine=doctrine, date__gte=since).order_by(
            "date"
        )
    )


def doctrine_member_rows(doctrine: Doctrine) -> list[MemberReportRow]:
    """Member-level readiness for one doctrine's target audience, sorted by
    character name. Caller should fetch ``doctrine`` with
    ``prefetch_related("categories__selected_groups", "categories__required_groups")``.
    Four queries regardless of audience size."""
    users = list(basic_access_users().select_related("profile__main_character"))
    group_ids_by_user = {u.pk: {g.id for g in u.groups.all()} for u in users}
    audience = audience_for(doctrine, users, group_ids_by_user)

    state_by_user: dict[int, tuple[str, FitSubmission | None]] = {}
    for result in api.iter_user_compliance(audience, doctrine=doctrine):
        if result.verdict == FitSubmission.Verdict.COMPLIANT:
            state_by_user[result.user_id] = (MemberState.COMPLIANT, result.submission)
        elif result.verdict == FitSubmission.Verdict.COMPLIANT_SUBS:
            state_by_user[result.user_id] = (
                MemberState.COMPLIANT_SUBS,
                result.submission,
            )

    rest_ids = [u.pk for u in audience if u.pk not in state_by_user]
    latest_pks = (
        FitSubmission.objects.filter(doctrine=doctrine, user_id__in=rest_ids)
        .values("user_id")
        .annotate(latest_pk=Max("pk"))
        .values_list("latest_pk", flat=True)
    )
    latest_attempts = {
        s.user_id: s
        for s in FitSubmission.objects.filter(pk__in=list(latest_pks)).select_related(
            "doctrine_fit"
        )
    }

    rows = []
    for user in audience:
        if user.pk in state_by_user:
            state, submission = state_by_user[user.pk]
        elif user.pk in latest_attempts:
            state, submission = MemberState.NON_COMPLIANT, latest_attempts[user.pk]
        else:
            state, submission = MemberState.NO_SUBMISSION, None
        main = getattr(user.profile, "main_character", None)
        rows.append(
            MemberReportRow(
                user=user,
                character_name=main.character_name if main else user.username,
                state=state,
                submission=submission,
            )
        )
    rows.sort(key=lambda r: r.character_name.lower())
    return rows


def latest_submission_ids(
    doctrine: Doctrine, *, window_days: int | None = None
) -> list[int]:
    """Ids of each pilot's newest non-rejected submission per (user, fit) graded
    under ``doctrine``. ``Max("pk")`` is a portable "newest" proxy because
    ``created_at`` is auto_now_add on an autoincrement pk. The window applies
    BEFORE picking the latest, so pilots inactive longer than the window drop
    out entirely — analytics reflect recent state. ``doctrine=doctrine``
    inherently excludes source-default (NULL-doctrine) submissions. Grouping per
    (user, fit) is deliberate: a pilot flying two hulls contributes one latest
    submission per hull."""
    qs = FitSubmission.objects.filter(doctrine=doctrine).exclude(
        status=FitSubmission.Status.REJECTED
    )
    if window_days:
        qs = qs.filter(created_at__gte=timezone.now() - dt.timedelta(days=window_days))
    return list(
        qs.values("user_id", "doctrine_fit_id")
        .annotate(latest_pk=Max("pk"))
        .values_list("latest_pk", flat=True)
    )


def doctrine_failure_analytics(
    doctrine: Doctrine, *, window_days: int | None = None
) -> dict:
    """Top failing modules and most-used substitutions for one doctrine, counted
    over each pilot's latest submission per fit (no resubmission over-weighting)."""
    ids = latest_submission_ids(doctrine, window_days=window_days)

    failures: dict[int, dict] = {}
    for row in (
        ComplianceFinding.objects.filter(
            submission_id__in=ids,
            code__in=FAILURE_CODES,
            expected_type__isnull=False,
        )
        .values("expected_type_id", "expected_type__name", "code")
        .annotate(
            occurrences=Count("id"),
            pilots=Count("submission__user_id", distinct=True),
        )
    ):
        entry = failures.setdefault(
            row["expected_type_id"],
            {
                "type_id": row["expected_type_id"],
                "name": row["expected_type__name"],
                "missing": 0,
                "not_allowed": 0,
                "qty_short": 0,
                "total": 0,
                "pilots": 0,
            },
        )
        key = {"MIS": "missing", "BAD": "not_allowed", "QTY": "qty_short"}[row["code"]]
        entry[key] += row["occurrences"]
        entry["total"] += row["occurrences"]
        entry["pilots"] = max(entry["pilots"], row["pilots"])

    top_failures = sorted(
        failures.values(), key=lambda e: (-e["total"], e["name"])
    )[:ANALYTICS_TOP_N]

    top_substitutions = list(
        ComplianceFinding.objects.filter(
            submission_id__in=ids,
            code="SUB",
            expected_type__isnull=False,
            actual_type__isnull=False,
        )
        .values(
            "expected_type_id",
            "expected_type__name",
            "actual_type_id",
            "actual_type__name",
        )
        .annotate(
            occurrences=Count("id"),
            pilots=Count("submission__user_id", distinct=True),
        )
        .order_by("-occurrences", "expected_type__name")[:ANALYTICS_TOP_N]
    )

    return {
        "top_failures": top_failures,
        "top_substitutions": top_substitutions,
        "submissions_considered": len(ids),
        "window_days": window_days,
    }
