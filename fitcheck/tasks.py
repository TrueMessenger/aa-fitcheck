import logging

from celery import shared_task

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
                title=f"Fit Check: verdict changed for {fit.name}",
                message=(
                    f"The doctrine fit '{fit}' was updated and your pending submission "
                    f"#{submission.pk} was re-checked. New verdict: "
                    f"{submission.get_verdict_display()}."
                ),
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
            title="Fit Check: new submission to review",
            message=(
                f"{submission.user} submitted a fit for '{submission.doctrine_fit}' "
                f"({submission.get_verdict_display()})."
            ),
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
            title=f"Fit Check: {total} submission{'s' if total != 1 else ''} awaiting review",
            message=f"Pending submissions by doctrine fit:\n{breakdown}",
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
    notify(
        submission.user,
        title=f"Fit Check: submission {'approved' if approved else 'rejected'}",
        message=(
            f"Your submission #{submission.pk} for '{submission.doctrine_fit}' was "
            f"{'approved' if approved else 'rejected'} by {submission.reviewed_by}."
            + (f"\n\nComment: {submission.review_comment}" if submission.review_comment else "")
        ),
        level="success" if approved else "danger",
    )
