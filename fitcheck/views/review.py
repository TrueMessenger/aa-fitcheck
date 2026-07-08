from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

from ..forms import ReviewDecisionForm
from ..managers import _has_review_perm, can_review_submission
from ..models import Doctrine, FitSubmission
from ..services.check_runner import review_submission
from .common import paginate as _paginate


def review_access_required(view):
    """Full reviewers and the Secure Group Doctrine Management role may reach the
    review queue; neither implies doctrine/standards editing rights. Holding a
    review permission only grants queue *access* - which submissions a reviewer
    may see and decide is scoped per category (see ``can_review_submission``)."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not _has_review_perm(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return login_required(wrapper)


@review_access_required
def queue(request):
    submissions = FitSubmission.objects.reviewable_by(request.user).select_related(
        "user", "character", "doctrine_fit", "doctrine", "ship_type__eve_group"
    )

    status = request.GET.get("status", FitSubmission.Status.PENDING)
    if status:
        submissions = submissions.filter(status=status)
    verdict = request.GET.get("verdict", "")
    if verdict:
        submissions = submissions.filter(verdict=verdict)
    pilot = request.GET.get("pilot", "").strip()
    if pilot:
        submissions = submissions.filter(
            Q(character__character_name__icontains=pilot)
            | Q(user__username__icontains=pilot)
        )
    ship = request.GET.get("ship", "").strip()
    if ship:
        submissions = submissions.filter(ship_type__name__icontains=ship)
    group = request.GET.get("group", "").strip()
    if group:
        submissions = submissions.filter(ship_type__eve_group__name__icontains=group)
    doctrine = request.GET.get("doctrine", "")
    if doctrine.isdigit():
        submissions = submissions.filter(doctrine_id=doctrine)

    page_obj, elided_range, querystring = _paginate(
        request, submissions.order_by("-created_at")
    )
    return render(
        request,
        "fitcheck/review/queue.html",
        {
            "submissions": page_obj,
            "page_obj": page_obj,
            "elided_range": elided_range,
            "querystring": querystring,
            "status_filter": status,
            "verdict_filter": verdict,
            "pilot_filter": pilot,
            "ship_filter": ship,
            "group_filter": group,
            "doctrine_filter": doctrine,
            # The dropdown offers only doctrines this reviewer can see; after
            # the visibility narrowing that is exactly their review scope.
            "doctrines": Doctrine.objects.visible_to(request.user).order_by("name"),
            "statuses": FitSubmission.Status,
            "verdicts": FitSubmission.Verdict,
            "page_title": _("Submissions"),
        },
    )


@review_access_required
@require_POST
def submissions_delete_bulk(request):
    """Reviewers clean up the queue: delete the selected submissions regardless
    of owner or status."""
    pks = [pk for pk in request.POST.getlist("submission_pks") if pk.isdigit()]
    # Only submissions within this reviewer's scope are eligible - a pk outside
    # it is silently skipped rather than deleted. Count the in-scope targets
    # before deleting so the message reflects what was actually removed.
    in_scope_pks = list(
        FitSubmission.objects.reviewable_by(request.user)
        .filter(pk__in=pks)
        .values_list("pk", flat=True)
    )
    if in_scope_pks:
        FitSubmission.objects.filter(pk__in=in_scope_pks).delete()
        messages.success(
            request, _("Deleted %(n)s submission(s).") % {"n": len(in_scope_pks)}
        )
    else:
        messages.info(request, _("No submissions selected."))
    return redirect("fitcheck:review_queue")


@review_access_required
@require_POST
def submissions_approve_bulk(request):
    """Reviewers clear out the easy cases in one shot: approve every selected
    submission that is still pending and already came back Compliant (or
    Compliant with substitutions). Anything else - non-compliant, errored,
    already decided, or a stale pk - is skipped and left for individual
    review rather than silently rejected."""
    pks = [pk for pk in request.POST.getlist("submission_pks") if pk.isdigit()]
    targets = list(
        FitSubmission.objects.reviewable_by(request.user).filter(
            pk__in=pks,
            status=FitSubmission.Status.PENDING,
            verdict__in=(
                FitSubmission.Verdict.COMPLIANT,
                FitSubmission.Verdict.COMPLIANT_SUBS,
            ),
        )
    )
    from ..tasks import notify_member_decision

    for submission in targets:
        review_submission(submission, request.user, approve=True)
        notify_member_decision.delay(submission.pk)

    approved = len(targets)
    skipped = len(pks) - approved
    if approved:
        message = ngettext(
            "Approved %(n)s submission.",
            "Approved %(n)s submissions.",
            approved,
        ) % {"n": approved}
        if skipped:
            message += " " + ngettext(
                "Skipped %(n)s (not pending, or not a compliant verdict) - "
                "review it individually.",
                "Skipped %(n)s (not pending, or not a compliant verdict) - "
                "review those individually.",
                skipped,
            ) % {"n": skipped}
        messages.success(request, message)
    elif skipped:
        messages.info(
            request,
            _(
                "None of the selected submissions were pending with a "
                "compliant verdict - review them individually."
            ),
        )
    else:
        messages.info(request, _("No submissions selected."))
    return redirect("fitcheck:review_queue")


@review_access_required
def decide(request, submission_pk: int):
    submission = get_object_or_404(FitSubmission, pk=submission_pk)
    # Queue access is not authority over every submission: a reviewer scoped
    # out of this submission's category cannot decide it.
    if not can_review_submission(request.user, submission):
        raise PermissionDenied
    if request.method != "POST":
        return redirect("fitcheck:submission_detail", submission_pk=submission.pk)
    form = ReviewDecisionForm(request.POST)
    if form.is_valid():
        try:
            review_submission(
                submission,
                request.user,
                approve=form.cleaned_data["decision"] == "approve",
                comment=form.cleaned_data["comment"],
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            from ..tasks import notify_member_decision

            notify_member_decision.delay(submission.pk)
            messages.success(request, _("Decision recorded."))
            return redirect("fitcheck:review_queue")
    else:
        messages.error(request, _("Invalid decision."))
    return redirect("fitcheck:submission_detail", submission_pk=submission.pk)
