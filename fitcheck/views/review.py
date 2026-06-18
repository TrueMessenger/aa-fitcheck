from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from ..forms import ReviewDecisionForm
from ..models import Doctrine, FitSubmission
from ..services.check_runner import review_submission


def review_access_required(view):
    """Full reviewers and the Secure Group Doctrine Management role may see and
    decide on submissions; neither implies doctrine/standards editing rights."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not (
            request.user.has_perm("fitcheck.review_submissions")
            or request.user.has_perm("fitcheck.secure_group_management")
        ):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return login_required(wrapper)


@review_access_required
def queue(request):
    submissions = FitSubmission.objects.select_related(
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

    return render(
        request,
        "fitcheck/review/queue.html",
        {
            "submissions": submissions.order_by("-created_at")[:300],
            "status_filter": status,
            "verdict_filter": verdict,
            "pilot_filter": pilot,
            "ship_filter": ship,
            "group_filter": group,
            "doctrine_filter": doctrine,
            "doctrines": Doctrine.objects.order_by("name"),
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
    deleted = 0
    if pks:
        deleted, _details = FitSubmission.objects.filter(pk__in=pks).delete()
    if deleted:
        messages.success(request, _("Deleted %(n)s submission(s).") % {"n": len(pks)})
    else:
        messages.info(request, _("No submissions selected."))
    return redirect("fitcheck:review_queue")


@review_access_required
def decide(request, submission_pk: int):
    submission = get_object_or_404(FitSubmission, pk=submission_pk)
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
