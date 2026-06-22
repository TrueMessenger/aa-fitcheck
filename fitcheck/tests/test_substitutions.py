from django.test import TestCase
from eveuniverse.models import EveType

from ..constants import EveCategoryId, EveMetaGroupId, Section, SlotKind
from ..models import FitItemOverride, SdeType
from ..models.doctrine import SubstitutionPolicy
from ..services.substitutions import (
    possible_meta_groups_bulk,
    possible_meta_groups_for_item,
    resolve_allowed_bulk,
)
from .testdata.factories import add_item, create_doctrine, create_fit
from .testdata.sde_fixtures import Attrs, T, create_sde_testdata


class TestSubstitutions(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER)

    def _resolve(self, item):
        return resolve_allowed_bulk([item])[item.pk]

    def test_variants_default_allows_all_meta_groups(self):
        item = add_item(
            self.fit, Section.LOW, T.HEAT_SINK_II, policy=SubstitutionPolicy.VARIANTS
        )
        allowed = self._resolve(item)
        # Default = all meta groups checked, no meta-level floor: every family
        # variant qualifies (faction AND lower-meta T1/basic).
        self.assertIn(T.HEAT_SINK_IMPERIAL, allowed.substitutes)
        self.assertIn(T.HEAT_SINK_AMMATAR, allowed.substitutes)
        self.assertIn(T.HEAT_SINK_I, allowed.substitutes)
        self.assertIn(T.HEAT_SINK_BASIC, allowed.substitutes)

    def test_variants_empty_meta_groups_allows_no_substitutes(self):
        # Reversed semantics: an empty allow-list means no family substitutes.
        item = add_item(
            self.fit, Section.LOW, T.HEAT_SINK_II,
            policy=SubstitutionPolicy.VARIANTS, allowed_meta_groups=[],
        )
        allowed = self._resolve(item)
        self.assertEqual(allowed.substitutes, {})
        self.assertTrue(allowed.allows_statically(T.HEAT_SINK_II))  # exact still ok

    def test_variants_meta_group_filter(self):
        item = add_item(
            self.fit, Section.LOW, T.HEAT_SINK_II,
            policy=SubstitutionPolicy.VARIANTS,
            allowed_meta_groups=[EveMetaGroupId.TECH_II],
        )
        allowed = self._resolve(item)
        self.assertNotIn(T.HEAT_SINK_IMPERIAL, allowed.substitutes)
        self.assertNotIn(T.HEAT_SINK_I, allowed.substitutes)  # T1 group filtered too

    def test_exact_policy_has_no_substitutes(self):
        item = add_item(
            self.fit, Section.LOW, T.HEAT_SINK_II, policy=SubstitutionPolicy.EXACT
        )
        allowed = self._resolve(item)
        self.assertEqual(allowed.substitutes, {})
        self.assertTrue(allowed.allows_statically(T.HEAT_SINK_II))

    def test_include_override_allows_foreign_type(self):
        item = add_item(
            self.fit, Section.MED, T.CAP_RECHARGER_II, policy=SubstitutionPolicy.EXACT
        )
        FitItemOverride.objects.create(
            item=item,
            alt_type=EveType.objects.get(id=T.WEB_II),
            mode=FitItemOverride.Mode.INCLUDE,
        )
        allowed = self._resolve(item)
        self.assertIn(T.WEB_II, allowed.substitutes)

    def test_exclude_override_removes_variant(self):
        item = add_item(
            self.fit, Section.LOW, T.HEAT_SINK_II, policy=SubstitutionPolicy.VARIANTS
        )
        FitItemOverride.objects.create(
            item=item,
            alt_type=EveType.objects.get(id=T.HEAT_SINK_AMMATAR),
            mode=FitItemOverride.Mode.EXCLUDE,
        )
        allowed = self._resolve(item)
        self.assertNotIn(T.HEAT_SINK_AMMATAR, allowed.substitutes)
        self.assertIn(T.HEAT_SINK_IMPERIAL, allowed.substitutes)

    def test_meet_or_beat_static_pass_and_fail(self):
        item = add_item(
            self.fit, Section.LOW, T.HEAT_SINK_II, policy=SubstitutionPolicy.MEET_OR_BEAT
        )
        allowed = self._resolve(item)
        # Faction beats T2 on both checked attributes (incl. lower-is-better RoF).
        self.assertIn(T.HEAT_SINK_IMPERIAL, allowed.substitutes)
        # T1 is worse on both.
        self.assertNotIn(T.HEAT_SINK_I, allowed.substitutes)
        # CPU usage varies across the family but is excluded by default.
        self.assertNotIn(Attrs.CPU_USAGE, allowed.checked_attributes)

    def test_meet_or_beat_respects_meta_groups(self):
        # Meta-group filter now applies to meets-or-beats too: a faction module
        # that beats the baseline is still rejected when Faction is unchecked.
        item = add_item(
            self.fit, Section.LOW, T.HEAT_SINK_II,
            policy=SubstitutionPolicy.MEET_OR_BEAT,
            allowed_meta_groups=[EveMetaGroupId.TECH_II],
        )
        allowed = self._resolve(item)
        self.assertNotIn(T.HEAT_SINK_IMPERIAL, allowed.substitutes)

    def test_meet_or_beat_lower_is_better_direction(self):
        item = add_item(
            self.fit, Section.MED, T.CAP_RECHARGER_II,
            policy=SubstitutionPolicy.MEET_OR_BEAT,
            checked_attributes=[Attrs.CAP_RECHARGE],
        )
        allowed = self._resolve(item)
        # Recharge bonus is lower-is-better: compact (-17) misses the T2 baseline (-20).
        self.assertNotIn(T.CAP_RECHARGER_COMPACT, allowed.substitutes)

    def test_meet_or_beat_collects_abyssal_candidates(self):
        item = add_item(
            self.fit, Section.MED, T.WEB_II,
            policy=SubstitutionPolicy.MEET_OR_BEAT,
            checked_attributes=[Attrs.WEB_STRENGTH, Attrs.WEB_RANGE],
        )
        allowed = self._resolve(item)
        self.assertIn(T.WEB_ABYSSAL, allowed.mutated_candidates)

        passed, checks = allowed.evaluate_mutated(
            T.WEB_ABYSSAL, {Attrs.WEB_STRENGTH: -62.5, Attrs.WEB_RANGE: 15000}
        )
        self.assertTrue(passed)
        self.assertEqual(len(checks), 2)

        passed, checks = allowed.evaluate_mutated(
            T.WEB_ABYSSAL, {Attrs.WEB_STRENGTH: -55, Attrs.WEB_RANGE: 15000}
        )
        self.assertFalse(passed)
        failed = [c for c in checks if not c.passed]
        self.assertEqual(failed[0].attribute_id, Attrs.WEB_STRENGTH)

    def test_meet_or_beat_allow_mutated_false(self):
        item = add_item(
            self.fit, Section.MED, T.WEB_II,
            policy=SubstitutionPolicy.MEET_OR_BEAT,
            allow_mutated=False,
        )
        allowed = self._resolve(item)
        self.assertEqual(allowed.mutated_candidates, {})

    def test_variants_never_includes_abyssal(self):
        item = add_item(
            self.fit, Section.MED, T.WEB_II, policy=SubstitutionPolicy.VARIANTS
        )
        allowed = self._resolve(item)
        self.assertNotIn(T.WEB_ABYSSAL, allowed.substitutes)
        self.assertEqual(allowed.mutated_candidates, {})


class TestPossibleMetaGroups(TestCase):
    """possible_meta_groups_* report the meta groups that actually exist in an
    item's variant family - the only groups worth offering in the policy editor."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER)

    def _possible(self, section, type_id):
        return possible_meta_groups_for_item(add_item(self.fit, section, type_id))

    def test_module_family_offers_only_its_tiers(self):
        # Heat Sink family = Tech I (I/Basic) + Tech II + Faction (two navy hulls).
        got = self._possible(Section.LOW, T.HEAT_SINK_II)
        self.assertEqual(
            got,
            {EveMetaGroupId.TECH_I, EveMetaGroupId.TECH_II, EveMetaGroupId.FACTION},
        )
        self.assertNotIn(EveMetaGroupId.OFFICER, got)
        self.assertNotIn(EveMetaGroupId.DEADSPACE, got)

    def test_tech1_tech2_only_family(self):
        # Cap Recharger family is Tech I + Tech II only (the rig-like case).
        self.assertEqual(
            self._possible(Section.MED, T.CAP_RECHARGER_II),
            {EveMetaGroupId.TECH_I, EveMetaGroupId.TECH_II},
        )

    def test_ammo_family_has_no_officer(self):
        # Charge family = Tech I base + Faction navy charge; never Officer/Deadspace.
        got = self._possible(Section.CARGO, T.MULTIFREQ_L_NAVY)
        self.assertEqual(got, {EveMetaGroupId.TECH_I, EveMetaGroupId.FACTION})
        self.assertNotIn(EveMetaGroupId.OFFICER, got)

    def test_structure_family_offers_structure_tiers(self):
        self.assertEqual(
            self._possible(Section.RIG, T.STRUCTURE_RIG_II),
            {EveMetaGroupId.STRUCTURE_TECH_I, EveMetaGroupId.STRUCTURE_TECH_II},
        )

    def test_abyssal_group_excluded(self):
        # A family containing an abyssal member (meta group 15) must drop it -
        # abyssal is gated by allow_mutated, never a meta-group checkbox.
        for tid, mg in (
            (990001, EveMetaGroupId.TECH_I),
            (990002, EveMetaGroupId.TECH_II),
            (990003, EveMetaGroupId.ABYSSAL),
        ):
            SdeType.objects.create(
                type_id=tid, name=f"Foo {tid}", group_id=70,
                category_id=EveCategoryId.MODULE, variation_parent_type_id=990001,
                meta_group_id=mg, slot_kind=SlotKind.MED, published=True,
            )
        self.assertEqual(
            possible_meta_groups_bulk({990001})[990001],
            {EveMetaGroupId.TECH_I, EveMetaGroupId.TECH_II},
        )

    def test_bulk_returns_all_and_unknown_is_empty(self):
        result = possible_meta_groups_bulk({T.HEAT_SINK_II, T.WEB_II, 999999})
        self.assertEqual(
            result[T.HEAT_SINK_II],
            {EveMetaGroupId.TECH_I, EveMetaGroupId.TECH_II, EveMetaGroupId.FACTION},
        )
        self.assertEqual(
            result[T.WEB_II], {EveMetaGroupId.TECH_I, EveMetaGroupId.TECH_II}
        )
        self.assertEqual(result.get(999999, set()), set())
