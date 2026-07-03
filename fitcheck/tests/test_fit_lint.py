from django.test import TestCase

from ..constants import Section
from ..services.fit_lint import slot_layout_warnings
from .testdata.factories import add_item, create_doctrine, create_fit
from .testdata.sde_fixtures import T, create_sde_testdata


class TestSlotLayoutWarnings(TestCase):
    """Harbinger fixture slot layout: 8 High / 4 Mid / 6 Low / 3 Rig."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()

    def test_fit_within_layout_has_no_warnings(self):
        fit = create_fit(self.doctrine, T.HARBINGER, name="Within Layout")
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 6)
        add_item(fit, Section.MED, T.CAP_RECHARGER_II, 4)
        add_item(fit, Section.HIGH, T.PULSE_LASER_II, 8)
        add_item(fit, Section.RIG, T.STRUCTURE_RIG_I, 3)
        self.assertEqual(slot_layout_warnings(fit), [])

    def test_low_section_exceeding_warns_once(self):
        fit = create_fit(self.doctrine, T.HARBINGER, name="Overloaded Lows")
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 7)  # hull has 6
        warnings = slot_layout_warnings(fit)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Low slots", warnings[0])
        self.assertIn("7", warnings[0])
        self.assertIn("6", warnings[0])

    def test_two_sections_exceeding_warns_twice(self):
        fit = create_fit(self.doctrine, T.HARBINGER, name="Overloaded Two")
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 7)  # hull has 6
        add_item(fit, Section.MED, T.CAP_RECHARGER_II, 5)  # hull has 4
        warnings = slot_layout_warnings(fit)
        self.assertEqual(len(warnings), 2)

    def test_hull_with_no_slot_attribute_rows_is_silent(self):
        # Oracle fixture carries no slot attrs (pre-reload mirror scenario).
        fit = create_fit(self.doctrine, T.ORACLE, name="No Attrs")
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 50)
        self.assertEqual(slot_layout_warnings(fit), [])

    def test_strategic_cruiser_hull_is_exempt(self):
        # Legion fixture has 4/4/4/3 slots and deliberately exceeded lows.
        fit = create_fit(self.doctrine, T.LEGION, name="T3C Overloaded")
        add_item(fit, Section.LOW, T.HEAT_SINK_II, 10)
        self.assertEqual(slot_layout_warnings(fit), [])
