from django.test import TestCase
from eveuniverse.models import EveType

from ..constants import EveMetaGroupId, Section
from ..models import ComplianceFinding, EnforcementSettings, FitItemOverride, FitSubmission
from ..models.doctrine import SubstitutionPolicy
from ..models.settings import VerificationMode
from ..services.compliance import check_fit
from ..services.fit_data import FitItem, ParsedFit
from .testdata.factories import add_item, create_doctrine, create_fit
from .testdata.sde_fixtures import Attrs, T, create_sde_testdata

Code = ComplianceFinding.Code
Verdict = FitSubmission.Verdict


def fit_of(*items: FitItem, ship=T.HARBINGER, implants=None) -> ParsedFit:
    return ParsedFit(
        ship_type_id=ship, items=list(items), pilot_implant_type_ids=implants
    )


def codes(result):
    return sorted(f.code for f in result.findings)


def finding(result, code):
    matches = [f for f in result.findings if f.code == code]
    assert matches, f"no finding with code {code}: {[(f.code, f.message) for f in result.findings]}"
    return matches[0]


class EngineTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()

    def make_fit(self, **kwargs):
        ship = kwargs.pop("ship", T.HARBINGER)
        return create_fit(self.doctrine, ship, name=f"fit-{self._testMethodName}", **kwargs)


class TestSlotMatching(EngineTestCase):
    def test_exact_fit_is_compliant(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 3)
        result = check_fit(fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 3)), fit)
        self.assertEqual(result.verdict, Verdict.COMPLIANT)
        ok = finding(result, Code.OK)
        self.assertEqual(ok.expected_qty, 3)

    def test_canonical_substitution_case(self):
        """3x Heat Sink II required; pilot has 2x HSII + 1x Imperial Navy."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.VARIANTS)
        result = check_fit(
            fit_of(
                FitItem(Section.LOW, T.HEAT_SINK_II, 2),
                FitItem(Section.LOW, T.HEAT_SINK_IMPERIAL, 1),
            ),
            fit,
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        ok = finding(result, Code.OK)
        self.assertEqual(ok.expected_qty, 2)  # exact pass consumed the two HSII first
        sub = finding(result, Code.SUBSTITUTE)
        self.assertEqual(sub.actual_type_id, T.HEAT_SINK_IMPERIAL)
        self.assertEqual(sub.expected_type_id, T.HEAT_SINK_II)

    def test_missing_module_lists_alternatives(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 2, policy=SubstitutionPolicy.VARIANTS)
        result = check_fit(fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 1)), fit)
        self.assertEqual(result.verdict, Verdict.NON_COMPLIANT)
        missing = finding(result, Code.MISSING)
        self.assertEqual(missing.expected_qty, 1)
        alt_names = {a["name"] for a in missing.allowed_alternatives}
        self.assertIn("Imperial Navy Heat Sink", alt_names)

    def test_disallowed_variant_vs_foreign_module(self):
        fit = self.make_fit()
        add_item(
            fit, Section.MED, T.CAP_RECHARGER_II, 1,
            policy=SubstitutionPolicy.VARIANTS,
            allowed_meta_groups=[EveMetaGroupId.TECH_II],  # Tech I variant disallowed
        )
        result = check_fit(
            fit_of(
                FitItem(Section.MED, T.CAP_RECHARGER_I, 1),  # family, disallowed meta group
                FitItem(Section.MED, T.WEB_II, 1),  # unrelated module
            ),
            fit,
        )
        self.assertEqual(result.verdict, Verdict.NON_COMPLIANT)
        bad = finding(result, Code.NOT_ALLOWED)
        self.assertEqual(bad.actual_type_id, T.CAP_RECHARGER_I)
        extra = finding(result, Code.EXTRA)
        self.assertEqual(extra.actual_type_id, T.WEB_II)

    def test_surplus_valid_substitute_is_extra_not_not_allowed(self):
        """A leftover that IS a valid substitute, but for a demand already fully
        met, is a SURPLUS - it must warn as EXTRA ('not part of the fit'), not a
        red NOT_ALLOWED. The matching exact module's surplus already does this;
        the substitute surplus must behave the same way."""
        fit = self.make_fit()
        # Doctrine wants exactly 1 Imperial Navy Heat Sink, variant family allowed.
        add_item(
            fit, Section.LOW, T.HEAT_SINK_IMPERIAL, 1,
            policy=SubstitutionPolicy.VARIANTS,
        )
        result = check_fit(
            fit_of(
                FitItem(Section.LOW, T.HEAT_SINK_IMPERIAL, 1),  # exact: satisfies the demand
                FitItem(Section.LOW, T.HEAT_SINK_II, 1),  # valid variant, but surplus
            ),
            fit,
        )
        # The single demand is met exactly; the extra Heat Sink II is surplus.
        self.assertNotIn(Code.NOT_ALLOWED, codes(result))
        ok = finding(result, Code.OK)
        self.assertEqual(ok.expected_type_id, T.HEAT_SINK_IMPERIAL)
        extra = finding(result, Code.EXTRA)
        self.assertEqual(extra.actual_type_id, T.HEAT_SINK_II)
        # Surplus is a warning, not a hard fail.
        self.assertNotEqual(result.verdict, Verdict.NON_COMPLIANT)

    def test_extras_warn_unless_strict(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        parsed = fit_of(
            FitItem(Section.LOW, T.HEAT_SINK_II, 1),
            FitItem(Section.MED, T.WEB_II, 1),
        )
        result = check_fit(parsed, fit)
        self.assertEqual(result.verdict, Verdict.COMPLIANT)
        self.assertIn(Code.EXTRA, codes(result))

        fit.strict_extras = True
        fit.save()
        result = check_fit(parsed, fit)
        self.assertEqual(result.verdict, Verdict.NON_COMPLIANT)

    def test_wrong_hull_short_circuits(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        result = check_fit(fit_of(ship=T.ORACLE), fit)
        self.assertEqual(result.verdict, Verdict.NON_COMPLIANT)
        self.assertEqual(codes(result), [Code.WRONG_HULL])

    def test_unresolved_names_give_error_verdict(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        parsed = fit_of()
        from ..services.fit_data import ParseError

        parsed.errors.append(ParseError(3, "Mystery Module", "unknown type name"))
        result = check_fit(parsed, fit)
        self.assertEqual(result.verdict, Verdict.ERROR)
        self.assertEqual(codes(result), [Code.UNRESOLVED])

    def test_overlapping_substitute_sets_use_bipartite_matching(self):
        """Item A accepts both faction sinks; item B (via include override) accepts
        only the Imperial. Pilot has one of each - both slots must fill."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1, policy=SubstitutionPolicy.VARIANTS)
        item_b = add_item(
            fit, Section.LOW, T.HEAT_SINK_BASIC, 1, policy=SubstitutionPolicy.EXACT
        )
        FitItemOverride.objects.create(
            item=item_b,
            alt_type=EveType.objects.get(id=T.HEAT_SINK_IMPERIAL),
            mode=FitItemOverride.Mode.INCLUDE,
        )
        result = check_fit(
            fit_of(
                FitItem(Section.LOW, T.HEAT_SINK_IMPERIAL, 1),
                FitItem(Section.LOW, T.HEAT_SINK_AMMATAR, 1),
            ),
            fit,
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        self.assertNotIn(Code.MISSING, codes(result))
        self.assertNotIn(Code.NOT_ALLOWED, codes(result))


class TestMutatedModules(EngineTestCase):
    def _web_fit(self):
        fit = self.make_fit()
        add_item(
            fit, Section.MED, T.WEB_II, 1,
            policy=SubstitutionPolicy.MEET_OR_BEAT,
            checked_attributes=[Attrs.WEB_STRENGTH, Attrs.WEB_RANGE],
        )
        return fit

    def test_abyssal_with_winning_rolls_is_substitute(self):
        result = check_fit(
            fit_of(
                FitItem(
                    Section.MED, T.WEB_ABYSSAL, 1,
                    mutated_attributes={Attrs.WEB_STRENGTH: -62.5, Attrs.WEB_RANGE: 15000},
                    mutation_source="PYFA",
                )
            ),
            self._web_fit(),
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        sub = finding(result, Code.SUBSTITUTE)
        self.assertTrue(all(row["passed"] for row in sub.attribute_results))

    def test_abyssal_with_losing_roll_fails_with_attribute_table(self):
        result = check_fit(
            fit_of(
                FitItem(
                    Section.MED, T.WEB_ABYSSAL, 1,
                    mutated_attributes={Attrs.WEB_STRENGTH: -55, Attrs.WEB_RANGE: 15000},
                    mutation_source="PYFA",
                )
            ),
            self._web_fit(),
        )
        self.assertEqual(result.verdict, Verdict.NON_COMPLIANT)
        bad = finding(result, Code.NOT_ALLOWED)
        failed = [row for row in bad.attribute_results if not row["passed"]]
        self.assertEqual(len(failed), 1)

    def test_abyssal_without_rolls_fails_with_guidance(self):
        result = check_fit(
            fit_of(FitItem(Section.MED, T.WEB_ABYSSAL, 1)), self._web_fit()
        )
        self.assertEqual(result.verdict, Verdict.NON_COMPLIANT)
        bad = finding(result, Code.NOT_ALLOWED)
        self.assertIn("Pyfa", bad.message)

    def test_allowed_abyssal_shows_passfail_not_extra(self):
        """Req 7: when the doctrine item allows mutated, the abyssal module is
        graded by its attributes (pass=SUBSTITUTE, fail=NOT_ALLOWED) and carries
        an attribute table - it is never reported as EXTRA 'Not part of the fit'."""
        passing = check_fit(
            fit_of(
                FitItem(
                    Section.MED, T.WEB_ABYSSAL, 1,
                    mutated_attributes={Attrs.WEB_STRENGTH: -62.5, Attrs.WEB_RANGE: 15000},
                    mutation_source="PYFA",
                )
            ),
            self._web_fit(),
        )
        self.assertNotIn(Code.EXTRA, codes(passing))
        self.assertTrue(finding(passing, Code.SUBSTITUTE).attribute_results)

        failing = check_fit(
            fit_of(
                FitItem(
                    Section.MED, T.WEB_ABYSSAL, 1,
                    mutated_attributes={Attrs.WEB_STRENGTH: -55, Attrs.WEB_RANGE: 15000},
                    mutation_source="PYFA",
                )
            ),
            self._web_fit(),
        )
        self.assertNotIn(Code.EXTRA, codes(failing))
        self.assertTrue(finding(failing, Code.NOT_ALLOWED).attribute_results)


class TestQuantitySections(EngineTestCase):
    def test_consumable_leeway_boundary(self):
        fit = self.make_fit()
        add_item(
            fit, Section.CARGO, T.NITROGEN_ISOTOPES, 30000,
            policy=SubstitutionPolicy.EXACT, min_quantity_pct=66,
        )
        # ceil(30000 * 0.66) = 19800
        passing = check_fit(
            fit_of(FitItem(Section.CARGO, T.NITROGEN_ISOTOPES, 19800)), fit
        )
        self.assertEqual(passing.verdict, Verdict.COMPLIANT)

        failing = check_fit(
            fit_of(FitItem(Section.CARGO, T.NITROGEN_ISOTOPES, 19799)), fit
        )
        self.assertEqual(failing.verdict, Verdict.NON_COMPLIANT)
        short = finding(failing, Code.QTY_SHORT)
        self.assertEqual(short.expected_qty, 19800)
        self.assertEqual(short.actual_qty, 19799)

    def test_extra_drones_surface_in_your_fit(self):
        """A pilot's drones that aren't the doctrine's drones show as EXTRA with
        the actual type populated, so they appear in the 'Your fit' column rather
        than vanishing from the comparison (CARGO extras stay suppressed)."""
        fit = self.make_fit()
        add_item(fit, Section.DRONE_BAY, T.HOBGOBLIN_II, 5, policy=SubstitutionPolicy.EXACT)
        result = check_fit(
            fit_of(FitItem(Section.DRONE_BAY, T.HOBGOBLIN_I, 5)), fit
        )
        extras = [
            f for f in result.findings
            if f.section == Section.DRONE_BAY and f.code == Code.EXTRA
        ]
        self.assertEqual(len(extras), 1)
        self.assertEqual(extras[0].actual_type_id, T.HOBGOBLIN_I)
        self.assertEqual(extras[0].actual_qty, 5)
        # The doctrine's drone still reports a shortfall (none of the exact type).
        self.assertTrue(
            any(f.section == Section.DRONE_BAY and f.code == Code.QTY_SHORT
                for f in result.findings)
        )

    def test_cargo_extras_stay_suppressed(self):
        """CARGO is a bulk hold, not a loadout slot - extra cargo must NOT spam
        the comparison with EXTRA rows."""
        fit = self.make_fit()
        add_item(fit, Section.CARGO, T.NANITE_PASTE, 10, policy=SubstitutionPolicy.EXACT)
        result = check_fit(
            fit_of(
                FitItem(Section.CARGO, T.NANITE_PASTE, 10),
                FitItem(Section.CARGO, T.NITROGEN_ISOTOPES, 5000),  # unrelated extra
            ),
            fit,
        )
        self.assertFalse(
            any(f.section == Section.CARGO and f.code == Code.EXTRA for f in result.findings)
        )

    def test_crystals_at_full_quantity_via_loaded_charges(self):
        """Doctrine: 2 lasers each loaded with Multifrequency. Pilot satisfies the
        crystal demand purely with loaded charges."""
        fit = self.make_fit()
        add_item(
            fit, Section.HIGH, T.PULSE_LASER_II, 2,
            policy=SubstitutionPolicy.EXACT, charge_type_id=T.MULTIFREQ_L,
        )
        compliant = check_fit(
            fit_of(
                FitItem(Section.HIGH, T.PULSE_LASER_II, 2, charge_type_id=T.MULTIFREQ_L)
            ),
            fit,
        )
        self.assertEqual(compliant.verdict, Verdict.COMPLIANT)

        # One gun without a crystal and none in cargo -> short.
        short = check_fit(
            fit_of(
                FitItem(Section.HIGH, T.PULSE_LASER_II, 1, charge_type_id=T.MULTIFREQ_L),
                FitItem(Section.HIGH, T.PULSE_LASER_II, 1),
            ),
            fit,
        )
        self.assertEqual(short.verdict, Verdict.NON_COMPLIANT)
        qty = finding(short, Code.QTY_SHORT)
        self.assertEqual(qty.expected_type_id, T.MULTIFREQ_L)

    def test_drone_substitution_and_shortfall(self):
        fit = self.make_fit()
        add_item(
            fit, Section.DRONE_BAY, T.HOBGOBLIN_I, 5,
            policy=SubstitutionPolicy.VARIANTS,
        )
        upgraded = check_fit(
            fit_of(FitItem(Section.DRONE_BAY, T.HOBGOBLIN_II, 5)), fit
        )
        self.assertEqual(upgraded.verdict, Verdict.COMPLIANT_SUBS)

        short = check_fit(fit_of(FitItem(Section.DRONE_BAY, T.HOBGOBLIN_II, 3)), fit)
        self.assertEqual(short.verdict, Verdict.NON_COMPLIANT)
        qty = finding(short, Code.QTY_SHORT)
        self.assertEqual(qty.actual_qty, 3)

    def test_fighter_squadrons(self):
        fit = self.make_fit(ship=T.HEL)
        add_item(
            fit, Section.FIGHTER_BAY, T.TEMPLAR_I, 9, policy=SubstitutionPolicy.VARIANTS
        )
        result = check_fit(
            fit_of(FitItem(Section.FIGHTER_BAY, T.TEMPLAR_II, 9), ship=T.HEL), fit
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)

    def test_cargo_surplus_is_ignored(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        result = check_fit(
            fit_of(
                FitItem(Section.LOW, T.HEAT_SINK_II, 1),
                FitItem(Section.CARGO, T.NANITE_PASTE, 50),
            ),
            fit,
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT)


class TestImplants(EngineTestCase):
    def _implant_fit(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        add_item(
            fit, Section.IMPLANT, T.IMPLANT_SM705, 1, policy=SubstitutionPolicy.EXACT
        )
        return fit

    def _hull_items(self):
        return FitItem(Section.LOW, T.HEAT_SINK_II, 1)

    def test_unverifiable_submission_warns_but_never_fails(self):
        result = check_fit(fit_of(self._hull_items(), implants=None), self._implant_fit())
        self.assertEqual(result.verdict, Verdict.COMPLIANT)
        self.assertIn(Code.UNVERIFIED, codes(result))

    def test_missing_implant_fails_when_verifiable(self):
        result = check_fit(
            fit_of(self._hull_items(), implants=set()), self._implant_fit()
        )
        self.assertEqual(result.verdict, Verdict.NON_COMPLIANT)
        self.assertIn(Code.IMPLANT_MISSING, codes(result))

    def test_present_implant_passes(self):
        result = check_fit(
            fit_of(self._hull_items(), implants={T.IMPLANT_SM705}), self._implant_fit()
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT)

    def test_implant_carried_in_cargo_passes_as_refit(self):
        """A required implant the pilot isn't wearing but carries in cargo/fleet
        hangar passes as CARGO_REFIT ('Carried in cargo as refit')."""
        result = check_fit(
            fit_of(
                self._hull_items(),
                FitItem(Section.CARGO, T.IMPLANT_SM705, 1),
                implants=set(),  # verifiably NOT plugged in
            ),
            self._implant_fit(),
        )
        refit = next(
            f for f in result.findings
            if f.section == Section.IMPLANT and f.code == Code.CARGO_REFIT
        )
        self.assertEqual(refit.expected_type_id, T.IMPLANT_SM705)
        self.assertEqual(refit.actual_type_id, T.IMPLANT_SM705)
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        self.assertNotIn(Code.IMPLANT_MISSING, codes(result))

    def test_plugged_implant_preferred_over_cargo(self):
        """When the implant is both plugged and in cargo, it's reported as plugged
        (OK), not as a cargo refit."""
        result = check_fit(
            fit_of(
                self._hull_items(),
                FitItem(Section.CARGO, T.IMPLANT_SM705, 1),
                implants={T.IMPLANT_SM705},
            ),
            self._implant_fit(),
        )
        implant_codes = {
            f.code for f in result.findings if f.section == Section.IMPLANT
        }
        self.assertIn(Code.OK, implant_codes)
        self.assertNotIn(Code.CARGO_REFIT, implant_codes)


class TestFuelBayAndBoosters(EngineTestCase):
    """Fuel bay and boosters carry a Qty % but are warn-only: a shortfall reports
    UNVERIFIED and never makes the fit NON_COMPLIANT."""

    def _hull(self):
        return FitItem(Section.LOW, T.HEAT_SINK_II, 1)

    def _fuel_fit(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        add_item(
            fit, Section.FUEL_BAY, T.HELIUM_ISOTOPES, 1000,
            policy=SubstitutionPolicy.EXACT, min_quantity_pct=100,
        )
        return fit

    def test_fuel_short_warns_never_fails(self):
        result = check_fit(
            fit_of(self._hull(), FitItem(Section.FUEL_BAY, T.HELIUM_ISOTOPES, 500)),
            self._fuel_fit(),
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT)
        self.assertNotIn(Code.QTY_SHORT, codes(result))
        self.assertIn(Code.UNVERIFIED, codes(result))

    def test_fuel_met_is_ok(self):
        result = check_fit(
            fit_of(self._hull(), FitItem(Section.FUEL_BAY, T.HELIUM_ISOTOPES, 1000)),
            self._fuel_fit(),
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT)
        fuel_ok = [
            f for f in result.findings
            if f.section == Section.FUEL_BAY and f.code == Code.OK
        ]
        self.assertTrue(fuel_ok)

    def test_fuel_short_reports_held_quantity_in_actual(self):
        """A shortfall must record what the pilot actually holds of the expected
        type, so the 'Your fit' column shows e.g. 500/1000 fuel rather than blank."""
        result = check_fit(
            fit_of(self._hull(), FitItem(Section.FUEL_BAY, T.HELIUM_ISOTOPES, 500)),
            self._fuel_fit(),
        )
        short = next(
            f for f in result.findings
            if f.section == Section.FUEL_BAY and f.code == Code.UNVERIFIED
        )
        self.assertEqual(short.actual_type_id, T.HELIUM_ISOTOPES)
        self.assertEqual(short.actual_qty, 500)

    def test_fuel_absent_leaves_actual_type_blank(self):
        """When the pilot has none of the expected type, actual stays empty
        (no misleading 'x0' of a type they don't carry)."""
        result = check_fit(
            fit_of(self._hull()),  # no fuel at all
            self._fuel_fit(),
        )
        short = next(
            f for f in result.findings
            if f.section == Section.FUEL_BAY and f.code == Code.UNVERIFIED
        )
        self.assertIsNone(short.actual_type_id)
        self.assertEqual(short.actual_qty, 0)

    def test_fuel_in_cargo_counts_as_carried_refit(self):
        """Capital jump fuel carried in the cargo hold or fleet hangar (both ESI
        location flags map to Section.CARGO) counts toward the fuel-bay
        requirement, flagged CARGO_REFIT - not in the bay, but accounted for."""
        result = check_fit(
            fit_of(self._hull(), FitItem(Section.CARGO, T.HELIUM_ISOTOPES, 1000)),
            self._fuel_fit(),
        )
        refit = next(
            f for f in result.findings
            if f.section == Section.FUEL_BAY and f.code == Code.CARGO_REFIT
        )
        self.assertEqual(refit.actual_type_id, T.HELIUM_ISOTOPES)
        self.assertEqual(refit.actual_qty, 1000)
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        self.assertNotIn(Code.UNVERIFIED, codes(result))  # requirement met, no shortfall

    def test_fuel_split_bay_and_cargo_pools(self):
        """Fuel partly in the bay and partly in cargo pools to meet the
        requirement: the bay portion is a clean OK, the cargo portion CARGO_REFIT."""
        result = check_fit(
            fit_of(
                self._hull(),
                FitItem(Section.FUEL_BAY, T.HELIUM_ISOTOPES, 600),
                FitItem(Section.CARGO, T.HELIUM_ISOTOPES, 400),
            ),
            self._fuel_fit(),
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        ok = next(
            f for f in result.findings
            if f.section == Section.FUEL_BAY and f.code == Code.OK
        )
        refit = next(
            f for f in result.findings
            if f.section == Section.FUEL_BAY and f.code == Code.CARGO_REFIT
        )
        self.assertEqual(ok.actual_qty, 600)
        self.assertEqual(refit.actual_qty, 400)
        self.assertNotIn(Code.UNVERIFIED, codes(result))

    def test_fuel_still_short_after_pooling_reports_total_held(self):
        """When the bay + cargo together still fall short, the shortfall reports
        the pooled total held (300 bay + 200 cargo = 500 of 1000)."""
        result = check_fit(
            fit_of(
                self._hull(),
                FitItem(Section.FUEL_BAY, T.HELIUM_ISOTOPES, 300),
                FitItem(Section.CARGO, T.HELIUM_ISOTOPES, 200),
            ),
            self._fuel_fit(),
        )
        self.assertNotEqual(result.verdict, Verdict.NON_COMPLIANT)  # warn-only by default
        short = next(
            f for f in result.findings
            if f.section == Section.FUEL_BAY and f.code == Code.UNVERIFIED
        )
        self.assertEqual(short.actual_qty, 500)

    def test_bay_fuel_alone_stays_clean_ok(self):
        """Regression: fuel actually in the bay is a clean OK / COMPLIANT, with no
        spurious carried-in-cargo row."""
        result = check_fit(
            fit_of(self._hull(), FitItem(Section.FUEL_BAY, T.HELIUM_ISOTOPES, 1000)),
            self._fuel_fit(),
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT)
        self.assertNotIn(Code.CARGO_REFIT, codes(result))

    def _booster_fit(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        add_item(
            fit, Section.BOOSTER, T.BOOSTER_STANDARD, 2,
            policy=SubstitutionPolicy.EXACT, min_quantity_pct=100,
        )
        return fit

    def test_booster_short_warns_never_fails(self):
        result = check_fit(
            fit_of(self._hull(), FitItem(Section.BOOSTER, T.BOOSTER_STANDARD, 1)),
            self._booster_fit(),
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT)
        self.assertNotIn(Code.QTY_SHORT, codes(result))
        self.assertIn(Code.UNVERIFIED, codes(result))

    def test_booster_present_enough_is_ok(self):
        result = check_fit(
            fit_of(self._hull(), FitItem(Section.BOOSTER, T.BOOSTER_STANDARD, 2)),
            self._booster_fit(),
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT)
        booster_ok = [
            f for f in result.findings
            if f.section == Section.BOOSTER and f.code == Code.OK
        ]
        self.assertTrue(booster_ok)

    def test_booster_carried_in_cargo_passes_as_refit(self):
        """Active boosters can't be read from ESI, but a spare carried in cargo /
        fleet hangar passes as CARGO_REFIT - any amount counts (doctrine wants 2,
        pilot carries 1)."""
        result = check_fit(
            fit_of(self._hull(), FitItem(Section.CARGO, T.BOOSTER_STANDARD, 1)),
            self._booster_fit(),
        )
        refit = next(
            f for f in result.findings
            if f.section == Section.BOOSTER and f.code == Code.CARGO_REFIT
        )
        self.assertEqual(refit.actual_type_id, T.BOOSTER_STANDARD)
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        self.assertNotIn(Code.UNVERIFIED, codes(result))


class TestEnforcementModes(EngineTestCase):
    """The site EnforcementSettings 4-mode selectors govern fuel / booster /
    implant verification (defaults preserve the historical warn/policy behaviour;
    those defaults are exercised by TestFuelBayAndBoosters / TestImplants)."""

    def _set(self, **modes):
        EnforcementSettings.objects.update_or_create(pk=1, defaults=modes)

    def _fuel_fit(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        add_item(
            fit, Section.FUEL_BAY, T.HELIUM_ISOTOPES, 1000,
            policy=SubstitutionPolicy.EXACT, min_quantity_pct=100,
        )
        return fit

    def _short_fuel(self, fit):
        return check_fit(
            fit_of(
                FitItem(Section.LOW, T.HEAT_SINK_II, 1),
                FitItem(Section.FUEL_BAY, T.HELIUM_ISOTOPES, 500),
            ),
            fit,
        )

    def test_fuel_reject_hard_fails(self):
        self._set(fuel_mode=VerificationMode.REJECT)
        r = self._short_fuel(self._fuel_fit())
        self.assertEqual(r.verdict, Verdict.NON_COMPLIANT)
        self.assertIn(Code.QTY_SHORT, codes(r))

    def test_fuel_ignore_emits_nothing(self):
        self._set(fuel_mode=VerificationMode.IGNORE)
        r = self._short_fuel(self._fuel_fit())
        self.assertEqual(r.verdict, Verdict.COMPLIANT)
        self.assertEqual([f for f in r.findings if f.section == Section.FUEL_BAY], [])

    def test_fuel_carried_in_cargo_passes_under_reject(self):
        """Carried fuel passes in every mode, like carried implants/boosters: a
        Reject fuel mode with the pilot's fuel entirely in cargo / the fleet
        hangar is still a pass (CARGO_REFIT), not a QTY_SHORT."""
        self._set(fuel_mode=VerificationMode.REJECT)
        r = check_fit(
            fit_of(
                FitItem(Section.LOW, T.HEAT_SINK_II, 1),
                FitItem(Section.CARGO, T.HELIUM_ISOTOPES, 1000),
            ),
            self._fuel_fit(),
        )
        self.assertEqual(r.verdict, Verdict.COMPLIANT_SUBS)
        self.assertIn(Code.CARGO_REFIT, codes(r))
        self.assertNotIn(Code.QTY_SHORT, codes(r))

    def _booster_fit(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        add_item(
            fit, Section.BOOSTER, T.BOOSTER_STANDARD, 2,
            policy=SubstitutionPolicy.EXACT, min_quantity_pct=100,
        )
        return fit

    def test_booster_reject_absent_hard_fails(self):
        self._set(booster_mode=VerificationMode.REJECT)
        r = check_fit(fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 1)), self._booster_fit())
        self.assertEqual(r.verdict, Verdict.NON_COMPLIANT)
        self.assertIn(Code.QTY_SHORT, codes(r))

    def _implant_fit(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        add_item(fit, Section.IMPLANT, T.IMPLANT_SM705, 1, policy=SubstitutionPolicy.EXACT)
        return fit

    def test_implant_carried_in_cargo_passes_under_reject(self):
        """Even under the strict Reject mode, an implant carried in cargo passes
        as CARGO_REFIT (carrying it is proof of possession)."""
        self._set(implant_mode=VerificationMode.REJECT)
        r = check_fit(
            fit_of(
                FitItem(Section.LOW, T.HEAT_SINK_II, 1),
                FitItem(Section.CARGO, T.IMPLANT_SM705, 1),
                implants=set(),
            ),
            self._implant_fit(),
        )
        self.assertEqual(r.verdict, Verdict.COMPLIANT_SUBS)
        self.assertIn(Code.CARGO_REFIT, codes(r))
        self.assertNotIn(Code.IMPLANT_MISSING, codes(r))

    def test_booster_carried_in_cargo_passes_under_reject(self):
        """A booster carried in cargo passes (REF) even under Reject."""
        self._set(booster_mode=VerificationMode.REJECT)
        r = check_fit(
            fit_of(
                FitItem(Section.LOW, T.HEAT_SINK_II, 1),
                FitItem(Section.CARGO, T.BOOSTER_STANDARD, 1),
            ),
            self._booster_fit(),
        )
        self.assertEqual(r.verdict, Verdict.COMPLIANT_SUBS)
        self.assertIn(Code.CARGO_REFIT, codes(r))
        self.assertNotIn(Code.QTY_SHORT, codes(r))

    def test_booster_ignore_emits_nothing(self):
        self._set(booster_mode=VerificationMode.IGNORE)
        r = check_fit(fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 1)), self._booster_fit())
        self.assertEqual([f for f in r.findings if f.section == Section.BOOSTER], [])

    def _implant_fit(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        add_item(fit, Section.IMPLANT, T.IMPLANT_SM705, 1, policy=SubstitutionPolicy.EXACT)
        return fit

    def test_implant_reject_unverifiable_fails(self):
        self._set(implant_mode=VerificationMode.REJECT)
        r = check_fit(
            fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 1), implants=None), self._implant_fit()
        )
        self.assertEqual(r.verdict, Verdict.NON_COMPLIANT)
        self.assertIn(Code.IMPLANT_MISSING, codes(r))

    def test_implant_warn_verifiable_missing_warns(self):
        self._set(implant_mode=VerificationMode.WARN)
        r = check_fit(
            fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 1), implants=set()), self._implant_fit()
        )
        self.assertEqual(r.verdict, Verdict.COMPLIANT)
        self.assertIn(Code.UNVERIFIED, codes(r))

    def test_implant_ignore_emits_nothing(self):
        self._set(implant_mode=VerificationMode.IGNORE)
        r = check_fit(
            fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 1), implants=set()), self._implant_fit()
        )
        self.assertEqual([f for f in r.findings if f.section == Section.IMPLANT], [])


class TestFeb(EngineTestCase):
    """Frigate Escape Bay matching against a doctrine-specified frigate, gated by
    the site feb_mode. Default (IGNORE) means no FEB check."""

    def _set(self, **modes):
        EnforcementSettings.objects.update_or_create(pk=1, defaults=modes)

    def _feb_fit(self, frigates=(T.ORACLE,)):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        fit.feb_frigate_type_ids = list(frigates)
        fit.save(update_fields=["feb_frigate_type_ids"])
        return fit

    def _check(self, feb_bay, allowed=(T.ORACLE,)):
        return check_fit(
            ParsedFit(
                ship_type_id=T.HARBINGER,
                items=[FitItem(Section.LOW, T.HEAT_SINK_II, 1)],
                frigate_escape_bay_type_id=feb_bay,
            ),
            self._feb_fit(allowed),
        )

    def test_match_is_ok(self):
        self._set(feb_mode=VerificationMode.POLICY)
        r = self._check(feb_bay=T.ORACLE)
        self.assertEqual(r.verdict, Verdict.COMPLIANT)
        self.assertTrue([f for f in r.findings if f.section == Section.FEB and f.code == Code.OK])

    def test_match_any_of_multiple_is_ok(self):
        self._set(feb_mode=VerificationMode.POLICY)
        r = self._check(feb_bay=T.HEL, allowed=(T.ORACLE, T.HEL))
        self.assertEqual(r.verdict, Verdict.COMPLIANT)
        self.assertTrue([f for f in r.findings if f.section == Section.FEB and f.code == Code.OK])

    def test_frigate_outside_multiple_set_fails(self):
        self._set(feb_mode=VerificationMode.POLICY)
        r = self._check(feb_bay=T.HARBINGER, allowed=(T.ORACLE, T.HEL))
        self.assertEqual(r.verdict, Verdict.NON_COMPLIANT)
        self.assertIn(Code.NOT_ALLOWED, codes(r))

    def test_mismatch_policy_hard_fails(self):
        self._set(feb_mode=VerificationMode.POLICY)
        r = self._check(feb_bay=T.HEL)
        self.assertEqual(r.verdict, Verdict.NON_COMPLIANT)
        self.assertIn(Code.NOT_ALLOWED, codes(r))

    def test_mismatch_warn_only_warns(self):
        self._set(feb_mode=VerificationMode.WARN)
        r = self._check(feb_bay=T.HEL)
        self.assertEqual(r.verdict, Verdict.COMPLIANT)
        self.assertTrue([f for f in r.findings if f.section == Section.FEB and f.code == Code.UNVERIFIED])

    def test_unknown_reject_fails(self):
        self._set(feb_mode=VerificationMode.REJECT)
        r = self._check(feb_bay=None)
        self.assertEqual(r.verdict, Verdict.NON_COMPLIANT)
        self.assertIn(Code.MISSING, codes(r))

    def test_unknown_policy_warns(self):
        self._set(feb_mode=VerificationMode.POLICY)
        r = self._check(feb_bay=None)
        self.assertEqual(r.verdict, Verdict.COMPLIANT)
        self.assertTrue([f for f in r.findings if f.section == Section.FEB and f.code == Code.UNVERIFIED])

    def test_ignore_emits_nothing(self):
        self._set(feb_mode=VerificationMode.IGNORE)
        r = self._check(feb_bay=T.HEL)
        self.assertEqual([f for f in r.findings if f.section == Section.FEB], [])

    def test_no_doctrine_frigate_no_check(self):
        self._set(feb_mode=VerificationMode.REJECT)
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)  # no feb_frigate_type_id
        r = check_fit(
            ParsedFit(
                ship_type_id=T.HARBINGER,
                items=[FitItem(Section.LOW, T.HEAT_SINK_II, 1)],
                frigate_escape_bay_type_id=T.ORACLE,
            ),
            fit,
        )
        self.assertEqual([f for f in r.findings if f.section == Section.FEB], [])


class TestCargoRefitFallback(EngineTestCase):
    """Modules sitting in cargo can satisfy a slot demand the same way a refit
    in-game would. The match emits a CARGO_REFIT finding (info-level), still
    bumps the verdict to COMPLIANT_SUBS, and consumes from the cargo pool so
    it doesn't also satisfy a CARGO demand for the same module."""

    def test_cargo_refit_covers_missing_slot_module(self):
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 3)
        result = check_fit(
            fit_of(
                FitItem(Section.LOW, T.HEAT_SINK_II, 2),
                FitItem(Section.CARGO, T.HEAT_SINK_II, 1),
            ),
            fit,
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        self.assertNotIn(Code.MISSING, codes(result))
        ref = finding(result, Code.CARGO_REFIT)
        self.assertEqual(ref.expected_qty, 1)
        self.assertEqual(ref.actual_type_id, T.HEAT_SINK_II)
        self.assertEqual(ref.section, Section.LOW)

    def test_cargo_refit_with_allowed_substitute_in_cargo(self):
        """Imperial Navy Heat Sink in cargo satisfies a Heat Sink II demand
        under VARIANTS policy."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.VARIANTS)
        result = check_fit(
            fit_of(
                FitItem(Section.LOW, T.HEAT_SINK_II, 2),
                FitItem(Section.CARGO, T.HEAT_SINK_IMPERIAL, 1),
            ),
            fit,
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        ref = finding(result, Code.CARGO_REFIT)
        self.assertEqual(ref.actual_type_id, T.HEAT_SINK_IMPERIAL)
        self.assertEqual(ref.expected_type_id, T.HEAT_SINK_II)

    def test_cargo_refit_does_not_double_count_when_cargo_demand_exists(self):
        """Doctrine wants 3 Heat Sink II in lows + 4 in cargo. Pilot has 2 lows
        + 5 cargo: refit consumes 1, cargo has 4 left, satisfies cargo demand."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 3)
        add_item(fit, Section.CARGO, T.HEAT_SINK_II, 4, policy=SubstitutionPolicy.EXACT)
        result = check_fit(
            fit_of(
                FitItem(Section.LOW, T.HEAT_SINK_II, 2),
                FitItem(Section.CARGO, T.HEAT_SINK_II, 5),
            ),
            fit,
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        self.assertNotIn(Code.QTY_SHORT, codes(result))
        self.assertNotIn(Code.MISSING, codes(result))
        self.assertIn(Code.CARGO_REFIT, codes(result))

    def test_cargo_refit_with_only_enough_for_one_side_fails_the_other(self):
        """Same shape but pilot has 2 lows + 4 cargo: refit takes 1, cargo has
        3 left, falls short of the 4-cargo demand."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 3)
        add_item(fit, Section.CARGO, T.HEAT_SINK_II, 4, policy=SubstitutionPolicy.EXACT)
        result = check_fit(
            fit_of(
                FitItem(Section.LOW, T.HEAT_SINK_II, 2),
                FitItem(Section.CARGO, T.HEAT_SINK_II, 4),
            ),
            fit,
        )
        self.assertEqual(result.verdict, Verdict.NON_COMPLIANT)
        self.assertIn(Code.CARGO_REFIT, codes(result))
        self.assertIn(Code.QTY_SHORT, codes(result))

    def test_no_refit_when_cargo_empty(self):
        """Baseline: without any cargo, the slot demand still goes MISSING."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 3)
        result = check_fit(
            fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 2)), fit
        )
        self.assertEqual(result.verdict, Verdict.NON_COMPLIANT)
        self.assertIn(Code.MISSING, codes(result))
        self.assertNotIn(Code.CARGO_REFIT, codes(result))

    def test_refit_finding_bumps_verdict_to_compliant_subs(self):
        """A purely exact-match fit + one refit module from cargo lands at
        COMPLIANT_SUBS, not COMPLIANT - reviewers need to see the asterisk."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        result = check_fit(
            fit_of(FitItem(Section.CARGO, T.HEAT_SINK_II, 1)), fit
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        self.assertIn(Code.CARGO_REFIT, codes(result))

    def test_refit_does_not_apply_under_no_enforcement(self):
        """Slots under ANY policy never go MISSING in the first place, so the
        refit pass shouldn't add noise to those sections."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.ANY)
        result = check_fit(
            fit_of(FitItem(Section.CARGO, T.HEAT_SINK_II, 1)), fit
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT)
        self.assertNotIn(Code.CARGO_REFIT, codes(result))


class TestFittedRefitFallback(EngineTestCase):
    """The reverse of CARGO_REFIT: a doctrine asks for X in cargo, the pilot
    has X fitted instead. Mark cargo as satisfied with FITTED_REFIT, don't
    mark the fitted slot as EXTRA, and don't double-count."""

    def test_fitted_module_satisfies_cargo_demand(self):
        fit = self.make_fit()
        # Doctrine: 1 Heat Sink II carried in cargo. No high-slot demand at all.
        add_item(fit, Section.CARGO, T.HEAT_SINK_II, 1, policy=SubstitutionPolicy.EXACT)
        # Pilot: 1 Heat Sink II fitted to a LOW slot instead of carried.
        result = check_fit(
            fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 1)), fit
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        # Cargo demand satisfied via fitted refit. The finding is shown under the
        # slot the module is ACTUALLY fitted in (LOW), not the cargo demand's
        # section - a fitted module belongs in its slot panel, not cargo.
        ref = finding(result, Code.FITTED_REFIT)
        self.assertEqual(ref.section, Section.LOW)
        self.assertEqual(ref.expected_type_id, T.HEAT_SINK_II)
        self.assertEqual(ref.expected_qty, 1)
        # No EXTRA finding for the fitted module - the cargo claim consumed it.
        self.assertNotIn(Code.EXTRA, codes(result))
        # And no QTY_SHORT for cargo.
        self.assertNotIn(Code.QTY_SHORT, codes(result))

    def test_fitted_substitute_satisfies_cargo_under_variants_policy(self):
        """Imperial Navy Heat Sink fitted to a low slot satisfies a Heat Sink II
        cargo demand under VARIANTS policy."""
        fit = self.make_fit()
        add_item(
            fit, Section.CARGO, T.HEAT_SINK_II, 1, policy=SubstitutionPolicy.VARIANTS
        )
        result = check_fit(
            fit_of(FitItem(Section.LOW, T.HEAT_SINK_IMPERIAL, 1)), fit
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        ref = finding(result, Code.FITTED_REFIT)
        self.assertEqual(ref.actual_type_id, T.HEAT_SINK_IMPERIAL)
        self.assertEqual(ref.expected_type_id, T.HEAT_SINK_II)
        self.assertNotIn(Code.EXTRA, codes(result))

    def test_fitted_module_not_consumed_when_its_own_slot_demands_it(self):
        """Doctrine wants 1 Heat Sink II in LOW slot AND 1 in cargo. Pilot has
        1 fitted - the slot demand claims it first; cargo goes QTY_SHORT."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        add_item(fit, Section.CARGO, T.HEAT_SINK_II, 1, policy=SubstitutionPolicy.EXACT)
        result = check_fit(
            fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 1)), fit
        )
        # Low-slot demand satisfied (OK), cargo demand goes short, no FRF.
        self.assertIn(Code.OK, codes(result))
        self.assertIn(Code.QTY_SHORT, codes(result))
        self.assertNotIn(Code.FITTED_REFIT, codes(result))
        self.assertEqual(result.verdict, Verdict.NON_COMPLIANT)

    def test_cargo_and_fitted_combined_satisfy_demand(self):
        """Doctrine asks for 2 Heat Sink II in cargo. Pilot has 1 in cargo + 1
        fitted: cargo OK accounts for 1, FITTED_REFIT for the other."""
        fit = self.make_fit()
        add_item(fit, Section.CARGO, T.HEAT_SINK_II, 2, policy=SubstitutionPolicy.EXACT)
        result = check_fit(
            fit_of(
                FitItem(Section.CARGO, T.HEAT_SINK_II, 1),
                FitItem(Section.LOW, T.HEAT_SINK_II, 1),
            ),
            fit,
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        ok = finding(result, Code.OK)
        self.assertEqual(ok.expected_qty, 1)
        ref = finding(result, Code.FITTED_REFIT)
        self.assertEqual(ref.expected_qty, 1)
        self.assertNotIn(Code.EXTRA, codes(result))
        self.assertNotIn(Code.QTY_SHORT, codes(result))

    def test_strict_extras_does_not_fail_on_fitted_refit(self):
        """A fitted-refit unit is consumed by the cargo claim, so strict_extras
        (which fails on EXTRA in slot sections) shouldn't trip on it."""
        fit = self.make_fit()
        fit.strict_extras = True
        fit.save()
        add_item(fit, Section.CARGO, T.HEAT_SINK_II, 1, policy=SubstitutionPolicy.EXACT)
        result = check_fit(
            fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 1)), fit
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        self.assertNotIn(Code.EXTRA, codes(result))

    def test_truly_foreign_module_still_emits_extra(self):
        """The deferred-EXTRA refactor mustn't lose the EXTRA finding when the
        fitted module has nothing to do with any cargo demand either."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1)
        result = check_fit(
            fit_of(
                FitItem(Section.LOW, T.HEAT_SINK_II, 1),
                FitItem(Section.MED, T.WEB_II, 1),
            ),
            fit,
        )
        self.assertIn(Code.EXTRA, codes(result))
        extra = finding(result, Code.EXTRA)
        self.assertEqual(extra.actual_type_id, T.WEB_II)

    def test_no_enforcement_slot_yields_module_to_cargo_refit(self):
        """Submission #79: a No-Enforcement high slot must not 'eat' a module the
        doctrine wants carried in cargo. Doctrine: high slot ANY + Heat Sink II in
        cargo. Pilot fits a Heat Sink II in the (free) high slot instead of carrying
        it. The high slot still passes as NO_ENFORCEMENT, and the cargo demand is
        satisfied as FITTED_REFIT (shown under the high slot) - so COMPLIANT_SUBS,
        not a QTY_SHORT hard-fail."""
        fit = self.make_fit()
        add_item(fit, Section.HIGH, T.PULSE_LASER_II, 1, policy=SubstitutionPolicy.ANY)
        add_item(fit, Section.CARGO, T.HEAT_SINK_II, 1, policy=SubstitutionPolicy.EXACT)
        result = check_fit(
            fit_of(FitItem(Section.HIGH, T.HEAT_SINK_II, 1)), fit
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        # High slot still passes as No Enforcement (the unchanged row).
        ne = finding(result, Code.NO_ENFORCEMENT)
        self.assertEqual(ne.expected_type_id, T.PULSE_LASER_II)
        self.assertEqual(ne.actual_type_id, T.HEAT_SINK_II)
        # Cargo refit satisfied by the fitted module, shown under the fitted slot.
        ref = finding(result, Code.FITTED_REFIT)
        self.assertEqual(ref.expected_type_id, T.HEAT_SINK_II)
        self.assertEqual(ref.section, Section.HIGH)
        self.assertNotIn(Code.QTY_SHORT, codes(result))
        self.assertNotIn(Code.EXTRA, codes(result))

    def test_exact_match_under_any_also_offers_cargo_refit(self):
        """A module the pilot fits that exactly matches an ANY-policy slot (pass 1)
        is also offered to the cargo refit fallback, for consistency with the
        substitute (pass 2) case above."""
        fit = self.make_fit()
        add_item(fit, Section.HIGH, T.HEAT_SINK_II, 1, policy=SubstitutionPolicy.ANY)
        add_item(fit, Section.CARGO, T.HEAT_SINK_II, 1, policy=SubstitutionPolicy.EXACT)
        result = check_fit(
            fit_of(FitItem(Section.HIGH, T.HEAT_SINK_II, 1)), fit
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT_SUBS)
        ref = finding(result, Code.FITTED_REFIT)
        self.assertEqual(ref.expected_type_id, T.HEAT_SINK_II)
        self.assertNotIn(Code.QTY_SHORT, codes(result))

    def test_no_enforcement_courtesy_clone_never_becomes_extra(self):
        """When no cargo demand claims it, the No-Enforcement courtesy clone must
        not leak out as an EXTRA (or a FITTED_REFIT) - the slot already accepted
        the module as NO_ENFORCEMENT and the fit stays COMPLIANT."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1, policy=SubstitutionPolicy.ANY)
        result = check_fit(
            fit_of(FitItem(Section.LOW, T.WEB_II, 1)), fit
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT)
        self.assertIn(Code.NO_ENFORCEMENT, codes(result))
        self.assertNotIn(Code.EXTRA, codes(result))
        self.assertNotIn(Code.FITTED_REFIT, codes(result))


class TestNoEnforcementFinding(EngineTestCase):
    """Modules accepted under an ANY-policy slot get NO_ENFORCEMENT, NOT OK.
    OK is reserved for actual exact matches; using it for ANY-policy passes
    was the bug surfaced on submission #5."""

    def test_any_policy_emits_no_enforcement_not_ok_for_foreign_module(self):
        """Drone Link Augmentor in a high slot where doctrine wants Heat Sink II
        under ANY policy: it passes, but as 'No enforcement', not 'Exact match'."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1, policy=SubstitutionPolicy.ANY)
        result = check_fit(
            fit_of(FitItem(Section.LOW, T.WEB_II, 1)), fit
        )
        ref = finding(result, Code.NO_ENFORCEMENT)
        self.assertEqual(ref.expected_type_id, T.HEAT_SINK_II)
        self.assertEqual(ref.actual_type_id, T.WEB_II)
        # And no OK finding should have been emitted for that pass.
        self.assertNotIn(Code.OK, codes(result))

    def test_exact_match_under_any_still_emits_ok(self):
        """If the pilot happens to fit the listed type, that's still an OK -
        it really is an exact match, ANY just means non-exacts pass too."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1, policy=SubstitutionPolicy.ANY)
        result = check_fit(
            fit_of(FitItem(Section.LOW, T.HEAT_SINK_II, 1)), fit
        )
        self.assertIn(Code.OK, codes(result))
        self.assertNotIn(Code.NO_ENFORCEMENT, codes(result))

    def test_no_enforcement_does_not_bump_to_compliant_subs(self):
        """A fit with only NO_ENFORCEMENT findings is COMPLIANT (not _SUBS) -
        admins consciously waived enforcement, not 'allowed substitution'."""
        fit = self.make_fit()
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 1, policy=SubstitutionPolicy.ANY)
        result = check_fit(
            fit_of(FitItem(Section.LOW, T.WEB_II, 1)), fit
        )
        self.assertEqual(result.verdict, Verdict.COMPLIANT)
