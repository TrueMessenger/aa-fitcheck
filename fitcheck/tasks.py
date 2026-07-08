import logging

from celery import shared_task

from django.utils.translation import gettext, ngettext

from allianceauth.notifications import notify

from .models import DoctrineFit, FitSubmission, NotificationSettings, UserNotificationPreference


def _reviewers():
    from django.contrib.auth.models import Permission, User

    from .services.permissions import users_with_permission

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


def _diff_block(diff) -> str:
    """Plain-text "what changed" appendix for a stale notification. A missing
    or empty diff means the module list is unchanged - the staleness came from
    policy edits, which the archive-based diff cannot describe."""
    if diff is None or diff.is_empty:
        return "\n\n" + gettext(
            "The fit's policy rules changed (module list unchanged)."
        )
    lines = []
    for row in diff.added:
        lines.append(
            gettext("+ %(qty)sx %(name)s (%(section)s)")
            % {"qty": row.new_qty, "name": row.name, "section": row.section_label}
        )
    for row in diff.removed:
        lines.append(
            gettext("- %(qty)sx %(name)s (%(section)s)")
            % {"qty": row.old_qty, "name": row.name, "section": row.section_label}
        )
    for row in diff.changed:
        if row.old_qty is not None or row.new_qty is not None:
            lines.append(
                gettext("~ %(name)s: %(old)s -> %(new)s (%(section)s)")
                % {
                    "name": row.name,
                    "old": row.old_qty,
                    "new": row.new_qty,
                    "section": row.section_label,
                }
            )
        if row.old_charge or row.new_charge:
            lines.append(
                gettext("~ %(name)s: loaded charge %(old)s -> %(new)s")
                % {
                    "name": row.name,
                    "old": row.old_charge or "-",
                    "new": row.new_charge or "-",
                }
            )
    return "\n\n" + gettext("What changed:") + "\n" + "\n".join(lines)


def _notify_approved_stale(fit: DoctrineFit) -> None:
    """Warn holders of approved submissions that the fit moved on - once per
    (submission, fit version). The decision itself is never re-graded; the
    cache guard keeps repeated "Recheck Stale" runs from re-notifying until
    the fit actually changes again."""
    from django.core.cache import cache

    from .services.fit_diff import diff_for_submission

    approved = (
        fit.submissions.filter(status=FitSubmission.Status.APPROVED)
        .with_staleness()
        .select_related("user", "doctrine_fit")
        .order_by("-created_at")
    )
    seen_users: set[int] = set()
    for submission in approved:
        if not submission.is_stale or submission.user_id in seen_users:
            continue
        seen_users.add(submission.user_id)
        if UserNotificationPreference.is_muted(submission.user):
            continue
        # Keyed on every ladder the submission's currency depends on, so a
        # repeated Recheck Stale run stays silent but any NEW change (global
        # or this submission's own policy ladder) re-arms the notification.
        if submission.doctrine_id:
            policy_ladder = submission.live_assignment_version
        else:
            policy_ladder = fit.source_policy_version
        guard_key = (
            f"fitcheck-stale-approved-{submission.pk}-v{fit.version}-p{policy_ladder}"
        )
        if not cache.add(guard_key, True, timeout=None):
            continue
        if submission.fit_version != fit.version:
            body = gettext(
                "The fitting standard '%(fit)s' has changed since your submission "
                "#%(id)s was approved (v%(old)s -> v%(new)s). Your approval still "
                "stands, but re-verify your fit against the current version."
            ) % {
                "fit": fit,
                "id": submission.pk,
                "old": submission.fit_version,
                "new": fit.version,
            }
        else:
            body = gettext(
                "The policy rules for '%(fit)s' have changed since your submission "
                "#%(id)s was approved. Your approval still stands, but re-verify "
                "your fit against the current rules."
            ) % {"fit": fit, "id": submission.pk}
        body += _diff_block(diff_for_submission(submission))
        notify(
            submission.user,
            title=gettext("Fit Check: '%(fit)s' changed since your approval")
            % {"fit": fit.name},
            message=body,
            level="info",
        )


@shared_task(time_limit=3600)
def recheck_pending_submissions(fit_id: int):
    """After a doctrine fit re-import or policy change, re-grade pending
    submissions and tell the affected pilots what happened (with an old->new
    module diff when the BOM changed). Holders of approved submissions that
    predate the change are warned once per fit version - their decision is
    never re-graded. Every notification here (pending re-grade, plain
    verdict-changed fallback, approved-holder warning) is one logical event -
    "a fit/policy change touched your submission" - gated by the single
    ``notify_pilots_stale`` toggle."""
    from .services.check_runner import recheck_submission
    from .services.fit_diff import diff_for_submission

    fit = DoctrineFit.objects.filter(pk=fit_id).first()
    if fit is None:
        return
    notify_stale = NotificationSettings.current().notify_pilots_stale
    pending = (
        fit.submissions.pending().with_staleness().select_related("doctrine_fit", "user")
    )
    for submission in pending:
        old_verdict = submission.verdict
        was_stale = submission.is_stale
        # Resolve the diff before the re-check resets fit_version to current.
        diff = diff_for_submission(submission) if was_stale else None
        recheck_submission(submission)
        if UserNotificationPreference.is_muted(submission.user):
            continue
        if was_stale and notify_stale:
            body = gettext(
                "The fitting standard '%(fit)s' was updated and your pending "
                "submission #%(id)s was re-graded against the new version. "
                "Verdict: %(verdict)s."
            ) % {
                "fit": fit,
                "id": submission.pk,
                "verdict": submission.get_verdict_display(),
            }
            body += _diff_block(diff)
            notify(
                submission.user,
                title=gettext("Fit Check: '%(fit)s' changed - submission re-checked")
                % {"fit": fit.name},
                message=body,
                level="warning",
            )
        elif notify_stale and submission.verdict != old_verdict:
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
    if notify_stale:
        _notify_approved_stale(fit)


@shared_task(time_limit=300)
def notify_reviewers_new_submission(submission_id: int):
    settings = NotificationSettings.current()
    if not settings.notify_reviewers_new_submission or settings.reviewer_digest:
        return
    submission = (
        FitSubmission.objects.filter(pk=submission_id)
        .select_related("doctrine_fit", "doctrine", "user")
        .first()
    )
    if submission is None:
        return
    # Auto-approved (or otherwise already-decided) submissions never ping
    # reviewers - there is nothing left in the queue for them to act on. Kept in
    # the task, not the call sites, so every future caller inherits the guard.
    if submission.status != FitSubmission.Status.PENDING:
        return
    from .managers import can_review_submission

    for reviewer in _reviewers():
        if UserNotificationPreference.is_muted(reviewer):
            continue
        # Ping only reviewers scoped to this submission's category (an unscoped
        # category or no category means every reviewer, as before).
        if not can_review_submission(reviewer, submission):
            continue
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
    CELERYBEAT_SCHEDULE, and turn the "Reviewer digest" toggle on in Settings ->
    Notifications - this task is a no-op while it's off, even if scheduled."""
    if not NotificationSettings.current().reviewer_digest:
        return
    all_pending = FitSubmission.objects.pending().select_related(
        "doctrine_fit", "doctrine"
    )
    if not all_pending.exists():
        return
    # Each reviewer's digest counts only the pending submissions within their
    # own review scope; a reviewer with an empty scoped queue is skipped.
    for reviewer in _reviewers():
        if UserNotificationPreference.is_muted(reviewer):
            continue
        pending = all_pending.reviewable_by(reviewer)
        total = pending.count()
        if not total:
            continue
        by_fit: dict[str, int] = {}
        for submission in pending:
            key = str(submission.doctrine_fit)
            by_fit[key] = by_fit.get(key, 0) + 1
        breakdown = "\n".join(
            f"- {name}: {count}" for name, count in sorted(by_fit.items())
        )
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
    """Tell a pilot their submission was approved or rejected - covers both a
    human reviewer's decision and a doctrine's automatic "approved by rule"
    decision (one logical event, one `notify_member_decision` toggle)."""
    if not NotificationSettings.current().notify_member_decision:
        return
    submission = (
        FitSubmission.objects.filter(pk=submission_id)
        .select_related("doctrine_fit", "user", "reviewed_by")
        .first()
    )
    if submission is None:
        return
    if UserNotificationPreference.is_muted(submission.user):
        return
    approved = submission.status == FitSubmission.Status.APPROVED
    # Approved with no reviewer but a decision time = a rule auto-approval.
    auto_approved = (
        approved
        and submission.reviewed_by is None
        and submission.reviewed_at is not None
    )
    # A reviewer-less submission that is NOT a rule approval has no decision to
    # report (e.g. still pending) - stay silent, as before.
    if submission.reviewed_by is None and not auto_approved:
        return
    if auto_approved:
        title = gettext("Fit Check: submission approved by rule")
        body = gettext(
            "Your submission #%(id)s for '%(fit)s' met the doctrine's standard "
            "and was approved automatically - no reviewer was needed."
        ) % {
            "id": submission.pk,
            "fit": submission.doctrine_fit,
        }
    elif approved:
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
