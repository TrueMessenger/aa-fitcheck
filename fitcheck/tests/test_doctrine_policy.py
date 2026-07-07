"""Tests for the doctrine-level policy preset: `Doctrine.compliance_policy`,
`services.policies.apply_policy_to_assignment` / `apply_policy_to_doctrine`,
the attach/resync auto-apply hooks in `services.assignments`, and the
`doctrine_apply_policy` confirm-then-apply view."""

from django.test import TestCase
from django.urls import reverse

from ..constants import Section
from ..models import AssignmentItemOverride, CompliancePolicy, PolicySlotRule
from ..models.doctrine import EnforcementMode, SubstitutionPolicy
from ..services.assignments import attach_fit_to_doctrine, resync_assignment_from_source
from ..services.policies import apply_policy_to_assignment, apply_policy_to_doctrine
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


def _make_policy(name: str) -> CompliancePolicy:
    """A policy with a LOW rule (EXACT) and a CARGO rule (GTE, 50%), the two
    slot groups this module's fits use."""
    policy = CompliancePolicy.objects.create(name=name)
    PolicySlotRule.objects.create(
        policy=policy, section=Section.LOW, enforcement=EnforcementMode.EXACT,
    )
    PolicySlotRule.objects.create(
        policy=policy,
        section=Section.CARGO,
        enforcement=EnforcementMode.GTE,
        min_quantity_pct=50,
    )
    return policy


class TestApplyPolicyToDoctrine(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.manager = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.doctrine = create_doctrine("Avatar")
        self.other = create_doctrine("Other")
        self.policy = _make_policy("Bulk Preset")

        self.fit1 = create_fit(None, T.HARBINGER, name="Brawl One")
        add_item(self.fit1, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.VARIANTS)
        add_item(self.fit1, Section.CARGO, T.NANITE_PASTE, 10, policy=SubstitutionPolicy.EXACT)
        self.fit2 = create_fit(None, T.HARBINGER, name="Brawl Two")
        add_item(self.fit2, Section.LOW, T.HEAT_SINK_II, 2, policy=SubstitutionPolicy.VARIANTS)

        self.assignment1 = attach_fit_to_doctrine(self.fit1, self.doctrine, user=self.manager)
        self.assignment2 = attach_fit_to_doctrine(self.fit2, self.doctrine, user=self.manager)
        # A second doctrine's assignment for fit1 - must stay untouched.
        self.other_assignment = attach_fit_to_doctrine(self.fit1, self.other, user=self.manager)

        # An override on assignment1's LOW item - must survive the bulk apply.
        low_policy = self.assignment1.item_policies.get(section=Section.LOW)
        AssignmentItemOverride.objects.create(
            assignment_item=low_policy,
            alt_type_id=T.HEAT_SINK_IMPERIAL,
            mode=AssignmentItemOverride.Mode.INCLUDE,
        )

    def test_applies_rules_to_every_assignment_in_the_doctrine(self):
        assignments_touched, items_updated = apply_policy_to_doctrine(self.doctrine, self.policy)

        self.assertEqual(assignments_touched, 2)
        self.assertEqual(items_updated, 3)  # 2x LOW + 1x CARGO

        low1 = self.assignment1.item_policies.get(section=Section.LOW)
        self.assertEqual(low1.policy, SubstitutionPolicy.EXACT)
        cargo1 = self.assignment1.item_policies.get(section=Section.CARGO)
        self.assertEqual(cargo1.policy, SubstitutionPolicy.MEET_OR_BEAT)
        low2 = self.assignment2.item_policies.get(section=Section.LOW)
        self.assertEqual(low2.policy, SubstitutionPolicy.EXACT)

    def test_sets_charge_policy_pair_from_the_cargo_rule(self):
        apply_policy_to_doctrine(self.doctrine, self.policy)

        self.assignment1.refresh_from_db()
        self.assertEqual(self.assignment1.charge_policy, SubstitutionPolicy.MEET_OR_BEAT)
        self.assertEqual(self.assignment1.charge_min_quantity_pct, 50)
        self.assignment2.refresh_from_db()
        self.assertEqual(self.assignment2.charge_policy, SubstitutionPolicy.MEET_OR_BEAT)
        self.assertEqual(self.assignment2.charge_min_quantity_pct, 50)

    def test_bumps_every_touched_assignment_version(self):
        before1 = self.assignment1.version
        before2 = self.assignment2.version
        apply_policy_to_doctrine(self.doctrine, self.policy)
        self.assignment1.refresh_from_db()
        self.assignment2.refresh_from_db()
        self.assertGreater(self.assignment1.version, before1)
        self.assertGreater(self.assignment2.version, before2)

    def test_records_the_doctrine_standing_preset(self):
        apply_policy_to_doctrine(self.doctrine, self.policy)
        self.doctrine.refresh_from_db()
        self.assertEqual(self.doctrine.compliance_policy, self.policy)

    def test_override_rows_survive(self):
        apply_policy_to_doctrine(self.doctrine, self.policy)
        low1 = self.assignment1.item_policies.get(section=Section.LOW)
        self.assertTrue(
            AssignmentItemOverride.objects.filter(assignment_item=low1).exists()
        )

    def test_other_doctrines_assignments_are_untouched(self):
        before_version = self.other_assignment.version
        # Give the other assignment's LOW item a distinct policy to prove it
        # doesn't get folded into the "Bulk Preset" rules.
        other_low = self.other_assignment.item_policies.get(section=Section.LOW)
        other_low.policy = SubstitutionPolicy.ANY
        other_low.save(update_fields=["policy"])

        apply_policy_to_doctrine(self.doctrine, self.policy)

        self.other_assignment.refresh_from_db()
        other_low.refresh_from_db()
        self.assertEqual(self.other_assignment.version, before_version)
        self.assertEqual(other_low.policy, SubstitutionPolicy.ANY)
        self.assertIsNone(self.other.compliance_policy_id)


class TestApplyPolicyToAssignment(TestCase):
    """The single-assignment helper apply_policy_to_doctrine builds on -
    doesn't bump the version itself (callers decide)."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.manager = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.doctrine = create_doctrine("Avatar")
        self.policy = _make_policy("Solo Preset")
        self.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(self.fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.VARIANTS)
        self.assignment = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.manager)

    def test_does_not_bump_version(self):
        before = self.assignment.version
        apply_policy_to_assignment(self.assignment, self.policy)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.version, before)

    def test_returns_updated_row_count(self):
        updated = apply_policy_to_assignment(self.assignment, self.policy)
        self.assertEqual(updated, 1)


class TestAttachAppliesDoctrinePreset(TestCase):
    """A fresh attachment picks up the doctrine's standing preset even when
    the source fit's own defaults differ."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.manager = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.policy = _make_policy("Standing Preset")
        self.doctrine = create_doctrine("Avatar", compliance_policy=self.policy)
        self.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(self.fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.VARIANTS)

    def test_fresh_snapshot_carries_the_doctrine_preset(self):
        assignment = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.manager)

        snapshot = assignment.item_policies.get(section=Section.LOW)
        self.assertEqual(snapshot.policy, SubstitutionPolicy.EXACT)  # from the preset
        # Source fit's own default is untouched - it stays VARIANTS.
        self.assertEqual(self.fit.items.get(section=Section.LOW).policy, SubstitutionPolicy.VARIANTS)

    def test_fresh_attach_does_not_bump_version(self):
        assignment = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.manager)
        self.assertEqual(assignment.version, 0)

    def test_no_doctrine_preset_leaves_source_defaults(self):
        plain_doctrine = create_doctrine("Plain")
        assignment = attach_fit_to_doctrine(self.fit, plain_doctrine, user=self.manager)
        snapshot = assignment.item_policies.get(section=Section.LOW)
        self.assertEqual(snapshot.policy, SubstitutionPolicy.VARIANTS)

    def test_idempotent_reattach_does_not_reapply_preset(self):
        assignment = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.manager)
        snapshot = assignment.item_policies.get(section=Section.LOW)
        snapshot.policy = SubstitutionPolicy.ANY
        snapshot.save(update_fields=["policy"])

        again = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.manager)

        self.assertEqual(again.pk, assignment.pk)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.policy, SubstitutionPolicy.ANY)  # left alone


class TestResyncReappliesDoctrinePreset(TestCase):
    """Re-syncing an assignment re-clones the source template, then
    re-overlays the doctrine's standing preset on top."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.manager = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.policy = _make_policy("Standing Preset")
        self.doctrine = create_doctrine("Avatar", compliance_policy=self.policy)
        self.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(self.fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.VARIANTS)
        self.assignment = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.manager)

    def test_resync_reapplies_the_preset_over_the_source_template(self):
        # Drift the snapshot away from the preset.
        snapshot = self.assignment.item_policies.get(section=Section.LOW)
        snapshot.policy = SubstitutionPolicy.ANY
        snapshot.save(update_fields=["policy"])

        resync_assignment_from_source(self.assignment)

        synced = self.assignment.item_policies.get(section=Section.LOW)
        # Cloning alone would restore VARIANTS (the source default); the
        # doctrine's preset (EXACT) must win as the final word.
        self.assertEqual(synced.policy, SubstitutionPolicy.EXACT)

    def test_resync_without_a_doctrine_preset_keeps_source_defaults(self):
        plain_doctrine = create_doctrine("Plain")
        assignment = attach_fit_to_doctrine(self.fit, plain_doctrine, user=self.manager)
        resync_assignment_from_source(assignment)
        synced = assignment.item_policies.get(section=Section.LOW)
        self.assertEqual(synced.policy, SubstitutionPolicy.VARIANTS)


class TestDoctrineApplyPolicyView(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.manager = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.doctrine = create_doctrine("Avatar")
        self.policy = _make_policy("View Preset")
        self.fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(self.fit, Section.LOW, T.HEAT_SINK_II, 3, policy=SubstitutionPolicy.VARIANTS)
        self.assignment = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.manager)
        self.url = reverse("fitcheck:doctrine_apply_policy", args=[self.doctrine.pk])

    def test_get_shows_confirmation_with_count_and_fit_names(self):
        self.client.force_login(self.manager)
        response = self.client.get(self.url, {"policy": self.policy.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Brawl")
        self.assertContains(response, "View Preset")
        self.assertContains(response, "1 fitting will be affected")

    def test_get_without_policy_redirects_with_error(self):
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertRedirects(
            response, reverse("fitcheck:doctrine_detail", args=[self.doctrine.pk])
        )

    def test_get_with_disabled_policy_is_rejected(self):
        from django.utils import timezone

        self.policy.disabled_at = timezone.now()
        self.policy.save(update_fields=["disabled_at"])
        self.client.force_login(self.manager)
        response = self.client.get(self.url, {"policy": self.policy.pk})
        self.assertRedirects(
            response, reverse("fitcheck:doctrine_detail", args=[self.doctrine.pk])
        )

    def test_post_applies_and_redirects(self):
        self.client.force_login(self.manager)
        response = self.client.post(self.url, {"policy": self.policy.pk})
        self.assertRedirects(
            response, reverse("fitcheck:doctrine_detail", args=[self.doctrine.pk])
        )
        self.doctrine.refresh_from_db()
        self.assertEqual(self.doctrine.compliance_policy, self.policy)
        snapshot = self.assignment.item_policies.get(section=Section.LOW)
        self.assertEqual(snapshot.policy, SubstitutionPolicy.EXACT)

    def test_non_manager_is_redirected(self):
        member = create_user("member")
        self.client.force_login(member)
        response = self.client.get(self.url, {"policy": self.policy.pk})
        self.assertEqual(response.status_code, 302)  # @permission_required -> login
