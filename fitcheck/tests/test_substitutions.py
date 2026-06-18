from django.test import TestCase
from eveuniverse.models import EveType

from ..constants import EveMetaGroupId, Section
from ..models import FitItemOverride
from ..models.doctrine import SubstitutionPolicy
from ..services.substitutions import resolve_allowed_bulk
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
