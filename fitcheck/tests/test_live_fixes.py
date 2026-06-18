"""Regression tests for the live-test fix batch:

- item 2: boosters classify as BOOSTER and are warn-only (not missing implants)
- item 4: an exact-type slot surplus reads as EXTRA, never "not a substitute for itself"
- item 5: skill/bookkeeping attributes are excluded from meet-or-beat comparison
- item 6: a manager-chosen subset of required attributes ignores the rest
"""

from django.test import TestCase
from django.urls import reverse

from ..constants import DEFAULT_EXCLUDED_CHECK_ATTRIBUTES, Section, SlotKind
from ..models import (
    ComplianceFinding,
    FitSubmission,
    SdeMutaplasmidMapping,
    SdeType,
    SdeTypeAttribute,
)
from ..models.doctrine import SubstitutionPolicy
from ..services.compliance import check_fit
from ..services.fit_data import FitItem, ParsedFit
from ..services.substitutions import (
    _default_checked_attributes,
    candidate_attributes_for_item,
    rollable_attributes_for_item,
)
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import Attrs, T, create_sde_testdata

Code = ComplianceFinding.Code
Verdict = FitSubmission.Verdict


def fit_of(*items, ship=T.HARBINGER, implants=None):
    return ParsedFit(ship_type_id=ship, items=list(items), pilot_implant_type_ids=implants)


class TestSlotSurplus(TestCase):
    """Item 4: a surplus of the exact doctrine module is EXTRA, not a failed
    substitution against itself."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()

    def test_exact_surplus_is_extra_not_self_substitute(self):
        fit = create_fit(self.doctrine, T.HARBINGER, name="surplus")
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 2, policy=SubstitutionPolicy.VARIANTS)
        result = check_fit(fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 3)), fit)
        result_codes = [f.code for f in result.findings]
        self.assertIn(Code.OK, result_codes)
        self.assertIn(Code.EXTRA, result_codes)
        self.assertNotIn(Code.NOT_ALLOWED, result_codes)
        extra = next(f for f in result.findings if f.code == Code.EXTRA)
        self.assertEqual(extra.actual_type_id, T.HEAT_SINK_II)
        self.assertEqual(extra.actual_qty, 1)

    def test_exact_count_match_is_compliant(self):
        fit = create_fit(self.doctrine, T.HARBINGER, name="exact")
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.VARIANTS)
        result = check_fit(fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 3)), fit)
        self.assertEqual(result.verdict, Verdict.COMPLIANT)
        self.assertNotIn(Code.NOT_ALLOWED, [f.code for f in result.findings])


class TestBoosterClassification(TestCase):
    """Item 2: boosters are a distinct section and warn-only."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()

    def test_booster_classified_as_booster_kind(self):
        self.assertEqual(
            SdeType.objects.get(type_id=T.BOOSTER_STANDARD).slot_kind, SlotKind.BOOSTER
        )

    def test_doctrine_booster_warns_not_implant_missing(self):
        fit = create_fit(self.doctrine, T.HARBINGER, name="boost")
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        add_item(
            fit, Section.BOOSTER, T.BOOSTER_STANDARD, 1, policy=SubstitutionPolicy.EXACT
        )
        result = check_fit(fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 1)), fit)
        result_codes = [f.code for f in result.findings]
        self.assertIn(Code.UNVERIFIED, result_codes)
        self.assertNotIn(Code.IMPLANT_MISSING, result_codes)
        # Warn-only: an unverifiable booster never hard-fails the fit.
        self.assertNotEqual(result.verdict, Verdict.NON_COMPLIANT)
        unv = next(f for f in result.findings if f.code == Code.UNVERIFIED)
        self.assertEqual(unv.section, Section.BOOSTER)


class TestMeetOrBeatAttributes(TestCase):
    """Items 5 & 6: bookkeeping attributes excluded; candidate list and
    manager-chosen subset behave correctly."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()

    def test_skill_attributes_are_excluded_by_default(self):
        # requiredSkill1 (182) and requiredSkill1Level (277) are bookkeeping.
        self.assertIn(182, DEFAULT_EXCLUDED_CHECK_ATTRIBUTES)
        self.assertIn(277, DEFAULT_EXCLUDED_CHECK_ATTRIBUTES)

    def test_rollable_offers_fitting_attrs_default_excludes(self):
        # Make the web's mutaplasmid also roll CPU, and give the web a CPU value.
        SdeTypeAttribute.objects.create(
            eve_type_id=T.WEB_II, attribute_id=Attrs.CPU_USAGE, value=30
        )
        mapping = SdeMutaplasmidMapping.objects.get(source_type_id=T.WEB_II)
        mapping.mutable_attributes = mapping.mutable_attributes + [
            {"attr_id": Attrs.CPU_USAGE, "min": 0.9, "max": 1.1, "high_is_good": False}
        ]
        mapping.save(update_fields=["mutable_attributes"])
        fit = create_fit(self.doctrine, T.HARBINGER, name="cpu")
        item = add_item(
            fit, Section.MED, T.WEB_II, 1, policy=SubstitutionPolicy.MEET_OR_BEAT
        )
        rollable = {a["attr_id"] for a in rollable_attributes_for_item(item)}
        self.assertIn(Attrs.CPU_USAGE, rollable)  # the modal offers CPU
        default = {a["attr_id"] for a in candidate_attributes_for_item(item)}
        self.assertNotIn(Attrs.CPU_USAGE, default)  # auto-default still skips it

    def test_candidate_attributes_lists_meaningful_set(self):
        fit = create_fit(self.doctrine, T.HARBINGER, name="cand")
        item = add_item(
            fit, Section.MED, T.WEB_II, 1, policy=SubstitutionPolicy.MEET_OR_BEAT
        )
        cands = {c["attr_id"]: c for c in candidate_attributes_for_item(item)}
        self.assertIn(Attrs.WEB_STRENGTH, cands)
        self.assertIn(Attrs.WEB_RANGE, cands)
        self.assertNotIn(Attrs.CPU_USAGE, cands)  # fitting cost - excluded
        self.assertFalse(cands[Attrs.WEB_STRENGTH]["high_is_good"])
        self.assertEqual(cands[Attrs.WEB_STRENGTH]["baseline"], -60)

    def _abyssal_web_fit(self, checked):
        fit = create_fit(self.doctrine, T.HARBINGER, name=f"ab-{len(checked)}")
        add_item(
            fit, Section.MED, T.WEB_II, 1,
            policy=SubstitutionPolicy.MEET_OR_BEAT, checked_attributes=checked,
        )
        return fit

    def _abyssal_submission(self):
        # Beats strength (-62.5 < -60) but fails range (12000 < 14000).
        return fit_of(
            FitItem(
                Section.MED, T.WEB_ABYSSAL, 1,
                mutated_attributes={Attrs.WEB_STRENGTH: -62.5, Attrs.WEB_RANGE: 12000},
            )
        )

    def test_checking_both_attributes_fails_on_range(self):
        fit = self._abyssal_web_fit([Attrs.WEB_STRENGTH, Attrs.WEB_RANGE])
        result = check_fit(self._abyssal_submission(), fit)
        self.assertEqual(result.verdict, Verdict.NON_COMPLIANT)

    def test_ignoring_range_lets_it_pass(self):
        fit = self._abyssal_web_fit([Attrs.WEB_STRENGTH])
        result = check_fit(self._abyssal_submission(), fit)
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)

    def test_attribute_bound_override_uses_worst_side_threshold(self):
        """Item 12: a manager window sets the worst acceptable value; a roll at
        least that good passes, a worse one fails. Web strength is lower-is-better
        so the worst-side handle is the upper (max) bound."""
        from ..services.substitutions import resolve_allowed_bulk

        fit = create_fit(self.doctrine, T.HARBINGER, name="bound")
        item = add_item(
            fit, Section.MED, T.WEB_II, 1,
            policy=SubstitutionPolicy.MEET_OR_BEAT,
            checked_attributes=[Attrs.WEB_STRENGTH],
            attribute_bounds={str(Attrs.WEB_STRENGTH): {"min": -72, "max": -55}},
        )
        allowed = resolve_allowed_bulk([item])[item.pk]
        # -62.5 is better than the -55 worst-side bound (lower = better) -> pass.
        passed, _ = allowed.evaluate_mutated(T.WEB_ABYSSAL, {Attrs.WEB_STRENGTH: -62.5})
        self.assertTrue(passed)
        # -50 is worse than -55 -> fail.
        passed, _ = allowed.evaluate_mutated(T.WEB_ABYSSAL, {Attrs.WEB_STRENGTH: -50})
        self.assertFalse(passed)


class TestAttributePolicyView(TestCase):
    """Item 6: the save endpoint stores a validated checked_attributes list."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.mgr = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.fit = create_fit(create_doctrine(), T.HARBINGER, name="view")
        self.item = add_item(
            self.fit, Section.MED, T.WEB_II, 1, policy=SubstitutionPolicy.MEET_OR_BEAT
        )

    def test_save_stores_selected_attributes(self):
        self.client.force_login(self.mgr)
        resp = self.client.post(
            reverse("fitcheck:attribute_policy_save", args=[self.item.pk]),
            {"attr_ids": [str(Attrs.WEB_STRENGTH)]},
        )
        self.assertEqual(resp.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.checked_attributes, [Attrs.WEB_STRENGTH])

    def test_non_candidate_attribute_is_rejected(self):
        self.client.force_login(self.mgr)
        # CPU usage is excluded from the meaningful set, so it must not stick.
        self.client.post(
            reverse("fitcheck:attribute_policy_save", args=[self.item.pk]),
            {"attr_ids": [str(Attrs.CPU_USAGE)]},
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.checked_attributes, [])

    def test_empty_selection_clears_to_defaults(self):
        self.item.checked_attributes = [Attrs.WEB_STRENGTH]
        self.item.save(update_fields=["checked_attributes"])
        self.client.force_login(self.mgr)
        self.client.post(
            reverse("fitcheck:attribute_policy_save", args=[self.item.pk]), {}
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.checked_attributes, [])

    def test_fit_items_page_offers_attribute_modal_for_mb_item(self):
        self.client.force_login(self.mgr)
        resp = self.client.get(reverse("fitcheck:manage_fit_items", args=[self.fit.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Abyssal attributes")  # modal trigger button
        # Candidates are served by the JSON endpoint that populates the modal,
        # carrying the abyssal min/max range for each attribute.
        cand = self.client.get(
            reverse("fitcheck:attribute_candidates", args=[self.item.pk])
        )
        attrs = {a["label"]: a for a in cand.json()["attributes"]}
        self.assertIn("Maximum Velocity Bonus", attrs)  # WEB_STRENGTH
        self.assertIn("abyssal_min", attrs["Maximum Velocity Bonus"])


class TestDeficitMultibuy(TestCase):
    """Item 2: the Missing Modules helper lists the gaps in multibuy format."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_lists_missing_quantity(self):
        from ..services.check_runner import build_deficit_multibuy, submit_fit
        from ..services.eft_parser import parse_eft

        doctrine = create_doctrine()
        fit = create_fit(doctrine, T.HARBINGER, name="deficit")
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.EXACT)
        user = create_user("pilot-d")
        sub = submit_fit(user, fit, parse_eft("[Harbinger, X]\nHeat Sink II\n"))
        text = build_deficit_multibuy(sub)
        self.assertIn("Heat Sink II 2", text)  # need 3, had 1 -> 2 short


class TestSig3Filter(TestCase):
    def test_rounds_to_three_significant_figures(self):
        from ..templatetags.fitcheck_extras import sig3

        self.assertEqual(sig3(21.373750576376914), "21.4")
        self.assertEqual(sig3(0.92500001), "0.925")
        self.assertEqual(sig3(15000), "15000")
        self.assertEqual(sig3(None), None)
        self.assertEqual(sig3("?"), "?")


class TestReviewBulkDelete(TestCase):
    """Item 6: reviewers can bulk-delete any submission from the queue."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_reviewer_deletes_selected(self):
        from ..services.check_runner import submit_fit
        from ..services.eft_parser import parse_eft

        doctrine = create_doctrine()
        fit = create_fit(doctrine, T.HARBINGER, name="rev")
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        pilot = create_user("pilot-r")
        sub = submit_fit(pilot, fit, parse_eft("[Harbinger, X]\nHeat Sink II\n"))
        reviewer = create_user("rev-u", permissions=("basic_access", "review_submissions"))
        self.client.force_login(reviewer)
        self.client.post(
            reverse("fitcheck:review_submissions_delete_bulk"),
            {"submission_pks": [str(sub.pk)]},
        )
        self.assertFalse(FitSubmission.objects.filter(pk=sub.pk).exists())

    def test_member_cannot_bulk_delete(self):
        member = create_user("plain")
        self.client.force_login(member)
        resp = self.client.post(
            reverse("fitcheck:review_submissions_delete_bulk"),
            {"submission_pks": ["1"]},
        )
        self.assertEqual(resp.status_code, 403)  # logged in but lacks review perm
