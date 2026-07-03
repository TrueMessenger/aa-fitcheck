import logging

from celery import shared_task

from django.utils.translation import gettext, ngettext

from allianceauth.notifications import notify

from .app_settings import FITCHECK_NOTIFY_REVIEWERS, FITCHECK_REVIEWER_DIGEST
from .models import DoctrineFit, FitSubmission


def _reviewers():
    from django.contrib.auth.models import Permission, User

    from app_utils.django import users_with_permission

    users = User.objects.none()
    for codename in ("review_submissions", "secure_group_management"):
        permission = Permission.objects.get(
            content_type__app_label="fitcheck", codename=codename
        )
        users |= users_with_permission(permission)
    return users.distinct()

logger = logging.getLogger(__name__)

TASK_PRIORITY_LOW = 7


@shared_task(time_limit=3600)
def update_sde_data(force: bool = False):
    """Daily: reload the static data slice when CCP ships a new build."""
    from .services.sde_loader import load_sde

    load_sde(force=force)


@shared_task(time_limit=300)
def run_compliance_check(submission_id: int):
    from .services.check_runner import recheck_submission

    submission = FitSubmission.objects.filter(pk=submission_id).first()
    if submission:
        recheck_submission(submission)


@shared_task(time_limit=3600)
def recheck_pending_submissions(fit_id: int):
    """After a doctrine fit re-import or policy change, re-grade pending submissions."""
    from .services.check_runner import recheck_submission

    fit = DoctrineFit.objects.filter(pk=fit_id).first()
    if fit is None:
        return
    for submission in fit.submissions.pending().select_related("doctrine_fit", "user"):
        old_verdict = submission.verdict
        recheck_submission(submission)
        if submission.verdict != old_verdict:
            notify(
                submission.user,
                title=gettext("Fit Check: verdict changed for %(fit)s") % {"fit": fit.name},
                message=gettext(
                    "The doctrine fit '%(fit)s' was updated and your pending submission "
                    "#%(id)s was re-checked. New verdict: %(verdict)s."
                )
                % {
                    "fit": fit,
                    "id": submission.pk,
                    "verdict": submission.get_verdict_display(),
                },
                level="warning",
            )


@shared_task(time_limit=300)
def notify_reviewers_new_submission(submission_id: int):
    if not FITCHECK_NOTIFY_REVIEWERS or FITCHECK_REVIEWER_DIGEST:
        return
    submission = (
        FitSubmission.objects.filter(pk=submission_id)
        .select_related("doctrine_fit", "user")
        .first()
    )
    if submission is None:
        return
    for reviewer in _reviewers():
        notify(
            reviewer,
            title=gettext("Fit Check: new submission to review"),
            message=gettext("%(user)s submitted a fit for '%(fit)s' (%(verdict)s).")
            % {
                "user": submission.user,
                "fit": submission.doctrine_fit,
                "verdict": submission.get_verdict_display(),
            },
            level="info",
        )


@shared_task(time_limit=300)
def send_review_digest():
    """Periodic summary of the pending queue for reviewers. Schedule this via
    CELERYBEAT_SCHEDULE when FITCHECK_REVIEWER_DIGEST is enabled."""
    pending = FitSubmission.objects.pending().select_related("doctrine_fit")
    total = pending.count()
    if not total:
        return
    by_fit: dict[str, int] = {}
    for submission in pending:
        key = str(submission.doctrine_fit)
        by_fit[key] = by_fit.get(key, 0) + 1
    breakdown = "\n".join(f"- {name}: {count}" for name, count in sorted(by_fit.items()))
    for reviewer in _reviewers():
        notify(
            reviewer,
            title=ngettext(
                "Fit Check: %(total)d submission awaiting review",
                "Fit Check: %(total)d submissions awaiting review",
                total,
            )
            % {"total": total},
            message=gettext("Pending submissions by doctrine fit:\n%(breakdown)s")
            % {"breakdown": breakdown},
            level="info",
        )


@shared_task(time_limit=1800)
def refresh_structure_names(limit: int = None):
    """Resolve/refresh cached private-structure (Citadel) names via ESI, paced and
    bounded, so the member-inventory scan can read them locally without ever
    tripping the ESI error limit. Schedule this via CELERYBEAT_SCHEDULE (e.g.
    daily); FITCHECK_STRUCTURE_CACHE_TTL bounds how stale a name can get."""
    from .services.structure_cache import resolve_pending_and_stale

    return resolve_pending_and_stale(limit=limit)


@shared_task(time_limit=1800)
def take_compliance_snapshots():
    """Daily: record per-doctrine compliance aggregates so reports can chart
    trends (history cannot be backfilled). Schedule via CELERYBEAT_SCHEDULE;
    FITCHECK_SNAPSHOT_RETENTION_DAYS bounds how much history is kept. Safe to
    run ad hoc — a same-day re-run updates the day's rows in place."""
    from .app_settings import FITCHECK_SNAPSHOT_RETENTION_DAYS
    from .services.snapshots import purge_snapshots, take_snapshots

    result = take_snapshots()
    if FITCHECK_SNAPSHOT_RETENTION_DAYS > 0:
        result["pruned"] = purge_snapshots(
            older_than_days=FITCHECK_SNAPSHOT_RETENTION_DAYS
        )
    return result


@shared_task(time_limit=300)
def notify_member_decision(submission_id: int):
    submission = (
        FitSubmission.objects.filter(pk=submission_id)
        .select_related("doctrine_fit", "user", "reviewed_by")
        .first()
    )
    if submission is None or not submission.reviewed_by:
        return
    approved = submission.status == FitSubmission.Status.APPROVED
    if approved:
        title = gettext("Fit Check: submission approved")
        body = gettext(
            "Your submission #%(id)s for '%(fit)s' was approved by %(reviewer)s."
        ) % {
            "id": submission.pk,
            "fit": submission.doctrine_fit,
            "reviewer": submission.reviewed_by,
        }
    else:
        title = gettext("Fit Check: submission rejected")
        body = gettext(
            "Your submission #%(id)s for '%(fit)s' was rejected by %(reviewer)s."
        ) % {
            "id": submission.pk,
            "fit": submission.doctrine_fit,
            "reviewer": submission.reviewed_by,
        }
    if submission.review_comment:
        body += "\n\n" + gettext("Comment: %(comment)s") % {
            "comment": submission.review_comment
        }
    notify(
        submission.user,
        title=title,
        message=body,
        level="success" if approved else "danger",
    )
