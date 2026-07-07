"""Apply a CompliancePolicy's slot-group rules to a fitting's items in bulk,
plus the pre-built (seeded) policies that ship with the plugin."""

from __future__ import annotations

from django.db import transaction

from ..constants import LEEWAY_SECTIONS, Section
from ..models import CompliancePolicy, Doctrine, DoctrineFit, FitAssignment
from ..models.doctrine import ENFORCEMENT_TO_POLICY, EnforcementMode, SubstitutionPolicy

# Pre-built policies seeded by migration 0022 (is_builtin=True). Pure data so the
# migration can consume it with historical model classes. Each rule: (enforcement,
# allow_mutated, min_quantity_pct).
# Sections not listed for a policy are "not overridden". Module slot groups share
# one rule; consumable bays carry the qty% leeway.
_MODULE_SECTIONS = (
    Section.HIGH, Section.MED, Section.LOW, Section.RIG, Section.SUBSYSTEM,
)
_BAY_SECTIONS = (Section.DRONE_BAY, Section.FIGHTER_BAY)


def _rules(module, drone, cargo, fuel, booster) -> dict:
    """Build a section->rule map. Each arg is (enforcement, allow_mutated, qty%)."""
    out: dict[str, dict] = {}
    for sec in _MODULE_SECTIONS:
        out[sec] = _rule(*module)
    for sec in _BAY_SECTIONS:
        out[sec] = _rule(*drone)
    out[Section.CARGO] = _rule(*cargo)
    out[Section.FUEL_BAY] = _rule(*fuel)
    out[Section.BOOSTER] = _rule(*booster)
    return out


def _rule(enforcement, allow_mutated, qty) -> dict:
    return {
        "enforcement": enforcement,
        "allow_mutated": allow_mutated,
        "min_quantity_pct": qty,
    }


_EX, _ME, _GE, _AN = (
    EnforcementMode.EXACT, EnforcementMode.META, EnforcementMode.GTE, EnforcementMode.ANY,
)

BUILTIN_POLICIES: dict[str, dict] = {
    "Strict": {
        "description": "Exact module match across every slot group; full consumable quantities.",
        "rules": _rules(
            module=(_EX, True, 100), drone=(_EX, True, 100), cargo=(_EX, True, 100),
            fuel=(_EX, True, 100), booster=(_EX, True, 100),
        ),
    },
    "Standard": {
        "description": (
            "Variant-family substitutes (same meta family); cargo passes at 25%, "
            "fuel at 66%."
        ),
        "rules": _rules(
            module=(_ME, True, 100), drone=(_ME, True, 100), cargo=(_ME, True, 25),
            fuel=(_EX, True, 66), booster=(_ME, True, 100),
        ),
    },
    "Flexible": {
        "description": "Meet-or-beat (abyssal allowed); generous cargo/fuel/booster leeway.",
        "rules": _rules(
            module=(_GE, True, 100), drone=(_GE, True, 100), cargo=(_GE, True, 66),
            fuel=(_GE, True, 66), booster=(_GE, True, 66),
        ),
    },
    "No Enforcement": {
        "description": "No enforcement on any slot group - anything goes.",
        "rules": _rules(
            module=(_AN, True, 100), drone=(_AN, True, 100), cargo=(_AN, True, 100),
            fuel=(_AN, True, 100), booster=(_AN, True, 100),
        ),
    },
}


def seed_builtin_policies(CompliancePolicyModel, PolicySlotRuleModel) -> list[str]:
    """Idempotently create/refresh the pre-built policies. Takes model classes so
    the data migration can pass historical models via apps.get_model(). Returns
    the names actually created. A pre-existing same-named CUSTOM policy is left
    untouched (we don't hijack a manager's policy)."""
    created: list[str] = []
    for name, spec in BUILTIN_POLICIES.items():
        policy, was_created = CompliancePolicyModel.objects.get_or_create(
            name=name, defaults={"description": spec["description"], "is_builtin": True}
        )
        if not was_created and not policy.is_builtin:
            # A custom policy already owns this name - don't touch it.
            continue
        policy.is_builtin = True
        policy.description = spec["description"]
        policy.save()
        PolicySlotRuleModel.objects.filter(policy=policy).delete()
        for section, rule in spec["rules"].items():
            PolicySlotRuleModel.objects.create(policy=policy, section=section, **rule)
        if was_created:
            created.append(name)
    return created


def seed_fields_for_section(policy: CompliancePolicy | None, section: str) -> dict:
    """Policy fields for a newly-materialised item in `section`, mirroring the
    per-rule field selection `apply_policy_to_fit` uses so a freshly-imported
    module and a bulk-applied one end up configured the same way. Falls back to
    plain VARIANTS substitution when there's no policy yet (a standalone import
    with no preset chosen) or the policy carries no rule for this section."""
    if policy is not None:
        rule = policy.rules.filter(section=section).first()
        if rule is not None:
            fields = {"policy": ENFORCEMENT_TO_POLICY[rule.enforcement]}
            if rule.enforcement == EnforcementMode.GTE:
                fields["allow_mutated"] = rule.allow_mutated
            if section in LEEWAY_SECTIONS:
                fields["min_quantity_pct"] = rule.min_quantity_pct
            return fields
    return {"policy": SubstitutionPolicy.VARIANTS}


def _apply_policy_rules(items, policy: CompliancePolicy) -> tuple[int, dict | None]:
    """Write each of `policy`'s slot rules onto `items` (a DoctrineFitItem or
    AssignmentItemPolicy queryset/related manager), section by section.

    Returns the number of rows updated and, if the policy carries a CARGO
    rule, the `{"charge_policy", "charge_min_quantity_pct"}` fields it implies
    (None otherwise) - the CARGO rule also governs the synthesized
    loaded-charge demand, which lives on the parent fit/assignment rather than
    a per-item row, so the caller applies it there. Shared by
    `apply_policy_to_fit` and `apply_policy_to_assignment` so a bulk-applied
    fit and a bulk-applied assignment snapshot end up configured the same way.
    """
    updated = 0
    charge_fields: dict | None = None
    for rule in policy.rules.all():
        fields = {"policy": ENFORCEMENT_TO_POLICY[rule.enforcement]}
        if rule.enforcement == EnforcementMode.GTE:
            fields["allow_mutated"] = rule.allow_mutated
        if rule.section in LEEWAY_SECTIONS:
            fields["min_quantity_pct"] = rule.min_quantity_pct
        updated += items.filter(section=rule.section).update(**fields)
        if rule.section == Section.CARGO:
            charge_fields = {
                "charge_policy": ENFORCEMENT_TO_POLICY[rule.enforcement],
                "charge_min_quantity_pct": rule.min_quantity_pct,
            }
    return updated, charge_fields


@transaction.atomic
def apply_policy_to_fit(fit: DoctrineFit, policy: CompliancePolicy) -> int:
    """Overwrite per-module policies on every item the policy's slot rules cover.

    Returns the number of items updated. The caller is responsible for bumping
    the fit version and re-checking pending submissions.
    """
    updated, charge_fields = _apply_policy_rules(fit.items, policy)
    fit_fields = ["compliance_policy"]
    if charge_fields is not None:
        fit.charge_policy = charge_fields["charge_policy"]
        fit.charge_min_quantity_pct = charge_fields["charge_min_quantity_pct"]
        fit_fields += ["charge_policy", "charge_min_quantity_pct"]
    fit.compliance_policy = policy
    fit.save(update_fields=fit_fields)
    return updated


@transaction.atomic
def apply_policy_to_assignment(assignment: FitAssignment, policy: CompliancePolicy) -> int:
    """Overwrite one FitAssignment's per-item policy snapshot from `policy`'s
    slot rules - the per-(doctrine, fit) counterpart to `apply_policy_to_fit`.

    Returns the number of AssignmentItemPolicy rows updated. Does NOT bump the
    assignment's version - callers decide when dependent submissions should go
    stale (e.g. `apply_policy_to_doctrine` bumps once per assignment after its
    rules are applied; a future single-assignment apply endpoint would bump
    immediately, like `assignment_resync` does).
    """
    updated, charge_fields = _apply_policy_rules(assignment.item_policies, policy)
    if charge_fields is not None:
        assignment.charge_policy = charge_fields["charge_policy"]
        assignment.charge_min_quantity_pct = charge_fields["charge_min_quantity_pct"]
        assignment.save(update_fields=["charge_policy", "charge_min_quantity_pct"])
    return updated


@transaction.atomic
def apply_policy_to_doctrine(doctrine: Doctrine, policy: CompliancePolicy) -> tuple[int, int]:
    """Apply `policy` to every FitAssignment in `doctrine`, then record it as
    the doctrine's standing preset (new/re-synced assignments pick it up
    automatically - see services.assignments).

    This overwrites per-assignment customizations for the slot groups the
    policy covers; `AssignmentItemOverride` rows (substitution exceptions) are
    untouched, since they live independently of the policy fields. Each
    touched assignment's version is bumped, so its doctrine's pending
    submissions go stale and need a recheck.

    Returns (assignments touched, item rows updated).
    """
    assignments_touched = 0
    items_updated = 0
    for assignment in doctrine.assignments.all():
        items_updated += apply_policy_to_assignment(assignment, policy)
        assignment.bump_version()
        assignments_touched += 1
    doctrine.compliance_policy = policy
    doctrine.save(update_fields=["compliance_policy"])
    return assignments_touched, items_updated
