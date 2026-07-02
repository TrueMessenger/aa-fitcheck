"""Reports tab: overview/drill-down services, analytics dedup, chart geometry,
views, permission gating, and CSV exports."""

import csv
import datetime as dt
import io

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..constants import Section
from ..models import ComplianceSnapshot, DoctrineCategory, FitSubmission
from ..services import reports
from ..services.assignments import attach_fit_to_doctrine
from ..services.charts import build_trend_chart, sparkline_points
from ..services.check_runner import review_submission, submit_fit
from ..services.eft_parser import parse_eft
from ..services.reports import MemberState
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata

COMPLIANT_EFT = "[Harbinger, Mine]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"
# Imperial Navy Heat Sink is an allowed faction substitute under VARIANTS.
SUBS_EFT = (
    "[Harbinger, Mine]\nImperial Navy Heat Sink\nHeat Sink II\nHeat Sink II\n"
)
SHORT_EFT = "[Harbinger, Mine]\nHeat Sink II\n"


def _snap(doctrine, days_ago=0, audience=10, compliant=5, subs=1, non=2, none=2):
    return ComplianceSnapshot.objects.create(
        doctrine=doctrine,
        date=timezone.now().date() - dt.timedelta(days=days_ago),
        audience_count=audience,
        compliant_count=compliant,
        compliant_subs_count=subs,
        non_compliant_count=non,
        no_submission_count=none,
    )


class ReportsTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(None, T.HARBINGER, name="Armor Brawl")
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        attach_fit_to_doctrine(cls.fit, cls.doctrine)
        cls.compliant = create_user("rep-compliant")
        cls.subs = create_user("rep-subs")
        cls.failing = create_user("rep-failing")
        cls.silent = create_user("rep-silent")
        cls.viewer = create_user(
            "rep-viewer", permissions=("basic_access", "view_compliance_reports")
        )

    def _submit(self, user, eft, *, doctrine=True):
        return submit_fit(
            user,
            self.fit,
            parse_eft(eft),
            eft_text=eft,
            doctrine=self.doctrine if doctrine else None,
        )

    def _submit_all_states(self):
        self._submit(self.compliant, COMPLIANT_EFT)
        self._submit(self.subs, SUBS_EFT)
        self._submit(self.failing, SHORT_EFT)


class OverviewRowsTests(ReportsTestCase):
    def test_latest_snapshot_wins_and_inactive_doctrine_absent(self):
        _snap(self.doctrine, days_ago=1, compliant=3)
        _snap(self.doctrine, days_ago=0, compliant=6)
        inactive = create_doctrine(name="Old Doctrine", is_active=False)
        _snap(inactive)
        rows = reports.overview_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].latest.compliant_count, 6)

    def test_category_filter(self):
        other = create_doctrine(name="Other")
        category = DoctrineCategory.objects.create(name="Caps")
        self.doctrine.categories.add(category)
        rows = reports.overview_rows(category)
        self.assertEqual([r.doctrine.pk for r in rows], [self.doctrine.pk])
        self.assertEqual(len(reports.overview_rows()), 2)
        self.assertTrue(other.pk)  # silence unused warning

    def test_zero_audience_guard(self):
        _snap(self.doctrine, audience=0, compliant=0, subs=0, non=0, none=0)
        rows = reports.overview_rows()
        self.assertEqual(rows[0].ready_pct, 0.0)

    def test_trend_and_delta(self):
        _snap(self.doctrine, days_ago=2, compliant=2, subs=0)  # 20%
        _snap(self.doctrine, days_ago=1, compliant=4, subs=0)  # 40%
        _snap(self.doctrine, days_ago=0, compliant=5, subs=1)  # 60%
        row = reports.overview_rows()[0]
        self.assertEqual(row.trend, [20.0, 40.0, 60.0])
        self.assertEqual(row.delta, 40.0)
        self.assertEqual(row.ready_pct, 60.0)

    def test_no_snapshots_row(self):
        row = reports.overview_rows()[0]
        self.assertIsNone(row.latest)
        self.assertEqual(row.trend, [])
        self.assertIsNone(row.delta)


class MemberRowsTests(ReportsTestCase):
    def test_states_split(self):
        self._submit_all_states()
        rows = reports.doctrine_member_rows(self.doctrine)
        by_user = {r.user.username: r for r in rows}
        self.assertEqual(by_user["rep-compliant"].state, MemberState.COMPLIANT)
        self.assertEqual(by_user["rep-subs"].state, MemberState.COMPLIANT_SUBS)
        self.assertEqual(by_user["rep-failing"].state, MemberState.NON_COMPLIANT)
        self.assertEqual(by_user["rep-silent"].state, MemberState.NO_SUBMISSION)
        self.assertIsNotNone(by_user["rep-failing"].submission)
        self.assertIsNone(by_user["rep-silent"].submission)

    def test_category_restricts_audience(self):
        group = Group.objects.create(name="Rep Team")
        self.compliant.groups.add(group)
        category = DoctrineCategory.objects.create(name="Rep Cat")
        category.selected_groups.add(group)
        self.doctrine.categories.add(category)
        doctrine = reports.Doctrine.objects.prefetch_related(
            "categories__selected_groups", "categories__required_groups"
        ).get(pk=self.doctrine.pk)
        rows = reports.doctrine_member_rows(doctrine)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].user, self.compliant)

    def test_stale_submission_is_non_compliant(self):
        self._submit(self.compliant, COMPLIANT_EFT)
        self.fit.bump_version()
        rows = reports.doctrine_member_rows(self.doctrine)
        by_user = {r.user.username: r for r in rows}
        self.assertEqual(by_user["rep-compliant"].state, MemberState.NON_COMPLIANT)
        # The stale submission still shows as the latest attempt.
        self.assertIsNotNone(by_user["rep-compliant"].submission)


class LatestSubmissionIdsTests(ReportsTestCase):
    def test_dedupes_resubmitters_to_newest(self):
        self._submit(self.failing, SHORT_EFT)
        self._submit(self.failing, SHORT_EFT)
        newest = self._submit(self.failing, SHORT_EFT)
        ids = reports.latest_submission_ids(self.doctrine)
        self.assertEqual(ids, [newest.pk])

    def test_per_fit_rejected_null_doctrine_and_window(self):
        second_fit = create_fit(None, T.HARBINGER, name="Second Fit")
        add_item(second_fit, Section.LOW, T.HEAT_SINK_II, 3)
        attach_fit_to_doctrine(second_fit, self.doctrine)
        first = self._submit(self.failing, SHORT_EFT)
        second = submit_fit(
            self.failing,
            second_fit,
            parse_eft(SHORT_EFT),
            eft_text=SHORT_EFT,
            doctrine=self.doctrine,
        )
        # One id per (user, fit).
        self.assertCountEqual(
            reports.latest_submission_ids(self.doctrine), [first.pk, second.pk]
        )
        # Rejected drops out.
        reviewer = create_user(
            "rep-reviewer", permissions=("basic_access", "review_submissions")
        )
        review_submission(second, reviewer, approve=False, comment="refit")
        self.assertEqual(reports.latest_submission_ids(self.doctrine), [first.pk])
        # Source-default (NULL doctrine) submissions never count.
        self._submit(self.silent, SHORT_EFT, doctrine=False)
        self.assertEqual(reports.latest_submission_ids(self.doctrine), [first.pk])
        # Window: backdate the survivor beyond 30 days -> its pilot drops out.
        FitSubmission.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - dt.timedelta(days=40)
        )
        self.assertEqual(
            reports.latest_submission_ids(self.doctrine, window_days=30), []
        )


class AnalyticsTests(ReportsTestCase):
    def test_top_failures_use_latest_submission_only(self):
        # Two failing submissions from one pilot: only the newest counts.
        self._submit(self.failing, SHORT_EFT)
        self._submit(self.failing, SHORT_EFT)
        self._submit(self.subs, SHORT_EFT)
        analytics = reports.doctrine_failure_analytics(self.doctrine)
        self.assertEqual(analytics["submissions_considered"], 2)
        self.assertEqual(len(analytics["top_failures"]), 1)
        top = analytics["top_failures"][0]
        self.assertEqual(top["name"], "Heat Sink II")
        self.assertEqual(top["pilots"], 2)
        # SHORT_EFT fits 1 of 3 Heat Sink IIs; slot-section shortfalls are
        # reported as MISSING (QTY_SHORT is for consumable sections).
        self.assertEqual(top["missing"], 2)
        self.assertEqual(top["total"], 2)

    def test_substitution_pairs(self):
        self._submit(self.subs, SUBS_EFT)
        analytics = reports.doctrine_failure_analytics(self.doctrine)
        pairs = analytics["top_substitutions"]
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["expected_type__name"], "Heat Sink II")
        self.assertEqual(pairs[0]["actual_type__name"], "Imperial Navy Heat Sink")
        self.assertEqual(pairs[0]["pilots"], 1)


class ChartTests(TestCase):
    def test_sparkline_degenerate_inputs(self):
        self.assertEqual(sparkline_points([]), "")
        single = sparkline_points([50.0])
        self.assertTrue(single)  # flat line, two points
        self.assertEqual(len(single.split()), 2)

    def test_sparkline_bounds(self):
        points = sparkline_points([0.0, 100.0, 50.0], width=120, height=28)
        for pair in points.split():
            x, y = (float(v) for v in pair.split(","))
            self.assertTrue(0 <= x <= 120)
            self.assertTrue(0 <= y <= 28)

    def test_trend_chart_needs_two_points(self):
        class Snap:
            def __init__(self, date, audience, compliant, subs):
                self.date = date
                self.audience_count = audience
                self.compliant_count = compliant
                self.compliant_subs_count = subs
                self.non_compliant_count = 0
                self.no_submission_count = 0

        today = timezone.now().date()
        self.assertIsNone(build_trend_chart([]))
        self.assertIsNone(build_trend_chart([Snap(today, 10, 5, 0)]))
        chart = build_trend_chart(
            [
                Snap(today - dt.timedelta(days=1), 10, 5, 0),
                Snap(today, 10, 8, 1),
            ]
        )
        self.assertEqual(len(chart["points"]), 2)
        self.assertEqual(chart["points"][1]["ready_pct"], 90.0)
        self.assertEqual(len(chart["y_ticks"]), 5)
        self.assertTrue(chart["x_ticks"])


class ReportsViewTests(ReportsTestCase):
    def test_all_urls_require_the_perm(self):
        self.client.force_login(self.compliant)  # basic_access only
        urls = [
            reverse("fitcheck:reports_overview"),
            reverse("fitcheck:reports_overview_csv"),
            reverse("fitcheck:reports_drilldown", args=[self.doctrine.pk]),
            reverse("fitcheck:reports_drilldown_csv", args=[self.doctrine.pk]),
        ]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 302, url)
        self.client.force_login(self.viewer)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_menu_tab_visibility(self):
        self.client.force_login(self.viewer)
        self.assertContains(
            self.client.get(reverse("fitcheck:index")),
            reverse("fitcheck:reports_overview"),
        )
        self.client.force_login(self.compliant)
        self.assertNotContains(
            self.client.get(reverse("fitcheck:index")),
            reverse("fitcheck:reports_overview"),
        )

    def test_overview_renders_and_filters_by_category(self):
        _snap(self.doctrine)
        other = create_doctrine(name="Elsewhere")
        category = DoctrineCategory.objects.create(name="Caps")
        self.doctrine.categories.add(category)
        self.client.force_login(self.viewer)
        resp = self.client.get(reverse("fitcheck:reports_overview"))
        self.assertContains(resp, self.doctrine.name)
        self.assertContains(resp, other.name)
        resp = self.client.get(
            reverse("fitcheck:reports_overview"), {"category": category.pk}
        )
        self.assertContains(resp, self.doctrine.name)
        self.assertNotContains(resp, other.name)

    def test_overview_warns_when_no_snapshots(self):
        self.client.force_login(self.viewer)
        resp = self.client.get(reverse("fitcheck:reports_overview"))
        self.assertContains(resp, "take_compliance_snapshots")
        _snap(self.doctrine)
        resp = self.client.get(reverse("fitcheck:reports_overview"))
        self.assertNotContains(resp, "trend history cannot be backfilled")

    def test_drilldown_state_filter_and_search(self):
        self._submit_all_states()
        self.client.force_login(self.viewer)
        url = reverse("fitcheck:reports_drilldown", args=[self.doctrine.pk])
        resp = self.client.get(url, {"state": "no_submission"})
        self.assertContains(resp, "Pilot rep-silent")
        self.assertNotContains(resp, "Pilot rep-failing")
        resp = self.client.get(url, {"q": "rep-subs"})
        self.assertContains(resp, "Pilot rep-subs")
        self.assertNotContains(resp, "Pilot rep-silent")

    def test_drilldown_paginates_at_50(self):
        for i in range(60):
            create_user(f"rep-bulk-{i}")
        self.client.force_login(self.viewer)
        resp = self.client.get(
            reverse("fitcheck:reports_drilldown", args=[self.doctrine.pk])
        )
        self.assertEqual(resp.context["page_obj"].paginator.num_pages, 2)

    def test_drilldown_without_snapshots_still_lists_members(self):
        self.client.force_login(self.viewer)
        resp = self.client.get(
            reverse("fitcheck:reports_drilldown", args=[self.doctrine.pk])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Not enough snapshot history")
        self.assertContains(resp, "Pilot rep-silent")

    def test_overview_csv(self):
        _snap(self.doctrine)
        category = DoctrineCategory.objects.create(name="Caps")
        self.doctrine.categories.add(category)
        other = create_doctrine(name="Elsewhere")
        self.client.force_login(self.viewer)
        resp = self.client.get(
            reverse("fitcheck:reports_overview_csv"), {"category": category.pk}
        )
        self.assertEqual(resp["Content-Type"], "text/csv")
        self.assertIn("attachment; filename=", resp["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8"))))
        self.assertEqual(rows[0][0], "Doctrine")
        names = [r[0] for r in rows[1:]]
        self.assertIn(self.doctrine.name, names)
        self.assertNotIn(other.name, names)

    def test_drilldown_csv_applies_same_filters_unpaginated(self):
        self._submit_all_states()
        for i in range(60):
            create_user(f"rep-bulk-{i}")
        self.client.force_login(self.viewer)
        resp = self.client.get(
            reverse("fitcheck:reports_drilldown_csv", args=[self.doctrine.pk]),
            {"state": "no_submission"},
        )
        rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8"))))
        data = rows[1:]
        # All 60 bulk users + rep-silent + viewer (both no-submission), one page.
        self.assertGreater(len(data), 50)
        self.assertTrue(all(r[2] == "No submission" for r in data))
