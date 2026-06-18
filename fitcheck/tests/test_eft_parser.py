from django.test import TestCase

from ..constants import Section
from ..services.eft_parser import aggregate_for_buy, parse_eft, render_eft
from .testdata.factories import add_item, create_fit
from .testdata.sde_fixtures import T, create_sde_testdata


def _items_by_type(parsed, section):
    return {i.type_id: i for i in parsed.items_in(section)}


class TestEftParser(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_parses_full_fit_with_sections_by_slot_kind(self):
        parsed = parse_eft(
            "[Harbinger, Armor Brawl]\n"
            "Heat Sink II\n"
            "Heat Sink II\n"
            "Imperial Navy Heat Sink\n"
            "[Empty Low slot]\n"
            "\n"
            "Cap Recharger II\n"
            "Stasis Webifier II /offline\n"
            "\n"
            "Focused Medium Pulse Laser II, Multifrequency L\n"
            "Focused Medium Pulse Laser II, Multifrequency L\n"
            "\n"
            "Hobgoblin II x5\n"
            "\n"
            "Multifrequency L x8\n"
            "Nanite Repair Paste x50\n"
        )
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.ship_type_id, T.HARBINGER)
        self.assertEqual(parsed.fit_name, "Armor Brawl")

        low = _items_by_type(parsed, Section.LOW)
        self.assertEqual(low[T.HEAT_SINK_II].quantity, 2)
        self.assertEqual(low[T.HEAT_SINK_IMPERIAL].quantity, 1)

        med = _items_by_type(parsed, Section.MED)
        self.assertIn(T.CAP_RECHARGER_II, med)
        self.assertIn(T.WEB_II, med)  # /offline still counts as fitted

        high = _items_by_type(parsed, Section.HIGH)
        laser = high[T.PULSE_LASER_II]
        self.assertEqual(laser.quantity, 2)
        self.assertEqual(laser.charge_type_id, T.MULTIFREQ_L)

        drones = _items_by_type(parsed, Section.DRONE_BAY)
        self.assertEqual(drones[T.HOBGOBLIN_II].quantity, 5)

        cargo = _items_by_type(parsed, Section.CARGO)
        self.assertEqual(cargo[T.MULTIFREQ_L].quantity, 8)
        self.assertEqual(cargo[T.NANITE_PASTE].quantity, 50)

    def test_module_with_quantity_is_cargo_spare(self):
        parsed = parse_eft("[Harbinger, Refit]\nHeat Sink II\nHeat Sink II x2\n")
        self.assertEqual(parsed.errors, [])
        self.assertEqual(_items_by_type(parsed, Section.LOW)[T.HEAT_SINK_II].quantity, 1)
        self.assertEqual(_items_by_type(parsed, Section.CARGO)[T.HEAT_SINK_II].quantity, 2)

    def test_fighters_classify_to_fighter_bay(self):
        parsed = parse_eft("[Hel, Supers]\nTemplar II x9\n")
        self.assertEqual(parsed.errors, [])
        self.assertEqual(
            _items_by_type(parsed, Section.FIGHTER_BAY)[T.TEMPLAR_II].quantity, 9
        )

    def test_implants_classify_to_implant_section(self):
        parsed = parse_eft(
            "[Harbinger, With implants]\nZainou 'Gnome' Shield Management SM-705 x1\n"
        )
        self.assertEqual(parsed.errors, [])
        self.assertIn(T.IMPLANT_SM705, _items_by_type(parsed, Section.IMPLANT))

    def test_isotopes_classify_to_fuel_bay(self):
        parsed = parse_eft("[Hel, Fueled]\nHelium Isotopes x5000\n")
        self.assertEqual(parsed.errors, [])
        fuel = _items_by_type(parsed, Section.FUEL_BAY)
        self.assertEqual(fuel[T.HELIUM_ISOTOPES].quantity, 5000)
        # ...and NOT misfiled into cargo.
        self.assertNotIn(T.HELIUM_ISOTOPES, _items_by_type(parsed, Section.CARGO))

    def test_unknown_name_is_line_numbered_error(self):
        parsed = parse_eft("[Harbinger, Bad]\nNot A Real Module\n")
        self.assertEqual(len(parsed.errors), 1)
        self.assertEqual(parsed.errors[0].line_no, 2)
        self.assertIn("unknown type name", parsed.errors[0].reason)

    def test_unknown_hull_is_error(self):
        parsed = parse_eft("[Not A Ship, X]\nHeat Sink II\n")
        self.assertIsNone(parsed.ship_type_id)
        self.assertTrue(parsed.has_blocking_errors)

    def test_garbage_input(self):
        parsed = parse_eft("complete nonsense without a header")
        self.assertTrue(parsed.has_blocking_errors)

    def test_pyfa_mutation_block(self):
        parsed = parse_eft(
            "[Harbinger, Abyssal]\n"
            "Abyssal Stasis Webifier [1]\n"
            "\n"
            "\n"
            "[1] Stasis Webifier II\n"
            "Gravid Stasis Webifier Mutaplasmid\n"
            "Maximum Velocity Bonus -62.5, Optimal Range 15000\n"
        )
        self.assertEqual(parsed.errors, [])
        web = _items_by_type(parsed, Section.MED)[T.WEB_ABYSSAL]
        self.assertEqual(web.mutation_source, "PYFA")
        self.assertEqual(web.mutated_attributes, {20: -62.5, 54: 15000.0})

    def test_render_eft_roundtrip(self):
        original = (
            "[Harbinger, Roundtrip]\n"
            "Heat Sink II\n"
            "Heat Sink II\n"
            "\n"
            "Focused Medium Pulse Laser II, Multifrequency L\n"
            "\n"
            "Hobgoblin II x5\n"
            "\n"
            "Nanite Repair Paste x50\n"
        )
        parsed = parse_eft(original)
        rendered = render_eft(parsed)
        reparsed = parse_eft(rendered)
        self.assertEqual(reparsed.errors, [])
        self.assertEqual(reparsed.ship_type_id, parsed.ship_type_id)
        self.assertEqual(
            sorted((i.section, i.type_id, i.quantity) for i in reparsed.items),
            sorted((i.section, i.type_id, i.quantity) for i in parsed.items),
        )

    def test_resolve_render_names_falls_back_to_eveuniverse(self):
        """Types outside our SDE slice (e.g. blueprints a pilot hauls in a bay)
        must render with their real eveuniverse name, not a bare 'Type 12345'."""
        from eveuniverse.models import EveGroup, EveType

        from ..models import SdeType
        from ..services.eft_parser import resolve_render_names

        bp_id = 990001
        self.assertFalse(SdeType.objects.filter(type_id=bp_id).exists())
        EveType.objects.create(
            id=bp_id,
            name="'Augmented' Hobgoblin Blueprint",
            eve_group=EveGroup.objects.first(),
            published=False,
        )
        names = resolve_render_names({bp_id})
        self.assertEqual(names[bp_id], "'Augmented' Hobgoblin Blueprint")


class TestAggregateForBuy(TestCase):
    """Buy-list aggregation: pool every fitted/cargo item by type_id and
    include the hull. Charges loaded into modules contribute one per gun."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_pools_types_across_sections_and_includes_hull(self):
        fit = create_fit(None, T.HARBINGER)
        add_item(fit, Section.LOW, T.HEAT_SINK_II, quantity=5)
        # 2 guns each loaded with a charge -> charge pooled with cargo
        add_item(
            fit, Section.HIGH, T.PULSE_LASER_II, quantity=2,
            charge_type_id=T.MULTIFREQ_L,
        )
        # Cargo carries more of the same charge - aggregation pools them.
        add_item(fit, Section.CARGO, T.MULTIFREQ_L, quantity=8)

        rows = aggregate_for_buy(
            fit.items.all().order_by("section", "module_type__name"),
            ship_type_id=fit.ship_type_id,
        )
        as_dict = dict(rows)
        # Hull always present as 1.
        self.assertEqual(as_dict["Harbinger"], 1)
        # Heat Sinks: 5.
        self.assertEqual(as_dict["Heat Sink II"], 5)
        # Lasers: 2.
        self.assertEqual(as_dict["Focused Medium Pulse Laser II"], 2)
        # Charges: 2 loaded + 8 cargo = 10 (this is the cross-section pool).
        self.assertEqual(as_dict["Multifrequency L"], 10)
        # Sorted case-insensitively by name.
        self.assertEqual(
            [name for name, _ in rows],
            sorted([name for name, _ in rows], key=str.lower),
        )

    def test_empty_fit_returns_just_hull(self):
        fit = create_fit(None, T.HARBINGER)
        rows = aggregate_for_buy(fit.items.all(), ship_type_id=fit.ship_type_id)
        self.assertEqual(rows, [("Harbinger", 1)])

    def test_no_ship_no_items_returns_empty(self):
        self.assertEqual(aggregate_for_buy([], ship_type_id=None), [])
