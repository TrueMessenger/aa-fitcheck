"""Public Python API for cross-plugin compliance queries.

This is the stable surface other Alliance Auth plugins (and aa-fitcheck's own
Secure Groups smart filter) use to ask *"is this user compliant with doctrine
X?"* without reaching into fitcheck internals. Per decision 22, the integration
surface is a documented Python API — not REST.

Everything here takes and returns plain Django objects plus the small
``ComplianceResult`` dataclass. Import from ``fitcheck.services.api``.

Compliance semantics
--------------------
A user is *compliant* with a target when they have a submission whose engine
verdict passes (``COMPLIANT`` or ``COMPLIANT_SUBS``). By default the check also
requires the submission to be **current** (graded against the live config -
the fit's global version plus the policy ladder it was actually graded from,
see ``FitSubmission.is_stale``) and **not reviewer-rejected**. Callers that
want a human-approved submission only can pass ``require_approved=True``.

A positive **staleness grace period** (``EnforcementSettings.stale_grace_days``,
set on the in-app Enforcement Settings page) keeps a stale submission counting
as current while every config change that staled it happened within the
window - pilots keep Secure Groups access for that long after a fit change
while they re-verify. The stale badge and notifications are always immediate.

Target selection
----------------
- ``fit=<DoctrineFit>``  — compliant with that specific fitting standard.
- ``doctrine=<Doctrine>`` — compliant with the doctrine, i.e. a passing
  submission for **any one** fit graded under that doctrine ("holds a compliant
  Hel *or* Wyvern"). Submissions graded against the fit's source defaults
  (``doctrine=None``) do not count toward a doctrine target.
- Both may be combined to require a specific fit under a specific doctrine.

At least one of ``doctrine`` / ``fit`` must be given.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth.models import User
from django.db.models import F, Q
from django.utils import timezone

from ..models import Doctrine, DoctrineFit, EnforcementSettings, FitSubmission

# Engine verdicts that count as passing.
PASSING_VERDICTS = (
    FitSubmission.Verdict.COMPLIANT,
    FitSubmission.Verdict.COMPLIANT_SUBS,
)


@dataclass(frozen=True)
class ComplianceResult:
    """The outcome of a compliance query for one user against one target."""

    user_id: int
    is_compliant: bool
    submission: FitSubmission | None

    @property
    def verdict(self) -> str | None:
        """The qualifying submission's verdict code, or ``None`` when not compliant."""
        return self.submission.verdict if self.submission else None


def _require_target(doctrine: Doctrine | None, fit: DoctrineFit | None) -> None:
    if doctrine is None and fit is None:
        raise ValueError("Pass a doctrine and/or a fit to check compliance against.")


def _qualifying_qs(
    *,
    doctrine: Doctrine | None,
    fit: DoctrineFit | None,
    require_approved: bool,
    require_current: bool,
):
    """Submissions that prove compliance with the target, newest first within
    each user. Caller scopes to a user / set of users."""
    qs = FitSubmission.objects.filter(verdict__in=PASSING_VERDICTS)
    if doctrine is not None:
        qs = qs.filter(doctrine=doctrine)
    if fit is not None:
        qs = qs.filter(doctrine_fit=fit)
    if require_approved:
        qs = qs.filter(status=FitSubmission.Status.APPROVED)
    else:
        # A reviewer's explicit rejection overrides the engine verdict.
        qs = qs.exclude(status=FitSubmission.Status.REJECTED)
    if require_current:
        qs = _filter_current(qs)
    return qs.select_related("doctrine_fit", "doctrine")


def _filter_current(qs):
    """Keep submissions graded against the live config - the SQL mirror of
    ``FitSubmission.is_stale``: the fit's global ladder plus whichever policy
    ladder (source or assignment) the submission was graded from.

    With a positive ``EnforcementSettings.stale_grace_days`` a stale submission
    still counts while every ladder that diverged last moved within the
    window; a divergent ladder older than the window (or of unknown age -
    NULL timestamps predate the ladder fields, and a deleted assignment has
    no ladder at all) expires it."""
    qs = qs.with_staleness()
    global_ok = Q(fit_version=F("doctrine_fit__version"))
    source_ok = Q(doctrine__isnull=True) & Q(
        policy_version=F("doctrine_fit__source_policy_version")
    )
    # NULL assignment_version (deleted assignment) fails this comparison in
    # SQL, which is exactly right: the grading basis is gone.
    assignment_ok = Q(doctrine__isnull=False) & Q(policy_version=F("assignment_version"))
    grace_days = EnforcementSettings.current().stale_grace_days
    if not grace_days:
        return qs.filter(global_ok & (source_ok | assignment_ok))
    cutoff = timezone.now() - timedelta(days=grace_days)
    global_expired = ~global_ok & (
        Q(doctrine_fit__version_bumped_at__lte=cutoff)
        | Q(doctrine_fit__version_bumped_at__isnull=True)
    )
    source_expired = (
        Q(doctrine__isnull=True)
        & ~Q(policy_version=F("doctrine_fit__source_policy_version"))
        & (
            Q(doctrine_fit__source_policy_bumped_at__lte=cutoff)
            | Q(doctrine_fit__source_policy_bumped_at__isnull=True)
        )
    )
    # NULL-safe divergence: NOT(x = NULL) is NULL in SQL and would silently
    # drop deleted-assignment rows from the expiry arm, granting them
    # indefinite grace - spell the isnull case out.
    assignment_divergent = Q(assignment_version__isnull=True) | (
        Q(assignment_version__isnull=False) & ~Q(policy_version=F("assignment_version"))
    )
    assignment_expired = (
        Q(doctrine__isnull=False)
        & assignment_divergent
        & (Q(assignment_bumped_at__lte=cutoff) | Q(assignment_bumped_at__isnull=True))
    )
    return qs.exclude(global_expired | source_expired | assignment_expired)


def get_qualifying_submission(
    user: User,
    *,
    doctrine: Doctrine | None = None,
    fit: DoctrineFit | None = None,
    require_approved: bool = False,
    require_current: bool = True,
) -> FitSubmission | None:
    """Return the newest submission proving ``user`` is compliant with the
    target, or ``None``. See the module docstring for target/semantics."""
    _require_target(doctrine, fit)
    return (
        _qualifying_qs(
            doctrine=doctrine,
            fit=fit,
            require_approved=require_approved,
            require_current=require_current,
        )
        .filter(user=user)
        .order_by("-created_at")
        .first()
    )


def is_user_compliant(
    user: User,
    *,
    doctrine: Doctrine | None = None,
    fit: DoctrineFit | None = None,
    require_approved: bool = False,
    require_current: bool = True,
) -> bool:
    """``True`` when ``user`` has a qualifying submission for the target."""
    return (
        get_qualifying_submission(
            user,
            doctrine=doctrine,
            fit=fit,
            require_approved=require_approved,
            require_current=require_current,
        )
        is not None
    )


def get_user_compliance(
    user: User,
    *,
    doctrine: Doctrine | None = None,
    fit: DoctrineFit | None = None,
    require_approved: bool = False,
    require_current: bool = True,
) -> ComplianceResult:
    """Compliance of one ``user`` against the target as a ``ComplianceResult``."""
    submission = get_qualifying_submission(
        user,
        doctrine=doctrine,
        fit=fit,
        require_approved=require_approved,
        require_current=require_current,
    )
    return ComplianceResult(
        user_id=user.pk,
        is_compliant=submission is not None,
        submission=submission,
    )


def iter_user_compliance(
    users: Iterable[User],
    *,
    doctrine: Doctrine | None = None,
    fit: DoctrineFit | None = None,
    require_approved: bool = False,
    require_current: bool = True,
) -> Iterator[ComplianceResult]:
    """Yield a ``ComplianceResult`` for each user in ``users`` (compliant or not),
    resolving everyone in a single submissions query. Built for the Secure Groups
    ``audit_filter`` bulk path. Order follows ``users``; unknown users come back
    ``is_compliant=False``."""
    _require_target(doctrine, fit)
    users = list(users)
    user_ids = [u.pk for u in users]
    # One query; keep the newest qualifying submission per user.
    best: dict[int, FitSubmission] = {}
    for submission in (
        _qualifying_qs(
            doctrine=doctrine,
            fit=fit,
            require_approved=require_approved,
            require_current=require_current,
        )
        .filter(user_id__in=user_ids)
        .order_by("user_id", "-created_at")
    ):
        best.setdefault(submission.user_id, submission)
    for user in users:
        submission = best.get(user.pk)
        yield ComplianceResult(
            user_id=user.pk,
            is_compliant=submission is not None,
            submission=submission,
        )
