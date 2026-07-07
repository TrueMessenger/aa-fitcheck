"""Tests for the unified import-time policy preset flow (replaces the old
per-fit ``default_policy`` enum):

- ``FitImportForm``'s preset dropdown (built-ins + custom policies, initial
  resolves to the built-in "Standard").
- ``import_fit``'s ``policy`` argument, and the #98 fix it enables: importing
  straight into a doctrine must apply the chosen preset to the doctrine's
  ``AssignmentItemPolicy`` snapshot, not just to the standalone fit afterwards.
- ``services.policies.seed_fields_for_section`` - new items (fresh import or a
  later BOM update) seed their policy fields from the fit's applied preset,
  falling back to plain VARIANTS substitution when there's none.
- The data migration that raises the built-in "Standard" preset's CARGO
  quantity leeway to 25%.
"""

from django.test import TestCase
from django.utils import timezone

from ..constants import Section
from ..forms import FitImportForm
from ..models import AssignmentItemPolicy, CompliancePolicy, PolicySlotRule
from ..models.doctrine import EnforcementMode, SubstitutionPolicy
from ..services.doctrine_import import import_fit
from ..services.fit_edit import update_fit_bom
from .testdata.factories import create_doctrine, create_user
from .testdata.sde_fixtures import T, create_sde_testdata

EFT = "[Harbinger, Brawler]\nHeat Sink II\nHeat Sink II\n"


class FitImportFormPresetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_dropdown_lists_builtins_and_custom_policies(self):
        custom = CompliancePolicy.objects.create(name="My Custom")
        names = set(FitImportForm().fields["policy"].queryset.values_list("name", flat=True))
        self.assertIn("Standard", names)
        self.assertIn(custom.name, names)

    def test_initial_resolves_to_the_builtin_standard_preset(self):
        standard = CompliancePolicy.objects.get(name="Standard", is_builtin=True)
        initial = FitImportForm().fields["policy"].initial
        resolved = initial() if callable(initial) else initial
        self.assertEqual(resolved, standard.pk)

    def test_disabled_standard_is_not_offered_or_defaulted(self):
        """A missing/disabled 'Standard' preset (e.g. a manager disabled it,
        or a fresh DB without the builtin seeded yet) must not crash the
        form - the initial callable just resolves to None."""
        standard = CompliancePolicy.objects.get(name="Standard", is_builtin=True)
        standard.disabled_at = timezone.now()
        standard.save(update_fields=["disabled_at"])

        form = FitImportForm()
        names = set(form.fields["policy"].queryset.values_list("name", flat=True))
        self.assertNotIn("Standard", names)
        initial = form.fields["policy"].initial
        resolved = initial() if callable(initial) else initial
        self.assertIsNone(resolved)


class ImportFitPolicyRegressionTests(TestCase):
    """Regression for #98: importing an EFT paste directly into a doctrine
    must apply the chosen preset to the doctrine's snapshot."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        cls.doctrine = create_doctrine("Avatar")
        # Deliberately lenient - LOW slots need no substitution match at all -
        # so its effect on the snapshot is unmistakable versus the VARIANTS/
        # EXACT-ish defaults a plain seed would produce.
        cls.policy = CompliancePolicy.objects.create(name="Lenient", created_by=cls.user)
        PolicySlotRule.objects.create(
            policy=cls.policy, section=Section.LOW, enforcement=EnforcementMode.ANY,
        )

    def test_doctrine_snapshot_carries_the_chosen_preset(self):
        fit = import_fit(
            EFT, self.user, doctrine=self.doctrine, name="Brawler", policy=self.policy,
        )
        snapshot = AssignmentItemPolicy.objects.get(
            assignment__fit=fit, assignment__doctrine=self.doctrine, section=Section.LOW,
        )
        self.assertEqual(snapshot.policy, SubstitutionPolicy.ANY)
        # The fit's own source item carries it too - apply_policy_to_fit ran
        # before attach_fit_to_doctrine, not after.
        source = fit.items.get(section=Section.LOW)
        self.assertEqual(source.policy, SubstitutionPolicy.ANY)
        self.assertEqual(fit.compliance_policy_id, self.policy.pk)

    def test_no_policy_arg_keeps_the_old_seed_behaviour(self):
        """Callers that don't offer a policy choice (the colcrunch importer)
        are unaffected - items seed VARIANTS and the snapshot mirrors that."""
        fit = import_fit(EFT, self.user, doctrine=self.doctrine, name="Baseline")
        snapshot = AssignmentItemPolicy.objects.get(
            assignment__fit=fit, assignment__doctrine=self.doctrine, section=Section.LOW,
        )
        self.assertEqual(snapshot.policy, SubstitutionPolicy.VARIANTS)
        self.assertIsNone(fit.compliance_policy_id)


class SeedFieldsForSectionTests(TestCase):
    """New-module seeding (services.policies.seed_fields_for_section, used by
    _materialise_items): a fit with an applied preset seeds new modules from
    it; a fit with none falls back to plain VARIANTS."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        cls.policy = CompliancePolicy.objects.create(name="Highs and Lows", created_by=cls.user)
        PolicySlotRule.objects.create(
            policy=cls.policy, section=Section.LOW, enforcement=EnforcementMode.EXACT,
        )
        PolicySlotRule.objects.create(
            policy=cls.policy, section=Section.HIGH, enforcement=EnforcementMode.ANY,
        )

    def test_fresh_import_with_policy_seeds_items_from_the_preset(self):
        fit = import_fit(EFT, self.user, name="Preset Seeded", policy=self.policy)
        item = fit.items.get(section=Section.LOW)
        self.assertEqual(item.policy, SubstitutionPolicy.EXACT)

    def test_fresh_import_without_policy_falls_back_to_variants(self):
        fit = import_fit(EFT, self.user, name="No Preset")
        item = fit.items.get(section=Section.LOW)
        self.assertEqual(item.policy, SubstitutionPolicy.VARIANTS)

    def test_bom_update_seeds_a_new_module_from_the_fits_applied_preset(self):
        fit = import_fit(EFT, self.user, name="BOM Preset", policy=self.policy)
        new_eft = (
            "[Harbinger, BOM Preset]\nHeat Sink II\nHeat Sink II\n"
            "Focused Medium Pulse Laser II\n"
        )
        update_fit_bom(fit, new_eft, self.user)
        new_module = fit.items.get(module_type_id=T.PULSE_LASER_II)
        self.assertEqual(new_module.section, Section.HIGH)
        self.assertEqual(new_module.policy, SubstitutionPolicy.ANY)

    def test_bom_update_new_module_falls_back_to_variants_with_no_preset(self):
        fit = import_fit(EFT, self.user, name="BOM No Preset")
        new_eft = (
            "[Harbinger, BOM No Preset]\nHeat Sink II\nHeat Sink II\n"
            "Focused Medium Pulse Laser II\n"
        )
        update_fit_bom(fit, new_eft, self.user)
        new_module = fit.items.get(module_type_id=T.PULSE_LASER_II)
        self.assertEqual(new_module.policy, SubstitutionPolicy.VARIANTS)


class BuiltinStandardCargoPctMigrationTests(TestCase):
    """Data migration 0035: the built-in "Standard" preset's CARGO rule
    should land at 25% quantity leeway (up from the original 100%)."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_standard_cargo_pct_is_25_after_migrations(self):
        standard = CompliancePolicy.objects.get(name="Standard", is_builtin=True)
        rule = standard.rules.get(section=Section.CARGO)
        self.assertEqual(rule.min_quantity_pct, 25)
