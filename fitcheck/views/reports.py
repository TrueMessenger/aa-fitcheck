"""Reports tab: org-wide compliance overview, per-doctrine drill-down, and CSV
exports. Everything is gated by ``fitcheck.view_compliance_reports`` and
read-only (local DB, no ESI). The CSV endpoints share the HTML views' filter
parsing so the export can never drift from what the page shows."""

import csv

from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _t
from django.utils.translation import gettext_lazy as _

from ..app_settings import FITCHECK_REPORT_ANALYTICS_WINDOW_DAYS
from ..models import Doctrine, DoctrineCategory
from ..services.charts import build_trend_chart
from ..services.reports import (
    MemberState,
    doctrine_failure_analytics,
    doctrine_member_rows,
    doctrine_trend,
    overview_rows,
)
from .common import paginate

STATE_LABELS = {
    MemberState.COMPLIANT: _("Compliant"),
    MemberState.COMPLIANT_SUBS: _("Compliant (subs)"),
    MemberState.NON_COMPLIANT: _("Non-compliant"),
    MemberState.NO_SUBMISSION: _("No submission"),
}


def _parse_category(request) -> DoctrineCategory | None:
    raw = request.GET.get("category", "").strip()
    if not raw.isdigit():
        return None
    return DoctrineCategory.objects.filter(pk=int(raw)).first()


def _parse_member_filters(request) -> tuple[str, str]:
    state = request.GET.get("state", "").strip()
    if state not in MemberState.ALL:
        state = ""
    return state, request.GET.get("q", "").strip()


def _get_report_doctrine(doctrine_pk: int) -> Doctrine:
    # No is_active filter: a deactivated doctrine keeps its snapshot history.
    return get_object_or_404(
        Doctrine.objects.prefetch_related(
            "categories__selected_groups", "categories__required_groups"
        ),
        pk=doctrine_pk,
    )


def _filtered_member_rows(doctrine, state: str, query: str):
    rows = doctrine_member_rows(doctrine)
    counts = {s: sum(1 for r in rows if r.state == s) for s in MemberState.ALL}
    if state:
        rows = [r for r in rows if r.state == state]
    if query:
        needle = query.lower()
        rows = [
            r
            for r in rows
            if needle in r.character_name.lower() or needle in r.user.username.lower()
        ]
    return rows, counts


@login_required
@permission_required("fitcheck.basic_access")
@permission_required("fitcheck.view_compliance_reports")
def overview(request):
    category = _parse_category(request)
    rows = overview_rows(category)
    return render(
        request,
        "fitcheck/reports/overview.html",
        {
            "page_title": _("Compliance Reports"),
            "rows": rows,
            "has_snapshots": any(r.latest for r in rows),
            "categories": DoctrineCategory.objects.order_by("name"),
            "active_category": category,
        },
    )


@login_required
@permission_required("fitcheck.basic_access")
@permission_required("fitcheck.view_compliance_reports")
def drilldown(request, doctrine_pk: int):
    doctrine = _get_report_doctrine(doctrine_pk)
    state, query = _parse_member_filters(request)
    rows, counts = _filtered_member_rows(doctrine, state, query)
    page_obj, elided_range, querystring = paginate(request, rows)
    window = FITCHECK_REPORT_ANALYTICS_WINDOW_DAYS or None
    return render(
        request,
        "fitcheck/reports/drilldown.html",
        {
            "page_title": doctrine.name,
            "doctrine": doctrine,
            "page_obj": page_obj,
            "elided_range": elided_range,
            "querystring": querystring,
            "counts": counts,
            "total_members": sum(counts.values()),
            "active_state": state,
            "query": query,
            "state_labels": STATE_LABELS,
            "chart": build_trend_chart(doctrine_trend(doctrine)),
            "analytics": doctrine_failure_analytics(doctrine, window_days=window),
        },
    )


def _csv_response(filename: str) -> HttpResponse:
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
@permission_required("fitcheck.basic_access")
@permission_required("fitcheck.view_compliance_reports")
def overview_csv(request):
    category = _parse_category(request)
    stamp = timezone.now().strftime("%Y%m%d")
    response = _csv_response(f"fitcheck-compliance-overview-{stamp}.csv")
    writer = csv.writer(response)
    writer.writerow(
        [
            _t("Doctrine"),
            _t("Categories"),
            _t("Snapshot date"),
            _t("Audience"),
            _t("Compliant"),
            _t("Compliant (subs)"),
            _t("Non-compliant"),
            _t("No submission"),
            _t("Ready %"),
        ]
    )
    for row in overview_rows(category):
        latest = row.latest
        writer.writerow(
            [
                row.doctrine.name,
                ", ".join(c.name for c in row.doctrine.categories.all()),
                latest.date.isoformat() if latest else "",
                latest.audience_count if latest else "",
                latest.compliant_count if latest else "",
                latest.compliant_subs_count if latest else "",
                latest.non_compliant_count if latest else "",
                latest.no_submission_count if latest else "",
                row.ready_pct if latest else "",
            ]
        )
    return response


@login_required
@permission_required("fitcheck.basic_access")
@permission_required("fitcheck.view_compliance_reports")
def drilldown_csv(request, doctrine_pk: int):
    doctrine = _get_report_doctrine(doctrine_pk)
    state, query = _parse_member_filters(request)
    rows, _counts = _filtered_member_rows(doctrine, state, query)
    stamp = timezone.now().strftime("%Y%m%d")
    response = _csv_response(f"fitcheck-{slugify(doctrine.name)}-members-{stamp}.csv")
    writer = csv.writer(response)
    writer.writerow(
        [
            _t("Character"),
            _t("Username"),
            _t("State"),
            _t("Fit"),
            _t("Verdict"),
            _t("Last submission"),
        ]
    )
    for row in rows:
        sub = row.submission
        writer.writerow(
            [
                row.character_name,
                row.user.username,
                str(STATE_LABELS[row.state]),
                sub.doctrine_fit.name if sub else "",
                sub.get_verdict_display() if sub else "",
                sub.created_at.isoformat(timespec="seconds") if sub else "",
            ]
        )
    return response
