"""Tests for the UI/flow rework: slot-group policies, ANY enforcement,
fittings-plugin conversion, multi-doctrine matching and inventory item mapping."""

from django.test import TestCase

from ..constants import Section
from ..models import CompliancePolicy, FitSubmission, PolicySlotRule, SubmissionItem
from ..models.doctrine import EnforcementMode, SubstitutionPolicy
from ..services.check_runner import matching_fits_for, validate_parsed_ship
from ..services.compliance import check_fit
from ..services.eft_parser import parse_eft
from ..services.esi_assets import fit_items_from_flags
from ..services.fit_data import FitItem, ParsedFit
from ..services.policies import apply_policy_to_fit
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import Attrs, T, create_sde_testdata


class TestAnyEnforcement(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER)

    def test_any_policy_skips_quantity_sections(self):
        # Slot-section ANY semantics (foreign module passes, never MISSING) are
        # covered in test_compliance_engine (TestNoEnforcementFinding,
        # TestCargoRefitFallback); the quantity-section ANY skip lives only here.
        add_item(self.fit, Section.DRONE_BAY, T.HOBGOBLIN_II, 5, policy=SubstitutionPolicy.ANY)
        result = check_fit(parse_eft("[Harbinger, X]\n"), self.fit)
        self.assertEqual(result.verdict, FitSubmission.Verdict.COMPLIANT)


class TestPolicyApplication(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER)
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        add_item(cls.fit, Section.MED, T.CAP_RECHARGER_II, 1)
        add_item(cls.fit, Section.DRONE_BAY, T.HOBGOBLIN_II, 5)
        add_item(cls.fit, Section.BOOSTER, T.BOOSTER_STANDARD, 2)
        cls.admin = create_user("padmin", permissions=["basic_access", "manage_policies"])
        cls.policy = CompliancePolicy.objects.create(name="Test Policy", created_by=cls.admin)
        PolicySlotRule.objects.create(
            policy=cls.policy,
            section=Section.LOW,
            enforcement=EnforcementMode.EXACT,
        )
        PolicySlotRule.objects.create(
            policy=cls.policy,
            section=Section.MED,
            enforcement=EnforcementMode.GTE,
            allow_mutated=False,
        )
        PolicySlotRule.objects.create(
            policy=cls.policy,
            section=Section.DRONE_BAY,
            enforcement=EnforcementMode.ANY,
            min_quantity_pct=50,
        )
        # Boosters are a quantity-leeway section in the policy editor (#14): the
        # rule must write min_quantity_pct onto booster items.
        PolicySlotRule.objects.create(
            policy=cls.policy,
            section=Section.BOOSTER,
            enforcement=EnforcementMode.EXACT,
            min_quantity_pct=80,
        )

    def test_rules_map_to_item_policies(self):
        updated = apply_policy_to_fit(self.fit, self.policy)
        self.assertEqual(updated, 4)
        items = {i.section: i for i in self.fit.items.all()}
        self.assertEqual(items[Section.LOW].policy, SubstitutionPolicy.EXACT)
        self.assertEqual(items[Section.MED].policy, SubstitutionPolicy.MEET_OR_BEAT)
        self.assertFalse(items[Section.MED].allow_mutated)
        self.assertEqual(items[Section.DRONE_BAY].policy, SubstitutionPolicy.ANY)
        self.assertEqual(items[Section.DRONE_BAY].min_quantity_pct, 50)
        self.assertEqual(items[Section.BOOSTER].policy, SubstitutionPolicy.EXACT)
        self.assertEqual(items[Section.BOOSTER].min_quantity_pct, 80)
        self.fit.refresh_from_db()
        self.assertEqual(self.fit.compliance_policy, self.policy)


class TestMultiDoctrineMatching(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.member = create_user("member")
        cls.armor = create_doctrine(name="Armor")
        cls.shield = create_doctrine(name="Shield")
        cls.fit_a = create_fit(cls.armor, T.HARBINGER, name="Armor Harb")
        add_item(cls.fit_a, Section.LOW, T.HEAT_SINK_II, 1)
        cls.fit_b = create_fit(cls.shield, T.HARBINGER, name="Shield Harb")
        add_item(cls.fit_b, Section.MED, T.CAP_RECHARGER_II, 1)
        # One fitting shared by both doctrines plus a standalone baseline.
        cls.fit_b.doctrines.add(cls.armor)
        cls.baseline = create_fit(None, T.HARBINGER, name="Baseline Harb")
        cls.other_hull = create_fit(None, T.ORACLE, name="Oracle Standard")

    def test_matching_fits_cover_doctrines_and_standalone(self):
        names = {fit.name for fit in matching_fits_for(self.member, T.HARBINGER)}
        self.assertEqual(names, {"Armor Harb", "Shield Harb", "Baseline Harb"})

    def test_one_ship_yields_one_submission_per_fit_doctrine_pair(self):
        """A ship grades once per (matching fit, visible doctrine) pair: a fit
        in two doctrines yields two submissions (one per policy snapshot), and a
        standalone fit yields one graded against its source-level defaults."""
        parsed = ParsedFit(ship_type_id=T.HARBINGER, fit_name="My Harb", items=[])
        submissions = validate_parsed_ship(self.member, parsed)
        # fit_a (armor) -> 1; fit_b (shield + armor) -> 2; baseline (standalone) -> 1
        self.assertEqual(len(submissions), 4)
        self.assertEqual(FitSubmission.objects.filter(user=self.member).count(), 4)
        for submission in submissions:
            self.assertEqual(submission.source, FitSubmission.Source.ESI)
        # The standalone baseline grades against source defaults (no doctrine).
        baseline_subs = [s for s in submissions if s.doctrine_fit_id == self.baseline.pk]
        self.assertEqual(len(baseline_subs), 1)
        self.assertIsNone(baseline_subs[0].doctrine)
        # fit_b sits in both doctrines -> one submission per doctrine.
        fit_b_doctrines = {
            s.doctrine_id for s in submissions if s.doctrine_fit_id == self.fit_b.pk
        }
        self.assertEqual(fit_b_doctrines, {self.armor.pk, self.shield.pk})


class TestFlaggedItemMapping(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_slots_bays_and_loaded_charges(self):
        rows = [
            (T.HEAT_SINK_II, "LoSlot0", 1),
            (T.HEAT_SINK_II, "LoSlot1", 1),
            (T.PULSE_LASER_II, "HiSlot0", 1),
            (T.MULTIFREQ_L, "HiSlot0", 1),  # loaded charge -> pooled into cargo
            (T.HOBGOBLIN_II, "DroneBay", 5),
            (T.MULTIFREQ_L, "Cargo", 4),
            (T.HEAT_SINK_II, "Hangar", 1),  # unfitted spare in hangar -> ignored
        ]
        items = fit_items_from_flags(rows)
        by_section: dict[str, dict[int, int]] = {}
        for item in items:
            by_section.setdefault(item.section, {}).setdefault(item.type_id, 0)
            by_section[item.section][item.type_id] += item.quantity
        self.assertEqual(by_section[Section.LOW][T.HEAT_SINK_II], 2)
        self.assertEqual(by_section[Section.HIGH][T.PULSE_LASER_II], 1)
        self.assertEqual(by_section[Section.CARGO][T.MULTIFREQ_L], 5)
        self.assertEqual(by_section[Section.DRONE_BAY][T.HOBGOBLIN_II], 5)
        self.assertNotIn(T.HEAT_SINK_II, by_section.get(Section.CARGO, {}))

    def test_fleet_hangar_items_map_to_cargo(self):
        """Carriers, supercarriers, Orca, Rorqual, etc. expose refit modules
        in FleetHangar. The engine should treat that as Cargo so fitted-refit
        logic and cargo demands see them."""
        items = fit_items_from_flags([(T.HEAT_SINK_II, "FleetHangar", 3)])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].section, Section.CARGO)
        self.assertEqual(items[0].type_id, T.HEAT_SINK_II)
        self.assertEqual(items[0].quantity, 3)

    def test_fleet_hangar_satisfies_cargo_demand_end_to_end(self):
        """Regression for submission #10: a fleet-hangar module previously
        showed MISSING because the parser dropped FleetHangar items entirely."""
        from ..models.doctrine import SubstitutionPolicy
        from ..services.compliance import check_fit
        from ..services.fit_data import ParsedFit
        from ..models import ComplianceFinding, FitSubmission
        from .testdata.factories import add_item, create_doctrine, create_fit

        doctrine = create_doctrine(name="Fleet hangar regression")
        fit = create_fit(doctrine, T.HARBINGER, name="Refit fit")
        add_item(fit, Section.CARGO, T.HEAT_SINK_II, 1, policy=SubstitutionPolicy.EXACT)

        # Asset rows: nothing fitted; module sits in fleet hangar.
        items = fit_items_from_flags([(T.HEAT_SINK_II, "FleetHangar", 1)])
        result = check_fit(ParsedFit(ship_type_id=T.HARBINGER, items=items), fit)
        codes = {f.code for f in result.findings}
        self.assertNotIn(ComplianceFinding.Code.MISSING, codes)
        self.assertNotIn(ComplianceFinding.Code.QTY_SHORT, codes)
        self.assertEqual(result.verdict, FitSubmission.Verdict.COMPLIANT)


class TestFrigateEscapeBayDetection(TestCase):
    """build_parsed_fit captures FEB contents from the asset tree and the model
    persists them; the submission detail view surfaces them in a gated panel."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def _mock_asset_payload(self, ship_item_id, feb_present):
        """Minimal ESI assets payload: one ship row + optional FEB row."""
        rows = [
            {
                "item_id": ship_item_id,
                "type_id": T.HARBINGER,  # parent battleship-stand-in for the test
                "location_id": 60003760,
                "location_flag": "Hangar",
                "is_singleton": True,
                "quantity": 1,
            }
        ]
        if feb_present:
            rows.append({
                "item_id": ship_item_id + 1,
                "type_id": T.WEB_II,  # any non-zero type_id; the engine doesn't care
                "location_id": ship_item_id,
                "location_flag": "FrigateEscapeBay",
                "quantity": 1,
            })
        return rows

    def _build(self, feb_present):
        """Run build_parsed_fit with mocked ESI calls and return the ParsedFit."""
        from unittest.mock import patch
        from ..services import esi_assets

        ship_item_id = 10_000_000_001
        rows = self._mock_asset_payload(ship_item_id, feb_present)
        with patch.object(esi_assets, "user_tokens_by_character",
                          return_value=({1: object()}, [])):
            with patch.object(esi_assets, "_fetch_assets", return_value=rows):
                with patch.object(esi_assets, "_fetch_asset_names",
                                  return_value={ship_item_id: "Brick Brawler"}):
                    with patch.object(esi_assets, "_verify_mutated_items"):
                        return esi_assets.build_parsed_fit(
                            user=None, character_id=1, ship_item_id=ship_item_id
                        )

    def test_build_parsed_fit_extracts_frigate_escape_bay_contents(self):
        parsed = self._build(feb_present=True)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.frigate_escape_bay_type_id, T.WEB_II)

    def test_build_parsed_fit_handles_empty_feb(self):
        parsed = self._build(feb_present=False)
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed.frigate_escape_bay_type_id)

    def _build_with_implants(self, fetch_implants):
        from unittest.mock import patch
        from ..services import esi_assets

        ship_item_id = 10_000_000_055
        rows = self._mock_asset_payload(ship_item_id, feb_present=False)
        with patch.object(esi_assets, "user_tokens_by_character",
                          return_value=({1: object()}, [])):
            with patch.object(esi_assets, "_fetch_assets", return_value=rows):
                with patch.object(esi_assets, "_fetch_asset_names",
                                  return_value={ship_item_id: "Implanted"}):
                    with patch.object(esi_assets, "_verify_mutated_items"):
                        with patch.object(esi_assets, "get_active_implants",
                                          return_value={T.IMPLANT_SM705}) as gai:
                            parsed = esi_assets.build_parsed_fit(
                                user=None, character_id=1, ship_item_id=ship_item_id,
                                fetch_implants=fetch_implants,
                            )
                            return parsed, gai

    def test_fetch_implants_populates_pilot_implant_type_ids(self):
        parsed, gai = self._build_with_implants(fetch_implants=True)
        self.assertEqual(parsed.pilot_implant_type_ids, {T.IMPLANT_SM705})
        gai.assert_called_once_with(1)

    def test_implants_not_fetched_by_default(self):
        parsed, gai = self._build_with_implants(fetch_implants=False)
        self.assertIsNone(parsed.pilot_implant_type_ids)
        gai.assert_not_called()


class TestEsiClientWiring(TestCase):
    """The ESI provider must instantiate against the installed django-esi version."""

    def test_esi_client_provider_constructs(self):
        from ..services import esi_assets

        # Reset the module-level singleton so we genuinely exercise the import +
        # constructor, not a cache from a previous test.
        esi_assets._provider = None
        # We never call .client to make a real request, but ESIClientProvider.__str__
        # exercises constructor arg wiring cheaply.
        provider = esi_assets.esi_client()
        self.assertIn("AaFitcheck", str(provider))

    def test_esi_operation_names_resolve_against_installed_spec(self):
        """Every ESI operation name used in esi_assets must exist on the loaded
        client. Catches breaking renames between django-esi versions (e.g. the
        snake_case -> PascalCase change in 9.x) without making network calls."""
        from ..services import esi_assets

        esi_assets._provider = None
        client = esi_assets.esi_client().client
        # (tag, operation) pairs that esi_assets.py invokes.
        required = [
            ("Assets", "GetCharactersCharacterIdAssets"),
            ("Assets", "PostCharactersCharacterIdAssetsNames"),
            ("Universe", "PostUniverseNames"),
            ("Dogma", "GetDogmaDynamicItemsTypeIdItemId"),
            ("Fittings", "PostCharactersCharacterIdFittings"),
            ("Fittings", "GetCharactersCharacterIdFittings"),
            ("Clones", "GetCharactersCharacterIdImplants"),
            ("Universe", "GetUniverseStructuresStructureId"),
        ]
        for tag_name, op_name in required:
            tag = getattr(client, tag_name)
            self.assertIn(
                op_name,
                tag._operations,
                f"{tag_name}.{op_name} missing from the installed django-esi spec",
            )


class TestSavedFittingsIntake(TestCase):
    """ESI saved-fitting dicts convert into ParsedFit with correct sections."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_parsed_fit_from_saved_builds_sections(self):
        from ..services.esi_fittings import parsed_fit_from_saved

        fitting = {
            "fitting_id": 7,
            "name": "Brawler",
            "ship_type_id": T.HARBINGER,
            "items": [
                {"flag": "HiSlot0", "type_id": T.PULSE_LASER_II, "quantity": 1},
                {"flag": "LoSlot0", "type_id": T.HEAT_SINK_II, "quantity": 1},
                {"flag": "DroneBay", "type_id": T.HOBGOBLIN_II, "quantity": 5},
                {"flag": "Cargo", "type_id": T.NANITE_PASTE, "quantity": 50},
            ],
        }
        parsed = parsed_fit_from_saved(fitting)
        self.assertEqual(parsed.ship_type_id, T.HARBINGER)
        self.assertEqual(parsed.fit_name, "Brawler")
        self.assertIn(T.PULSE_LASER_II, {i.type_id for i in parsed.items_in(Section.HIGH)})
        self.assertIn(T.HEAT_SINK_II, {i.type_id for i in parsed.items_in(Section.LOW)})
        drones = {i.type_id: i.quantity for i in parsed.items_in(Section.DRONE_BAY)}
        self.assertEqual(drones[T.HOBGOBLIN_II], 5)
        cargo = {i.type_id: i.quantity for i in parsed.items_in(Section.CARGO)}
        self.assertEqual(cargo[T.NANITE_PASTE], 50)


class TestLocationResolution(TestCase):
    """_resolve_structure (used by the out-of-band refresh task only - inventory
    listings read locations from the StructureNameCache, never live)."""

    def test_resolve_structure_uses_single_object_result(self):
        """GetUniverseStructuresStructureId returns ONE object, so _resolve_structure
        must call .result() (singular). Calling .results() (plural) yields a list,
        which _to_dict passes through and .get() then blows up on - the live bug that
        left Citadels showing as a bare id. The fake op makes .results() return a list
        so a regression to the plural call fails here instead of only in production."""
        from unittest.mock import MagicMock, patch
        from ..services import esi_assets

        payload = {"name": "X47L-Q - Rogue Threshold", "solar_system_id": 30001967}
        op = MagicMock()
        op.result.return_value = payload          # singular: the object (correct)
        op.results.return_value = [payload]        # plural: a list (would break .get)
        client = MagicMock()
        client.Universe.GetUniverseStructuresStructureId.return_value = op
        provider = MagicMock()
        provider.client = client

        with patch.object(esi_assets, "esi_client", return_value=provider):
            name, system_id = esi_assets._resolve_structure(1_041_669_946_862, ["tok"])

        self.assertEqual(name, "X47L-Q - Rogue Threshold")
        self.assertEqual(system_id, 30001967)
        op.result.assert_called_once()
        op.results.assert_not_called()


class TestFittingsPluginConversion(TestCase):
    """convert_plugin_fit works on anything quacking like a fittings-plugin Fitting."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_convert_duck_typed_fitting(self):
        from ..services.fittings_import import convert_plugin_fit

        class FakeItem:
            def __init__(self, type_id, flag, quantity=1):
                self.type_id = type_id
                self.flag = flag
                self.quantity = quantity

        class FakeManager:
            def __init__(self, items):
                self._items = items

            def all(self):
                return self._items

        class FakeFitting:
            name = "Plugin Harb"
            ship_type_type_id = T.HARBINGER
            items = FakeManager(
                [
                    FakeItem(T.HEAT_SINK_II, "LoSlot0"),
                    FakeItem(T.HEAT_SINK_II, "LoSlot1"),
                    FakeItem(T.HOBGOBLIN_II, "DroneBay", 5),
                ]
            )

        parsed = convert_plugin_fit(FakeFitting())
        self.assertEqual(parsed.ship_type_id, T.HARBINGER)
        self.assertEqual(parsed.fit_name, "Plugin Harb")
        self.assertEqual(len(parsed.items), 3)
        sections = {item.section for item in parsed.items}
        self.assertEqual(sections, {Section.LOW, Section.DRONE_BAY})


def _fake_dogma_provider(rolls_by_item_id, *, raise_for=frozenset()):
    """Stand-in for esi_client(): its Dogma op returns canned dynamic-item data
    keyed by asset item_id (or raises, to exercise the fallthrough)."""

    class _Op:
        def __init__(self, item_id):
            self._item_id = item_id

        def result(self, use_etag=False):
            if self._item_id in raise_for:
                raise RuntimeError("ESI 500")
            rolls = rolls_by_item_id[self._item_id]
            return {
                "dogma_attributes": [
                    {"attribute_id": attr_id, "value": value}
                    for attr_id, value in rolls.items()
                ]
            }

    class _Dogma:
        def GetDogmaDynamicItemsTypeIdItemId(self, type_id, item_id):
            return _Op(item_id)

    class _Client:
        Dogma = _Dogma()

    class _Provider:
        client = _Client()

    return _Provider()


class TestMutatedRollEsiVerification(TestCase):
    """_verify_mutated_items pulls abyssal rolls from the dynamic-items endpoint
    and matches them to the exact fitted module by asset item_id."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def _verify(self, items, asset_rows, provider):
        from unittest.mock import patch

        from ..services import esi_assets

        with patch.object(esi_assets, "esi_client", return_value=provider):
            esi_assets._verify_mutated_items(items, asset_rows)

    def test_maps_rolls_and_stamps_esi_source(self):
        item = FitItem(Section.MED, T.WEB_ABYSSAL, 1, source_item_id=111)
        rows = [{"type_id": T.WEB_ABYSSAL, "item_id": 111}]
        provider = _fake_dogma_provider(
            {111: {Attrs.WEB_STRENGTH: -62.5, Attrs.WEB_RANGE: 15000}}
        )
        self._verify([item], rows, provider)
        self.assertEqual(
            item.mutated_attributes, {Attrs.WEB_STRENGTH: -62.5, Attrs.WEB_RANGE: 15000}
        )
        self.assertEqual(
            item.mutation_source, SubmissionItem.MutationSource.ESI_VERIFIED
        )

    def test_same_type_modules_keep_distinct_rolls(self):
        """Regression: two abyssal webs of the same type must not share rolls."""
        a = FitItem(Section.MED, T.WEB_ABYSSAL, 1, source_item_id=111)
        b = FitItem(Section.MED, T.WEB_ABYSSAL, 1, source_item_id=222)
        rows = [
            {"type_id": T.WEB_ABYSSAL, "item_id": 111},
            {"type_id": T.WEB_ABYSSAL, "item_id": 222},
        ]
        provider = _fake_dogma_provider(
            {
                111: {Attrs.WEB_STRENGTH: -62.5, Attrs.WEB_RANGE: 15000},
                222: {Attrs.WEB_STRENGTH: -48.0, Attrs.WEB_RANGE: 11000},
            }
        )
        self._verify([a, b], rows, provider)
        self.assertEqual(a.mutated_attributes[Attrs.WEB_STRENGTH], -62.5)
        self.assertEqual(b.mutated_attributes[Attrs.WEB_STRENGTH], -48.0)

    def test_lookup_error_leaves_item_unverified(self):
        item = FitItem(Section.MED, T.WEB_ABYSSAL, 1, source_item_id=111)
        rows = [{"type_id": T.WEB_ABYSSAL, "item_id": 111}]
        provider = _fake_dogma_provider({}, raise_for={111})
        self._verify([item], rows, provider)
        self.assertIsNone(item.mutated_attributes)
        self.assertEqual(item.mutation_source, "")

    def test_non_abyssal_module_untouched(self):
        item = FitItem(Section.LOW, T.HEAT_SINK_II, 1, source_item_id=999)
        rows = [{"type_id": T.HEAT_SINK_II, "item_id": 999}]
        # Provider would KeyError if called; the abyssal pre-filter must skip it.
        self._verify([item], rows, _fake_dogma_provider({}))
        self.assertIsNone(item.mutated_attributes)
        self.assertEqual(item.mutation_source, "")

    def test_build_parsed_fit_verifies_each_abyssal_module(self):
        """End-to-end through build_parsed_fit: two same-type abyssal modules in
        different slots come back with their own rolls."""
        from unittest.mock import patch

        from ..services import esi_assets

        ship_item_id = 10_000_000_900
        assets = [
            {"item_id": ship_item_id, "type_id": T.HARBINGER, "location_id": 0,
             "location_flag": "Hangar", "quantity": 1},
            {"item_id": 111, "type_id": T.WEB_ABYSSAL, "location_id": ship_item_id,
             "location_flag": "MedSlot0", "quantity": 1},
            {"item_id": 222, "type_id": T.WEB_ABYSSAL, "location_id": ship_item_id,
             "location_flag": "MedSlot1", "quantity": 1},
        ]
        provider = _fake_dogma_provider(
            {
                111: {Attrs.WEB_STRENGTH: -62.5, Attrs.WEB_RANGE: 15000},
                222: {Attrs.WEB_STRENGTH: -48.0, Attrs.WEB_RANGE: 11000},
            }
        )
        with patch.object(esi_assets, "user_tokens_by_character",
                          return_value=({1: object()}, [])), \
                patch.object(esi_assets, "_fetch_assets", return_value=assets), \
                patch.object(esi_assets, "_fetch_asset_names",
                             return_value={ship_item_id: "Web Boat"}), \
                patch.object(esi_assets, "esi_client", return_value=provider):
            parsed = esi_assets.build_parsed_fit(
                user=None, character_id=1, ship_item_id=ship_item_id
            )
        webs = sorted(
            (i for i in parsed.items if i.type_id == T.WEB_ABYSSAL),
            key=lambda i: i.source_item_id,
        )
        self.assertEqual(len(webs), 2)
        self.assertEqual(webs[0].mutated_attributes[Attrs.WEB_STRENGTH], -62.5)
        self.assertEqual(webs[1].mutated_attributes[Attrs.WEB_STRENGTH], -48.0)
        self.assertTrue(
            all(w.mutation_source == SubmissionItem.MutationSource.ESI_VERIFIED
                for w in webs)
        )


class TestMutationCappedFinding(TestCase):
    """#48: a mutated module whose roll lookup was skipped by the per-ship
    abyssal cap must say so, distinct from "no rolled stats were provided"
    (which would otherwise blame the pilot for a scan-side truncation)."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Web Fit")
        add_item(
            cls.fit, Section.MED, T.WEB_II, 1,
            policy=SubstitutionPolicy.MEET_OR_BEAT,
            checked_attributes=[Attrs.WEB_STRENGTH, Attrs.WEB_RANGE],
        )

    def _check(self, mutation_capped):
        item = FitItem(
            section=Section.MED, type_id=T.WEB_ABYSSAL, quantity=1,
            mutated_attributes=None, mutation_capped=mutation_capped,
        )
        result = check_fit(ParsedFit(ship_type_id=T.HARBINGER, items=[item]), self.fit)
        return next(
            f for f in result.findings
            if f.actual_type_id == T.WEB_ABYSSAL
        )

    def test_capped_lookup_names_the_scan_limit(self):
        finding = self._check(mutation_capped=True)
        self.assertIn("per-ship abyssal lookup cap", finding.message)
        self.assertIn("Scan & Result Limits", finding.message)
        self.assertNotIn("no rolled stats were provided", finding.message)

    def test_uncapped_missing_rolls_keeps_old_wording(self):
        finding = self._check(mutation_capped=False)
        self.assertIn("no rolled stats were provided", finding.message)
        self.assertNotIn("per-ship abyssal lookup cap", finding.message)
