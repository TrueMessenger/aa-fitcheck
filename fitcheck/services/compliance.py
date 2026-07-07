"""The compliance engine: grade a ParsedFit against a DoctrineFit.

Pure logic - no database writes. Matching strategy per slot section:
pass 1 consumes exact matches, pass 2 assigns substitutes via maximum bipartite
matching (greedy mishandles overlapping substitute sets), pass 3 explains all
leftovers. Bay/cargo sections check "at least N" with per-item leeway; loaded
charges pool into cargo on both sides so "4 guns need 4 crystals" works whether
the crystals sit in the guns or the hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..constants import QUANTITY_SECTIONS, SECTION_ORDER, SLOT_SECTIONS, Section
from ..models import ComplianceFinding, DoctrineFit, EnforcementSettings, FitSubmission, SdeType
from ..models.doctrine import SubstitutionPolicy
from ..models.settings import VerificationMode
from .fit_data import FitItem, ParsedFit
from .substitutions import AllowedSet, resolve_allowed_bulk

Code = ComplianceFinding.Code
Verdict = FitSubmission.Verdict

HARD_FAIL_CODES = frozenset(
    {Code.WRONG_HULL, Code.MISSING, Code.NOT_ALLOWED, Code.QTY_SHORT, Code.IMPLANT_MISSING}
)


def _shortfall_code(mode: str) -> str:
    """A quantity/presence shortfall is a hard QTY_SHORT under REJECT/POLICY, a
    warning under WARN. (IGNORE is handled by skipping the concern upstream.)"""
    return (
        Code.QTY_SHORT
        if mode in (VerificationMode.REJECT, VerificationMode.POLICY)
        else Code.UNVERIFIED
    )


@dataclass
class Finding:
    code: str
    section: str = ""
    expected_type_id: int | None = None
    actual_type_id: int | None = None
    expected_qty: int | None = None
    actual_qty: int | None = None
    message: str = ""
    allowed_alternatives: list[dict] = field(default_factory=list)
    attribute_results: list[dict] = field(default_factory=list)


@dataclass
class CheckResult:
    verdict: str
    findings: list[Finding]


@dataclass
class _PilotUnit:
    type_id: int
    mutated_attributes: dict[int, float] | None = None
    consumed: bool = False
    # A courtesy clone deposited into fitted_extra_pool when a No-Enforcement
    # slot accepted this module: claimable by the cargo FITTED_REFIT fallback,
    # but excluded from the final EXTRA sweep when nothing claims it (the slot
    # already passed it as NO_ENFORCEMENT/OK).
    from_no_enforcement: bool = False
    # Mirrors FitItem.mutation_capped: True when this abyssal module's roll
    # lookup was skipped by the per-ship cap, so mutated_attributes is None
    # for a reason other than "no rolls provided".
    mutation_capped: bool = False


@dataclass
class _DoctrineDemand:
    item_pk: int
    type_id: int
    allowed: AllowedSet
    remaining: int


class _NameMap:
    def __init__(self, type_ids: set[int]):
        self._names = dict(
            SdeType.objects.filter(type_id__in=type_ids).values_list("type_id", "name")
        )

    def __call__(self, type_id: int | None) -> str:
        if type_id is None:
            return "?"
        return self._names.get(type_id, f"Type {type_id}")


def _max_bipartite_match(
    left_count: int, right_count: int, edges: dict[int, list[int]]
) -> dict[int, int]:
    """Kuhn's augmenting-path matching. Returns {left_index: right_index}."""
    match_right: dict[int, int] = {}

    def try_assign(u: int, visited: set[int]) -> bool:
        for v in edges.get(u, []):
            if v in visited:
                continue
            visited.add(v)
            if v not in match_right or try_assign(match_right[v], visited):
                match_right[v] = u
                return True
        return False

    for u in range(left_count):
        try_assign(u, set())
    return {u: v for v, u in match_right.items()}


def check_fit(
    parsed: ParsedFit,
    fit: DoctrineFit,
    *,
    doctrine_items=None,
    overrides_by_item=None,
) -> CheckResult:
    """Run the engine against the fit's source-level defaults, OR against a
    per-(doctrine, fit) assignment snapshot when the caller passes
    `doctrine_items` (an iterable of AssignmentItemPolicy) plus the matching
    `overrides_by_item`. Use `check_fit_for_doctrine()` for the assignment
    path - it computes both and calls this."""
    findings: list[Finding] = []

    for err in parsed.errors:
        findings.append(
            Finding(
                code=Code.UNRESOLVED,
                message=f"Line {err.line_no}: '{err.text}' - {err.reason}",
            )
        )
    if findings:
        return CheckResult(Verdict.ERROR, findings)

    if doctrine_items is None:
        doctrine_items = list(fit.items.all())
    else:
        doctrine_items = list(doctrine_items)
    allowed_sets = resolve_allowed_bulk(doctrine_items, overrides_by_item=overrides_by_item)

    involved_ids = {item.module_type_id for item in doctrine_items}
    involved_ids |= {i.type_id for i in parsed.items}
    involved_ids |= {i.charge_type_id for i in parsed.items if i.charge_type_id}
    involved_ids |= {i.charge_type_id for i in doctrine_items if i.charge_type_id}
    involved_ids.add(fit.ship_type_id)
    if parsed.ship_type_id:
        involved_ids.add(parsed.ship_type_id)
    involved_ids |= set(fit.feb_frigate_type_ids or [])
    if parsed.frigate_escape_bay_type_id:
        involved_ids.add(parsed.frigate_escape_bay_type_id)
    name_of = _NameMap(involved_ids)

    if parsed.ship_type_id != fit.ship_type_id:
        findings.append(
            Finding(
                code=Code.WRONG_HULL,
                expected_type_id=fit.ship_type_id,
                actual_type_id=parsed.ship_type_id,
                message=(
                    f"This doctrine fit is for a {name_of(fit.ship_type_id)}, "
                    f"but the submitted fit is a {name_of(parsed.ship_type_id)}."
                ),
            )
        )
        return CheckResult(Verdict.NON_COMPLIANT, findings)

    sde_meta = {
        row["type_id"]: row["meta_group_id"]
        for row in SdeType.objects.filter(
            type_id__in={i.type_id for i in parsed.items}
        ).values("type_id", "meta_group_id")
    }

    # Shared cargo pool: refit fallback (slot pass 2.5) and the CARGO quantity
    # section both consume from it. One physical module can only be in one place,
    # so consumption for refit reduces the cargo available to satisfy cargo
    # demands later - that's the intent.
    cargo_pool: dict[int, list[_PilotUnit]] = {}
    for fit_item in parsed.items_in(Section.CARGO):
        for _ in range(fit_item.quantity):
            cargo_pool.setdefault(fit_item.type_id, []).append(
                _PilotUnit(
                    fit_item.type_id, fit_item.mutated_attributes,
                    mutation_capped=fit_item.mutation_capped,
                )
            )

    # Fitted-extra pool: would-be-EXTRA slot units (no matching doctrine demand
    # in their own section) are collected here instead of emitted as EXTRA.
    # _check_quantity_sections() can then consume from it for the cargo-demand
    # fallback (req 2: pilot fitted DLA instead of carrying it). Whatever's
    # left after cargo runs becomes EXTRA in a final pass below.
    fitted_extra_pool: dict[int, list[_PilotUnit]] = {}
    extra_sections: dict[int, str] = {}  # type_id -> first slot section it appeared in

    settings = EnforcementSettings.current()

    for section in SLOT_SECTIONS:
        findings.extend(
            _check_slot_section(
                section, parsed, doctrine_items, allowed_sets, name_of,
                cargo_pool, fitted_extra_pool, extra_sections,
            )
        )
    findings.extend(
        _check_quantity_sections(
            parsed, doctrine_items, allowed_sets, name_of,
            cargo_pool, fitted_extra_pool, fuel_mode=settings.fuel_mode,
            extra_sections=extra_sections,
        )
    )
    findings.extend(
        _check_implants(
            parsed, doctrine_items, allowed_sets, name_of, settings.implant_mode, cargo_pool
        )
    )
    findings.extend(
        _check_boosters(
            parsed, doctrine_items, allowed_sets, name_of, settings.booster_mode, cargo_pool
        )
    )
    findings.extend(_check_feb(parsed, fit, name_of, settings.feb_mode))

    # Final EXTRA pass: anything left in the fitted-extra pool that the cargo
    # section did NOT claim as a fitted refit really is foreign to the fit.
    for type_id, units in fitted_extra_pool.items():
        unclaimed = [u for u in units if not u.consumed and not u.from_no_enforcement]
        if not unclaimed:
            continue
        findings.append(
            Finding(
                code=Code.EXTRA,
                section=extra_sections.get(type_id, ""),
                actual_type_id=type_id,
                actual_qty=len(unclaimed),
                message=f"{len(unclaimed)}x {name_of(type_id)} is not part of this doctrine fit.",
            )
        )

    findings.sort(key=lambda f: (SECTION_ORDER.get(f.section, 99), f.code))

    hard_fail = any(f.code in HARD_FAIL_CODES for f in findings)
    if not hard_fail and fit.strict_extras:
        hard_fail = any(
            f.code == Code.EXTRA and f.section in SLOT_SECTIONS for f in findings
        )
    if hard_fail:
        verdict = Verdict.NON_COMPLIANT
    elif any(
        f.code in (Code.SUBSTITUTE, Code.CARGO_REFIT, Code.FITTED_REFIT)
        for f in findings
    ):
        verdict = Verdict.COMPLIANT_SUBS
    else:
        verdict = Verdict.COMPLIANT
    return CheckResult(verdict, findings)


def check_fit_for_doctrine(
    parsed: ParsedFit, fit: DoctrineFit, doctrine
) -> CheckResult:
    """Run the engine against the per-(doctrine, fit) policy snapshot.

    Falls back to the source-level defaults (i.e. plain check_fit) when no
    assignment exists - that preserves the legacy behaviour for fits that
    were never explicitly attached to a doctrine, and for the fit-only
    pilot inventory flow."""
    from collections import defaultdict
    from ..models import FitAssignment

    assignment = (
        FitAssignment.objects.filter(doctrine=doctrine, fit=fit)
        .prefetch_related("item_policies__overrides")
        .first()
    )
    if assignment is None:
        return check_fit(parsed, fit)
    items = list(
        assignment.item_policies.select_related("module_type", "charge_type").all()
    )
    overrides_by_item: dict[int, list] = defaultdict(list)
    for item in items:
        for override in item.overrides.all():
            overrides_by_item[item.pk].append(override)
    return check_fit(
        parsed, fit, doctrine_items=items, overrides_by_item=overrides_by_item
    )


def _offer_no_enforcement_refit(
    unit: _PilotUnit,
    section: str,
    fitted_extra_pool: dict[int, list[_PilotUnit]] | None,
    extra_sections: dict[int, str] | None,
) -> None:
    """A No-Enforcement slot requires nothing, so a module the pilot fitted there
    should still be available to satisfy an enforced cargo refit demand for the
    same type. Deposit a flagged clone into fitted_extra_pool (the original unit
    stays consumed so its NO_ENFORCEMENT/OK row renders and it's out of pass 3).
    The clone is claimable by the cargo FITTED_REFIT fallback and skipped by the
    final EXTRA sweep when unclaimed."""
    if fitted_extra_pool is None:
        return
    fitted_extra_pool.setdefault(unit.type_id, []).append(
        _PilotUnit(
            unit.type_id, unit.mutated_attributes, from_no_enforcement=True,
            mutation_capped=unit.mutation_capped,
        )
    )
    if extra_sections is not None and unit.type_id not in extra_sections:
        extra_sections[unit.type_id] = section


def _check_slot_section(
    section: str,
    parsed: ParsedFit,
    doctrine_items: list,
    allowed_sets: dict[int, AllowedSet],
    name_of: _NameMap,
    cargo_pool: dict[int, list[_PilotUnit]] | None = None,
    fitted_extra_pool: dict[int, list[_PilotUnit]] | None = None,
    extra_sections: dict[int, str] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    section_items = [i for i in doctrine_items if i.section == section]
    pilot_units: list[_PilotUnit] = []
    for fit_item in parsed.items_in(section):
        for _ in range(fit_item.quantity):
            pilot_units.append(
                _PilotUnit(
                    fit_item.type_id, fit_item.mutated_attributes,
                    mutation_capped=fit_item.mutation_capped,
                )
            )

    demands = [
        _DoctrineDemand(item.pk, item.module_type_id, allowed_sets[item.pk], item.quantity)
        for item in section_items
    ]

    # Pass 1: exact matches consume first.
    for demand in demands:
        taken = 0
        for unit in pilot_units:
            if demand.remaining == 0:
                break
            if not unit.consumed and unit.type_id == demand.type_id:
                unit.consumed = True
                demand.remaining -= 1
                taken += 1
                if demand.allowed.allow_any:
                    _offer_no_enforcement_refit(
                        unit, section, fitted_extra_pool, extra_sections
                    )
        if taken:
            findings.append(
                Finding(
                    code=Code.OK,
                    section=section,
                    expected_type_id=demand.type_id,
                    actual_type_id=demand.type_id,
                    expected_qty=taken,
                    actual_qty=taken,
                    message=f"{taken}x {name_of(demand.type_id)}: exact match.",
                )
            )

    # Pass 2: substitutes via maximum bipartite matching over remaining units/slots.
    open_units = [u for u in pilot_units if not u.consumed]
    open_slots: list[_DoctrineDemand] = []
    for demand in demands:
        open_slots.extend([demand] * demand.remaining)

    mutation_checks: dict[tuple[int, int], tuple[bool, list]] = {}

    def unit_fits(unit: _PilotUnit, demand: _DoctrineDemand) -> bool:
        if demand.allowed.allows_statically(unit.type_id) and unit.type_id != demand.type_id:
            return True
        if unit.type_id in demand.allowed.mutated_candidates:
            key = (id(unit), demand.item_pk)
            if key not in mutation_checks:
                mutation_checks[key] = demand.allowed.evaluate_mutated(
                    unit.type_id, unit.mutated_attributes
                )
            return mutation_checks[key][0]
        return False

    edges = {
        ui: [si for si, slot in enumerate(open_slots) if unit_fits(unit, slot)]
        for ui, unit in enumerate(open_units)
    }
    matching = _max_bipartite_match(len(open_units), len(open_slots), edges)

    sub_groups: dict[tuple[int, int], dict] = {}
    for ui, si in matching.items():
        unit, demand = open_units[ui], open_slots[si]
        unit.consumed = True
        demand.remaining -= 1
        if demand.allowed.allow_any:
            _offer_no_enforcement_refit(unit, section, fitted_extra_pool, extra_sections)
        key = (unit.type_id, demand.item_pk)
        group = sub_groups.setdefault(
            key,
            {
                "qty": 0,
                "demand": demand,
                "attribute_results": [
                    c.as_dict()
                    for c in mutation_checks.get((id(unit), demand.item_pk), (None, []))[1]
                ],
            },
        )
        group["qty"] += 1
    for (pilot_type_id, _item_pk), group in sub_groups.items():
        demand = group["demand"]
        if demand.allowed.allow_any:
            findings.append(
                Finding(
                    code=Code.NO_ENFORCEMENT,
                    section=section,
                    expected_type_id=demand.type_id,
                    actual_type_id=pilot_type_id,
                    expected_qty=group["qty"],
                    actual_qty=group["qty"],
                    message=(
                        f"{group['qty']}x {name_of(pilot_type_id)} accepted "
                        f"(no enforcement for this slot)."
                    ),
                )
            )
            continue
        findings.append(
            Finding(
                code=Code.SUBSTITUTE,
                section=section,
                expected_type_id=demand.type_id,
                actual_type_id=pilot_type_id,
                expected_qty=group["qty"],
                actual_qty=group["qty"],
                message=(
                    f"{group['qty']}x {name_of(pilot_type_id)} accepted as a substitute "
                    f"for {name_of(demand.type_id)}."
                ),
                attribute_results=group["attribute_results"],
            )
        )

    # Pass 2.5: refit fallback. A missing slot module can be satisfied by the
    # SAME module (or an allowed substitute) sitting in cargo. EVE doctrine fits
    # routinely carry spare modules to refit during gameplay; the engine should
    # acknowledge that instead of marking the slot MISSING. Cargo units consumed
    # here are NOT available to satisfy CARGO demands later (one physical module,
    # one place) - that's why we share cargo_pool with _check_quantity_sections.
    if cargo_pool:
        for demand in demands:
            if demand.allowed.allow_any or demand.remaining == 0:
                continue
            refit_groups: dict[int, int] = {}
            # Exact-type cargo first, then static substitutes, then mutated.
            candidate_type_ids = (
                [demand.type_id]
                + [tid for tid in demand.allowed.substitutes if tid != demand.type_id]
                + list(demand.allowed.mutated_candidates)
            )
            for cand_type_id in candidate_type_ids:
                if demand.remaining == 0:
                    break
                bucket = cargo_pool.get(cand_type_id)
                if not bucket:
                    continue
                for unit in bucket:
                    if demand.remaining == 0:
                        break
                    if unit.consumed:
                        continue
                    if cand_type_id == demand.type_id or demand.allowed.allows_statically(
                        cand_type_id
                    ):
                        unit.consumed = True
                        demand.remaining -= 1
                        refit_groups[cand_type_id] = refit_groups.get(cand_type_id, 0) + 1
                    elif cand_type_id in demand.allowed.mutated_candidates:
                        passed, _checks = demand.allowed.evaluate_mutated(
                            cand_type_id, unit.mutated_attributes
                        )
                        if passed:
                            unit.consumed = True
                            demand.remaining -= 1
                            refit_groups[cand_type_id] = refit_groups.get(cand_type_id, 0) + 1
            for cand_type_id, qty in refit_groups.items():
                if cand_type_id == demand.type_id:
                    message = (
                        f"{qty}x {name_of(cand_type_id)} carried in cargo as refit "
                        f"for the {section.lower()} slot."
                    )
                else:
                    message = (
                        f"{qty}x {name_of(cand_type_id)} carried in cargo as a "
                        f"refit substitute for {name_of(demand.type_id)}."
                    )
                findings.append(
                    Finding(
                        code=Code.CARGO_REFIT,
                        section=section,
                        expected_type_id=demand.type_id,
                        actual_type_id=cand_type_id,
                        expected_qty=qty,
                        actual_qty=qty,
                        message=message,
                    )
                )

    # Pass 3: leftovers on both sides. Items under "no enforcement" are never missing.
    for demand in demands:
        if demand.allowed.allow_any:
            continue
        if demand.remaining > 0:
            findings.append(
                Finding(
                    code=Code.MISSING,
                    section=section,
                    expected_type_id=demand.type_id,
                    expected_qty=demand.remaining,
                    actual_qty=0,
                    message=(
                        f"Missing {demand.remaining}x {name_of(demand.type_id)} "
                        f"(or an allowed substitute)."
                    ),
                    allowed_alternatives=demand.allowed.alternatives(),
                )
            )

    leftover_counts: dict[int, list[_PilotUnit]] = {}
    for unit in pilot_units:
        if not unit.consumed:
            leftover_counts.setdefault(unit.type_id, []).append(unit)
    for type_id, units in leftover_counts.items():
        # NOT_ALLOWED is for a leftover that was OFFERED against a demand still
        # needing units but didn't qualify (wrong meta group, mutated rolls too
        # poor). We therefore only relate to a demand with remaining > 0:
        #  - A leftover whose type IS a doctrine module is a SURPLUS of that exact
        #    module (excluded by the type_id guard) -> falls through to EXTRA.
        #  - A leftover that IS a valid substitute (e.g. Heat Sink II for an
        #    Imperial Navy Heat Sink slot) only stays unconsumed when the demand
        #    was already fully met in passes 1-2; that's a SURPLUS too, so with
        #    every family demand at remaining 0 it also falls through to EXTRA
        #    ("not part of the fit") instead of a misleading "not an allowed
        #    substitute" failure.
        related = next(
            (
                d
                for d in demands
                if type_id != d.type_id
                and d.remaining > 0
                and (
                    type_id in d.allowed.family_type_ids
                    or type_id in d.allowed.mutated_candidates
                )
            ),
            None,
        )
        if related is not None:
            attribute_results: list[dict] = []
            if type_id in related.allowed.mutated_candidates:
                passed, checks = related.allowed.evaluate_mutated(
                    type_id, units[0].mutated_attributes
                )
                attribute_results = [c.as_dict() for c in checks]
                if units[0].mutated_attributes is None and units[0].mutation_capped:
                    message = (
                        f"{name_of(type_id)} is mutated and its rolled stats were not "
                        "verified - the per-ship abyssal lookup cap was reached. Raise "
                        "'Abyssal lookups per ship' under Settings -> Scan & Result "
                        "Limits and re-check."
                    )
                elif units[0].mutated_attributes is None:
                    message = (
                        f"{name_of(type_id)} is mutated and no rolled stats were provided - "
                        "export the fit from Pyfa (with mutations) or enter the stats manually."
                    )
                else:
                    message = (
                        f"{name_of(type_id)} does not meet the required attributes of "
                        f"{name_of(related.type_id)}."
                    )
            else:
                message = (
                    f"{name_of(type_id)} is not an allowed substitute for "
                    f"{name_of(related.type_id)}."
                )
            findings.append(
                Finding(
                    code=Code.NOT_ALLOWED,
                    section=section,
                    expected_type_id=related.type_id,
                    actual_type_id=type_id,
                    actual_qty=len(units),
                    message=message,
                    allowed_alternatives=related.allowed.alternatives(),
                    attribute_results=attribute_results,
                )
            )
        else:
            # Would-be EXTRA: defer the finding. _check_quantity_sections may
            # still claim these units to satisfy a CARGO demand as fitted refit
            # (req 2). Whatever's not claimed becomes EXTRA in check_fit's final
            # pass. Fall back to inline emission if no pool was provided (older
            # callers, defensive only).
            if fitted_extra_pool is not None:
                bucket = fitted_extra_pool.setdefault(type_id, [])
                bucket.extend(units)
                if extra_sections is not None and type_id not in extra_sections:
                    extra_sections[type_id] = section
            else:
                findings.append(
                    Finding(
                        code=Code.EXTRA,
                        section=section,
                        actual_type_id=type_id,
                        actual_qty=len(units),
                        message=f"{len(units)}x {name_of(type_id)} is not part of this doctrine fit.",
                    )
                )
    return findings


def _pool_loaded_charges(items: list[FitItem]) -> dict[int, int]:
    """Charges loaded into slot modules, counted one per module unit."""
    pooled: dict[int, int] = {}
    for item in items:
        if item.section in SLOT_SECTIONS and item.charge_type_id:
            pooled[item.charge_type_id] = pooled.get(item.charge_type_id, 0) + item.quantity
    return pooled


def _check_quantity_sections(
    parsed: ParsedFit,
    doctrine_items: list,
    allowed_sets: dict[int, AllowedSet],
    name_of: _NameMap,
    cargo_pool: dict[int, list[_PilotUnit]] | None = None,
    fitted_extra_pool: dict[int, list[_PilotUnit]] | None = None,
    fuel_mode: str = VerificationMode.WARN,
    extra_sections: dict[int, str] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []

    pilot_charge_pool = _pool_loaded_charges(parsed.items)
    doctrine_charge_pool: dict[int, int] = {}
    for item in doctrine_items:
        if item.section in SLOT_SECTIONS and item.charge_type_id:
            doctrine_charge_pool[item.charge_type_id] = (
                doctrine_charge_pool.get(item.charge_type_id, 0) + item.quantity
            )

    for section in QUANTITY_SECTIONS:
        # Fuel bay shortfalls are governed by the site fuel mode; IGNORE skips it.
        if section == Section.FUEL_BAY and fuel_mode == VerificationMode.IGNORE:
            continue
        section_items = [i for i in doctrine_items if i.section == section]

        pilot_counts: dict[int, int] = {}
        pilot_mutated: dict[int, list[FitItem]] = {}
        if section == Section.CARGO and cargo_pool is not None:
            # Rebuild from the shared pool, skipping units consumed for refit.
            for type_id, units in cargo_pool.items():
                for unit in units:
                    if unit.consumed:
                        continue
                    if unit.mutated_attributes is not None:
                        pilot_mutated.setdefault(type_id, []).append(
                            FitItem(
                                section=Section.CARGO,
                                type_id=type_id,
                                quantity=1,
                                mutated_attributes=unit.mutated_attributes,
                            )
                        )
                    else:
                        pilot_counts[type_id] = pilot_counts.get(type_id, 0) + 1
        else:
            for fit_item in parsed.items_in(section):
                if fit_item.mutated_attributes is not None:
                    pilot_mutated.setdefault(fit_item.type_id, []).append(fit_item)
                else:
                    pilot_counts[fit_item.type_id] = (
                        pilot_counts.get(fit_item.type_id, 0) + fit_item.quantity
                    )
        if section == Section.CARGO:
            for type_id, qty in pilot_charge_pool.items():
                pilot_counts[type_id] = pilot_counts.get(type_id, 0) + qty

        demands: list[tuple] = []  # (item, demand_qty, threshold_qty)
        seen_doctrine_charge_types = set()
        for item in section_items:
            if item.policy == SubstitutionPolicy.ANY:
                continue  # no enforcement for this slot group
            demand_qty = item.quantity
            if section == Section.CARGO and item.module_type_id in doctrine_charge_pool:
                demand_qty += doctrine_charge_pool[item.module_type_id]
                seen_doctrine_charge_types.add(item.module_type_id)
            threshold = -(-demand_qty * item.min_quantity_pct // 100)
            demands.append((item, demand_qty, threshold))
        if section == Section.CARGO:
            # Loaded doctrine charges with no matching cargo line become synthetic
            # exact-match requirements (so "4 loaded crystals" still demand 4).
            for type_id, qty in doctrine_charge_pool.items():
                if type_id not in seen_doctrine_charge_types:
                    demands.append((None, qty, qty, type_id))

        for demand in demands:
            if len(demand) == 4:
                item, demand_qty, threshold, exact_type_id = None, demand[1], demand[2], demand[3]
                allowed = None
            else:
                item, demand_qty, threshold = demand
                exact_type_id = item.module_type_id
                allowed = allowed_sets[item.pk]

            matched = 0
            used_subs: dict[int, int] = {}
            available = pilot_counts.get(exact_type_id, 0)
            take = min(available, threshold)
            if take:
                pilot_counts[exact_type_id] = available - take
                matched += take

            if matched < threshold and allowed is not None:
                for sub_id in list(allowed.substitutes):
                    if matched >= threshold:
                        break
                    available = pilot_counts.get(sub_id, 0)
                    take = min(available, threshold - matched)
                    if take:
                        pilot_counts[sub_id] = available - take
                        matched += take
                        used_subs[sub_id] = used_subs.get(sub_id, 0) + take
                for mut_id, mut_items in pilot_mutated.items():
                    if matched >= threshold:
                        break
                    if mut_id not in allowed.mutated_candidates:
                        continue
                    for mut_item in mut_items:
                        passed, _checks = allowed.evaluate_mutated(
                            mut_id, mut_item.mutated_attributes
                        )
                        if passed:
                            matched += mut_item.quantity
                            used_subs[mut_id] = used_subs.get(mut_id, 0) + mut_item.quantity
                            mut_item.quantity = 0
                        if matched >= threshold:
                            break

            # Fitted-refit fallback: a module the pilot has FITTED instead of
            # carried can still satisfy this CARGO demand. The slot section
            # deposited would-be EXTRA units into fitted_extra_pool; consume
            # from it here in exact-then-substitutes-then-mutated order.
            used_fitted: dict[int, int] = {}
            if (
                matched < threshold
                and section == Section.CARGO
                and fitted_extra_pool is not None
                and allowed is not None
            ):
                candidate_ids = (
                    [exact_type_id]
                    + [tid for tid in allowed.substitutes if tid != exact_type_id]
                    + list(allowed.mutated_candidates)
                )
                for cand_id in candidate_ids:
                    if matched >= threshold:
                        break
                    bucket = fitted_extra_pool.get(cand_id) or []
                    for unit in bucket:
                        if matched >= threshold:
                            break
                        if unit.consumed:
                            continue
                        if cand_id == exact_type_id or allowed.allows_statically(cand_id):
                            unit.consumed = True
                            matched += 1
                            used_fitted[cand_id] = used_fitted.get(cand_id, 0) + 1
                        elif cand_id in allowed.mutated_candidates:
                            passed, _checks = allowed.evaluate_mutated(
                                cand_id, unit.mutated_attributes
                            )
                            if passed:
                                unit.consumed = True
                                matched += 1
                                used_fitted[cand_id] = used_fitted.get(cand_id, 0) + 1

            # Capital jump fuel counts whether it sits in the fuel bay, the cargo
            # hold, or the fleet/freight hangar (the latter two both map to
            # Section.CARGO). Draw any fuel-bay shortfall from the pilot's leftover
            # cargo as CARGO_REFIT (carried, not in the bay) - mirroring implants/
            # boosters carried in cargo; passes in every enforcement mode.
            carried_fuel: dict[int, int] = {}
            if matched < threshold and section == Section.FUEL_BAY and cargo_pool is not None:
                candidate_ids = [exact_type_id] + (
                    [t for t in allowed.substitutes if t != exact_type_id] if allowed else []
                )
                for cand_id in candidate_ids:
                    if matched >= threshold:
                        break
                    units = [
                        u for u in cargo_pool.get(cand_id, [])
                        if not u.consumed and u.mutated_attributes is None
                    ]
                    take = min(len(units), threshold - matched)
                    for u in units[:take]:
                        u.consumed = True
                    if take:
                        matched += take
                        carried_fuel[cand_id] = carried_fuel.get(cand_id, 0) + take

            for sub_id, qty in used_subs.items():
                findings.append(
                    Finding(
                        code=Code.SUBSTITUTE,
                        section=section,
                        expected_type_id=exact_type_id,
                        actual_type_id=sub_id,
                        expected_qty=qty,
                        actual_qty=qty,
                        message=(
                            f"{qty}x {name_of(sub_id)} accepted as a substitute for "
                            f"{name_of(exact_type_id)}."
                        ),
                    )
                )
            for cand_id, qty in used_fitted.items():
                if cand_id == exact_type_id:
                    message = (
                        f"{qty}x {name_of(cand_id)} fitted to the ship instead of "
                        "carried in cargo."
                    )
                else:
                    message = (
                        f"{qty}x {name_of(cand_id)} fitted instead of carrying "
                        f"{name_of(exact_type_id)}."
                    )
                # Show this under the slot the module is ACTUALLY fitted in (recorded
                # when it was deposited into fitted_extra_pool), not this CARGO demand's
                # section - a fitted module belongs in its slot panel, not in cargo.
                fitted_section = (
                    extra_sections.get(cand_id, section) if extra_sections else section
                )
                findings.append(
                    Finding(
                        code=Code.FITTED_REFIT,
                        section=fitted_section,
                        expected_type_id=exact_type_id,
                        actual_type_id=cand_id,
                        expected_qty=qty,
                        actual_qty=qty,
                        message=message,
                    )
                )

            for cand_id, qty in carried_fuel.items():
                if cand_id == exact_type_id:
                    message = (
                        f"{qty}x {name_of(cand_id)} carried in cargo or the fleet "
                        "hangar (not in the fuel bay)."
                    )
                else:
                    message = (
                        f"{qty}x {name_of(cand_id)} carried in cargo as a substitute "
                        f"for {name_of(exact_type_id)}."
                    )
                findings.append(
                    Finding(
                        code=Code.CARGO_REFIT,
                        section=Section.FUEL_BAY,
                        expected_type_id=exact_type_id,
                        actual_type_id=cand_id,
                        expected_qty=qty,
                        actual_qty=qty,
                        message=message,
                    )
                )

            if matched >= threshold:
                exact_qty = (
                    matched
                    - sum(used_subs.values())
                    - sum(used_fitted.values())
                    - sum(carried_fuel.values())
                )
                if exact_qty > 0:
                    findings.append(
                        Finding(
                            code=Code.OK,
                            section=section,
                            expected_type_id=exact_type_id,
                            actual_type_id=exact_type_id,
                            expected_qty=exact_qty,
                            actual_qty=exact_qty,
                            message=f"{exact_qty}x {name_of(exact_type_id)}: requirement met.",
                        )
                    )
            else:
                leeway_note = (
                    f" (doctrine lists {demand_qty}, leeway allows {threshold})"
                    if threshold != demand_qty
                    else ""
                )
                code = (
                    _shortfall_code(fuel_mode)
                    if section == Section.FUEL_BAY
                    else Code.QTY_SHORT
                )
                warn = code == Code.UNVERIFIED
                findings.append(
                    Finding(
                        code=code,
                        section=section,
                        expected_type_id=exact_type_id,
                        # Show what the pilot actually holds of the expected type
                        # (e.g. 21,483 of 50,000 fuel) instead of a blank cell.
                        actual_type_id=exact_type_id if matched else None,
                        expected_qty=threshold,
                        actual_qty=matched,
                        message=(
                            f"Need at least {threshold}x {name_of(exact_type_id)}"
                            f"{leeway_note}, found {matched}."
                            + (" (not enforced)" if warn else "")
                        ),
                        allowed_alternatives=allowed.alternatives() if allowed else [],
                    )
                )
        # Surface the pilot's leftover bay items - drones/fighters they carry that
        # the doctrine doesn't list - as EXTRA (warn), so they show in the "Your fit"
        # column instead of silently vanishing. CARGO is intentionally excluded:
        # it's a bulk hold, not a loadout slot, and would flood the table.
        if section in (Section.DRONE_BAY, Section.FIGHTER_BAY):
            leftovers: dict[int, int] = {
                type_id: qty for type_id, qty in pilot_counts.items() if qty > 0
            }
            for type_id, mut_items in pilot_mutated.items():
                remaining = sum(m.quantity for m in mut_items if m.quantity > 0)
                if remaining:
                    leftovers[type_id] = leftovers.get(type_id, 0) + remaining
            for type_id, qty in leftovers.items():
                findings.append(
                    Finding(
                        code=Code.EXTRA,
                        section=section,
                        actual_type_id=type_id,
                        actual_qty=qty,
                        message=f"{qty}x {name_of(type_id)} carried but not in the doctrine.",
                    )
                )
    return findings


def _take_from_cargo(cargo_pool, candidate_type_ids) -> int | None:
    """Find and consume the first unconsumed cargo/fleet-hangar unit matching any
    candidate type (the doctrine type first, then allowed substitutes). Returns
    the matched type_id, or None when nothing is carried. Implants/boosters a
    pilot hauls as spares land in Section.CARGO, so this lets them count as
    'Carried in cargo as refit' (a pass) when not plugged in / not slotted."""
    if cargo_pool is None:
        return None
    for type_id in candidate_type_ids:
        for unit in cargo_pool.get(type_id, []):
            if not unit.consumed:
                unit.consumed = True
                return type_id
    return None


def _carried_refit_finding(section, doctrine_type_id, carried_id, name_of) -> Finding:
    """A CARGO_REFIT ('Carried in cargo as refit') pass for an implant/booster
    found in the pilot's cargo or fleet hangar."""
    if carried_id == doctrine_type_id:
        message = f"{name_of(doctrine_type_id)} carried in cargo as refit."
    else:
        message = (
            f"{name_of(carried_id)} carried in cargo as a refit substitute for "
            f"{name_of(doctrine_type_id)}."
        )
    return Finding(
        code=Code.CARGO_REFIT,
        section=section,
        expected_type_id=doctrine_type_id,
        actual_type_id=carried_id,
        message=message,
    )


def _check_implants(
    parsed: ParsedFit,
    doctrine_items: list,
    allowed_sets: dict[int, AllowedSet],
    name_of: _NameMap,
    implant_mode: str = VerificationMode.POLICY,
    cargo_pool=None,
) -> list[Finding]:
    findings: list[Finding] = []
    if implant_mode == VerificationMode.IGNORE:
        return findings
    requirements = [
        i
        for i in doctrine_items
        if i.section == Section.IMPLANT and i.policy != SubstitutionPolicy.ANY
    ]
    if not requirements:
        return findings
    pilot_implants = parsed.pilot_implant_type_ids
    for item in requirements:
        allowed = allowed_sets[item.pk]
        matched_id = None
        if pilot_implants is not None:
            if item.module_type_id in pilot_implants:
                matched_id = item.module_type_id
            else:
                matched_id = next(
                    (s for s in allowed.substitutes if s in pilot_implants), None
                )
        if matched_id is not None:
            findings.append(
                Finding(
                    code=Code.OK,
                    section=Section.IMPLANT,
                    expected_type_id=item.module_type_id,
                    # Show the plugged implant in the "Your fit" column.
                    actual_type_id=matched_id,
                    message=f"Implant {name_of(item.module_type_id)}: present.",
                )
            )
            continue
        # Not plugged: a spare carried in cargo / fleet hangar still passes (REF).
        carried_id = _take_from_cargo(
            cargo_pool, [item.module_type_id, *allowed.substitutes]
        )
        if carried_id is not None:
            findings.append(
                _carried_refit_finding(
                    Section.IMPLANT, item.module_type_id, carried_id, name_of
                )
            )
            continue
        if pilot_implants is None:
            # Unverifiable (EFT paste / no clones scope). REJECT still fails; the
            # softer modes warn.
            findings.append(
                Finding(
                    code=(
                        Code.IMPLANT_MISSING
                        if implant_mode == VerificationMode.REJECT
                        else Code.UNVERIFIED
                    ),
                    section=Section.IMPLANT,
                    expected_type_id=item.module_type_id,
                    message=(
                        f"Required implant {name_of(item.module_type_id)} cannot be verified "
                        "from this submission (needs an ESI check with implant scopes)."
                    ),
                    allowed_alternatives=allowed.alternatives(),
                )
            )
        else:
            # Verifiably absent. WARN downgrades to a warning; REJECT/POLICY fail.
            findings.append(
                Finding(
                    code=(
                        Code.UNVERIFIED
                        if implant_mode == VerificationMode.WARN
                        else Code.IMPLANT_MISSING
                    ),
                    section=Section.IMPLANT,
                    expected_type_id=item.module_type_id,
                    message=(
                        f"Required implant {name_of(item.module_type_id)} is neither plugged "
                        "in nor in inventory."
                    ),
                    allowed_alternatives=allowed.alternatives(),
                )
            )
    return findings


def _check_boosters(
    parsed: ParsedFit,
    doctrine_items: list,
    allowed_sets: dict[int, AllowedSet],
    name_of: _NameMap,
    booster_mode: str = VerificationMode.WARN,
    cargo_pool=None,
) -> list[Finding]:
    """Boosters are consumables that neither EFT pastes nor ESI assets reliably
    report. The site booster_mode governs how a shortfall/absence is treated:
    WARN (default) keeps everything informational; REJECT/POLICY hard-fail a
    confirmed shortfall; IGNORE skips boosters entirely. An absent booster is
    unverifiable, so only REJECT ('must prove it') hard-fails on absence."""
    findings: list[Finding] = []
    if booster_mode == VerificationMode.IGNORE:
        return findings
    requirements = [
        i
        for i in doctrine_items
        if i.section == Section.BOOSTER and i.policy != SubstitutionPolicy.ANY
    ]
    if not requirements:
        return findings
    present_counts: dict[int, int] = {}
    for fit_item in parsed.items_in(Section.BOOSTER):
        present_counts[fit_item.type_id] = (
            present_counts.get(fit_item.type_id, 0) + fit_item.quantity
        )
    enforce = booster_mode in (VerificationMode.REJECT, VerificationMode.POLICY)
    for item in requirements:
        allowed = allowed_sets[item.pk]
        threshold = -(-item.quantity * item.min_quantity_pct // 100)
        present_qty = present_counts.get(item.module_type_id, 0) + sum(
            present_counts.get(sub_id, 0) for sub_id in allowed.substitutes
        )
        if present_qty >= threshold:
            findings.append(
                Finding(
                    code=Code.OK,
                    section=Section.BOOSTER,
                    expected_type_id=item.module_type_id,
                    # Show the carried booster in the "Your fit" column.
                    actual_type_id=item.module_type_id,
                    expected_qty=threshold,
                    actual_qty=present_qty,
                    message=f"Booster {name_of(item.module_type_id)}: present in the submitted fit.",
                )
            )
            continue
        # Active boosters can't be read from ESI, but a spare carried in cargo /
        # fleet hangar proves availability - any amount counts as a pass (REF).
        carried_id = _take_from_cargo(
            cargo_pool, [item.module_type_id, *allowed.substitutes]
        )
        if carried_id is not None:
            findings.append(
                _carried_refit_finding(
                    Section.BOOSTER, item.module_type_id, carried_id, name_of
                )
            )
            continue
        if present_qty > 0:
            # Confirmed shortfall: hard-fail under REJECT/POLICY, else warn.
            findings.append(
                Finding(
                    code=Code.QTY_SHORT if enforce else Code.UNVERIFIED,
                    section=Section.BOOSTER,
                    expected_type_id=item.module_type_id,
                    expected_qty=threshold,
                    actual_qty=present_qty,
                    message=(
                        f"Booster {name_of(item.module_type_id)}: found {present_qty}, "
                        f"doctrine wants {threshold}"
                        + ("." if enforce else " (not enforced).")
                    ),
                    allowed_alternatives=allowed.alternatives(),
                )
            )
        else:
            # Absent: unverifiable. Only REJECT ('must prove it') hard-fails.
            findings.append(
                Finding(
                    code=(
                        Code.QTY_SHORT
                        if booster_mode == VerificationMode.REJECT
                        else Code.UNVERIFIED
                    ),
                    section=Section.BOOSTER,
                    expected_type_id=item.module_type_id,
                    message=(
                        f"Required booster {name_of(item.module_type_id)} cannot be verified - "
                        "boosters are not reported by EFT pastes or ESI assets."
                    ),
                    allowed_alternatives=allowed.alternatives(),
                )
            )
    return findings


def _check_feb(parsed: ParsedFit, fit, name_of: _NameMap, feb_mode: str) -> list[Finding]:
    """Match the doctrine fit's accepted Frigate Escape Bay frigate(s) against the
    pilot's bay (ESI-sourced; None for EFT pastes). Gated by the site FEB mode;
    only runs when the doctrine names at least one FEB frigate. The bay passes if
    it holds ANY one of the accepted frigates."""
    if feb_mode == VerificationMode.IGNORE:
        return []
    expected_ids = list(fit.feb_frigate_type_ids or [])
    if not expected_ids:
        return []
    accepted = ", ".join(name_of(t) for t in expected_ids)
    actual = parsed.frigate_escape_bay_type_id
    if actual in expected_ids:
        return [
            Finding(
                code=Code.OK,
                section=Section.FEB,
                expected_type_id=actual,
                actual_type_id=actual,
                message=f"Frigate Escape Bay: {name_of(actual)} present.",
            )
        ]
    if actual is None:
        # Unverifiable (EFT paste / no ESI assets). Only REJECT fails.
        return [
            Finding(
                code=Code.MISSING if feb_mode == VerificationMode.REJECT else Code.UNVERIFIED,
                section=Section.FEB,
                expected_type_id=expected_ids[0],
                message=(
                    f"Frigate Escape Bay frigate ({accepted}) could not be verified "
                    "from this submission."
                ),
            )
        ]
    # Verifiably a frigate that isn't on the accepted list. WARN downgrades it.
    return [
        Finding(
            code=Code.UNVERIFIED if feb_mode == VerificationMode.WARN else Code.NOT_ALLOWED,
            section=Section.FEB,
            expected_type_id=expected_ids[0],
            actual_type_id=actual,
            message=(
                f"Frigate Escape Bay holds {name_of(actual)}; doctrine "
                f"expects {accepted}."
            ),
        )
    ]
