"""Apply a CompliancePolicy's slot-group rules to a fitting's items in bulk,
plus the pre-built (seeded) policies that ship with the plugin."""

from __future__ import annotations

from django.db import transaction

from ..constants import LEEWAY_SECTIONS, Section
from ..models import CompliancePolicy, DoctrineFit
from ..models.doctrine import ENFORCEMENT_TO_POLICY, EnforcementMode

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
        "description": "Variant-family substitutes (same meta family); fuel passes at 66%.",
        "rules": _rules(
            module=(_ME, True, 100), drone=(_ME, True, 100), cargo=(_ME, True, 100),
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


@transaction.atomic
def apply_policy_to_fit(fit: DoctrineFit, policy: CompliancePolicy) -> int:
    """Overwrite per-module policies on every item the policy's slot rules cover.

    Returns the number of items updated. The caller is responsible for bumping
    the fit version and re-checking pending submissions.
    """
    updated = 0
    fit_fields = ["compliance_policy"]
    for rule in policy.rules.all():
        fields = {"policy": ENFORCEMENT_TO_POLICY[rule.enforcement]}
        if rule.enforcement == EnforcementMode.GTE:
            fields["allow_mutated"] = rule.allow_mutated
        if rule.section in LEEWAY_SECTIONS:
            fields["min_quantity_pct"] = rule.min_quantity_pct
        updated += fit.items.filter(section=rule.section).update(**fields)
        if rule.section == Section.CARGO:
            # The CARGO rule also governs the synthesized loaded-charge demand
            # (charges loaded in fitted modules with no explicit cargo line of
            # their own) - keep it in step with the rest of the cargo policy.
            fit.charge_policy = ENFORCEMENT_TO_POLICY[rule.enforcement]
            fit.charge_min_quantity_pct = rule.min_quantity_pct
            fit_fields += ["charge_policy", "charge_min_quantity_pct"]
    fit.compliance_policy = policy
    fit.save(update_fields=fit_fields)
    return updated
