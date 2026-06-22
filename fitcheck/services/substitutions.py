"""Resolve the set of allowed substitute types for doctrine fit items.

Three policies:
- EXACT: only the doctrine type (plus explicit INCLUDE overrides).
- VARIANTS: the SDE variant family (Pyfa's "Variations"), filtered by meta
  level floor and allowed meta groups. Abyssal types never qualify here.
- MEET_OR_BEAT: family members and reachable abyssal types qualify when every
  checked attribute meets or beats the doctrine module's baseline value,
  honoring each attribute's ``high_is_good`` direction. Static values decide
  for normal types; per-item rolled values decide for mutated modules.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..constants import DEFAULT_EXCLUDED_CHECK_ATTRIBUTES, EveMetaGroupId
from ..models import (
    DoctrineFitItem,
    FitItemOverride,
    SdeAttribute,
    SdeMutaplasmidMapping,
    SdeType,
    SdeTypeAttribute,
)
from ..models.doctrine import SubstitutionPolicy


@dataclass
class AttributeCheck:
    attribute_id: int
    label: str
    required: float
    actual: float | None
    high_is_good: bool

    @property
    def passed(self) -> bool:
        if self.actual is None:
            return False
        return self.actual >= self.required if self.high_is_good else self.actual <= self.required

    def as_dict(self) -> dict:
        return {
            "attribute": self.label,
            "required": self.required,
            "actual": self.actual,
            "high_is_good": self.high_is_good,
            "passed": self.passed,
        }


@dataclass
class AllowedSet:
    """Pre-computed substitution data for one doctrine fit item."""

    item_id: int
    exact_type_id: int
    # No enforcement: anything in the slot satisfies this item, and the item
    # itself is never required.
    allow_any: bool = False
    # Every member of the variant family (incl. disallowed ones) - used to tell
    # "wrong variant" apart from "foreign module" in feedback.
    family_type_ids: set[int] = field(default_factory=set)
    # Static substitutes that already passed their policy check (type_id -> name).
    substitutes: dict[int, str] = field(default_factory=dict)
    # Abyssal types reachable via mutaplasmids; rolls are judged at check time.
    mutated_candidates: dict[int, str] = field(default_factory=dict)
    checked_attributes: list[int] = field(default_factory=list)
    baseline: dict[int, float] = field(default_factory=dict)
    attribute_labels: dict[int, str] = field(default_factory=dict)
    attribute_high_is_good: dict[int, bool] = field(default_factory=dict)
    # Optional per-attribute abyssal acceptance window {attr_id: {"min", "max"}};
    # the worst-side handle becomes the pass threshold (else the baseline).
    attribute_bounds: dict[int, dict] = field(default_factory=dict)
    # Static attribute values for mutated candidates (fallback for un-rolled attrs).
    candidate_static_values: dict[int, dict[int, float]] = field(default_factory=dict)

    def allows_statically(self, type_id: int) -> bool:
        if self.allow_any:
            return True
        return type_id == self.exact_type_id or type_id in self.substitutes

    def evaluate_mutated(
        self, type_id: int, rolled: dict[int, float] | None
    ) -> tuple[bool, list[AttributeCheck]]:
        """Judge a mutated module's rolled attributes against the baseline."""
        if type_id not in self.mutated_candidates:
            return False, []
        rolled = rolled or {}
        static = self.candidate_static_values.get(type_id, {})
        checks: list[AttributeCheck] = []
        for attr_id in self.checked_attributes:
            high = self.attribute_high_is_good.get(attr_id, True)
            bound = self.attribute_bounds.get(attr_id)
            if bound is not None:
                # The worst acceptable value the manager set: the low handle when
                # higher is better, the high handle when lower is better.
                required = bound.get("min") if high else bound.get("max")
            else:
                required = self.baseline.get(attr_id)
            if required is None:
                continue
            actual = rolled.get(attr_id, static.get(attr_id))
            checks.append(
                AttributeCheck(
                    attribute_id=attr_id,
                    label=self.attribute_labels.get(attr_id, str(attr_id)),
                    required=required,
                    actual=actual,
                    high_is_good=high,
                )
            )
        return bool(checks) and all(c.passed for c in checks), checks

    def alternatives(self, limit: int = 10) -> list[dict]:
        """Human-friendly list of allowed options for feedback messages."""
        result = [
            {"type_id": tid, "name": name}
            for tid, name in sorted(self.substitutes.items(), key=lambda kv: kv[1])
        ]
        return result[:limit]


def _meets_or_beats(
    candidate_values: dict[int, float],
    baseline: dict[int, float],
    checked: list[int],
    high_is_good: dict[int, bool],
) -> bool:
    for attr_id in checked:
        required = baseline.get(attr_id)
        if required is None:
            continue
        actual = candidate_values.get(attr_id)
        if actual is None:
            return False
        if high_is_good.get(attr_id, True):
            if actual < required:
                return False
        elif actual > required:
            return False
    return True


def resolve_allowed_bulk(
    items: list,
    overrides_by_item: dict | None = None,
) -> dict[int, AllowedSet]:
    """Resolve allowed substitutes for many doctrine items with a handful of queries.

    `items` may be DoctrineFitItem (legacy fit-defaults path) or
    AssignmentItemPolicy (per-(doctrine, fit) snapshot path). Both expose the
    same attribute surface: section/module_type_id/quantity/charge_type_id/
    policy/min_meta_level/allowed_meta_groups/checked_attributes/allow_mutated/
    min_quantity_pct/notes, plus a unique `.pk`.

    `overrides_by_item`, if provided, replaces the FitItemOverride lookup -
    callers using AssignmentItemPolicy pre-build it from AssignmentItemOverride.
    """
    if not items:
        return {}

    module_type_ids = {item.module_type_id for item in items}
    sde_types = SdeType.objects.in_bulk(module_type_ids)

    parent_ids = set()
    for type_id in module_type_ids:
        sde = sde_types.get(type_id)
        if sde:
            parent_ids.add(sde.variation_parent_type_id or sde.type_id)

    family_rows = list(
        SdeType.objects.filter(
            variation_parent_type_id__in=parent_ids, published=True
        ).values("type_id", "name", "variation_parent_type_id", "meta_group_id", "meta_level")
    )
    families: dict[int, list[dict]] = defaultdict(list)
    for row in family_rows:
        families[row["variation_parent_type_id"]].append(row)

    family_type_ids = {row["type_id"] for row in family_rows}

    # Mutaplasmid candidates reachable from any family member.
    mb_items = [item for item in items if item.policy == SubstitutionPolicy.MEET_OR_BEAT]
    mutation_rows = []
    mutable_attr_ids: dict[int, set[int]] = defaultdict(set)  # source family parent -> attrs
    abyssal_by_source: dict[int, list[tuple[int, str]]] = defaultdict(list)
    if mb_items and family_type_ids:
        mutation_rows = list(
            SdeMutaplasmidMapping.objects.filter(
                source_type_id__in=family_type_ids
            ).select_related("abyssal_type")
        )
        for mapping in mutation_rows:
            abyssal_by_source[mapping.source_type_id].append(
                (mapping.abyssal_type_id, mapping.abyssal_type.name)
            )
            for spec in mapping.mutable_attributes:
                attr_id = spec.get("attr_id")
                if attr_id:
                    mutable_attr_ids[mapping.source_type_id].add(int(attr_id))

    # Static attribute values for everything we may need to compare.
    attr_value_type_ids = set()
    if mb_items:
        attr_value_type_ids |= family_type_ids
        attr_value_type_ids |= {a_id for pairs in abyssal_by_source.values() for a_id, _ in pairs}
    values_by_type: dict[int, dict[int, float]] = defaultdict(dict)
    if attr_value_type_ids:
        for type_id, attr_id, value in SdeTypeAttribute.objects.filter(
            eve_type_id__in=attr_value_type_ids
        ).values_list("eve_type_id", "attribute_id", "value"):
            values_by_type[type_id][attr_id] = value

    # Overrides, bulk. Callers may pre-build this dict (the assignment path
    # does, with AssignmentItemOverride rows in place of FitItemOverride).
    if overrides_by_item is None:
        overrides_by_item = defaultdict(list)
        override_rows = FitItemOverride.objects.filter(
            item_id__in=[item.pk for item in items]
        ).select_related("alt_type")
        for override in override_rows:
            overrides_by_item[override.item_id].append(override)
    else:
        # Flatten to a plain dict in case the caller handed in a defaultdict.
        overrides_by_item = dict(overrides_by_item)
    override_type_ids = {
        o.alt_type_id for rows in overrides_by_item.values() for o in rows
    }
    override_sde = SdeType.objects.in_bulk(override_type_ids) if override_type_ids else {}

    attribute_meta = {
        row["attribute_id"]: (row["display_name"] or row["name"], row["high_is_good"])
        for row in SdeAttribute.objects.values("attribute_id", "display_name", "name", "high_is_good")
    }

    result: dict[int, AllowedSet] = {}
    for item in items:
        sde = sde_types.get(item.module_type_id)
        allowed = AllowedSet(item_id=item.pk, exact_type_id=item.module_type_id)
        if item.policy == SubstitutionPolicy.ANY:
            allowed.allow_any = True
            result[item.pk] = allowed
            continue
        if sde is None:
            result[item.pk] = allowed
            continue

        parent_id = sde.variation_parent_type_id or sde.type_id
        family = families.get(parent_id, [])
        allowed.family_type_ids = {row["type_id"] for row in family}
        baseline_values = values_by_type.get(sde.type_id, {})
        # Reversed meta-group semantics: a candidate's meta group must be checked
        # to qualify; an empty set allows no family substitutes. Applies to both
        # VARIANTS and MEET_OR_BEAT. (Abyssal candidates are gated separately by
        # allow_mutated, not by this filter.)
        allowed_groups = {int(g) for g in (item.allowed_meta_groups or [])}

        if item.policy == SubstitutionPolicy.VARIANTS:
            for row in family:
                if row["type_id"] == sde.type_id:
                    continue
                if row["meta_group_id"] == EveMetaGroupId.ABYSSAL:
                    continue
                if row["meta_group_id"] not in allowed_groups:
                    continue
                allowed.substitutes[row["type_id"]] = row["name"]

        elif item.policy == SubstitutionPolicy.MEET_OR_BEAT:
            checked = [int(a) for a in item.checked_attributes] or _default_checked_attributes(
                sde.type_id, family, values_by_type, mutable_attr_ids
            )
            checked = [a for a in checked if a in baseline_values]
            allowed.checked_attributes = checked
            allowed.baseline = {a: baseline_values[a] for a in checked}
            raw_bounds = item.attribute_bounds or {}
            allowed.attribute_bounds = {
                int(k): v for k, v in raw_bounds.items() if int(k) in checked
            }
            for attr_id in checked:
                label, hig = attribute_meta.get(attr_id, (str(attr_id), True))
                allowed.attribute_labels[attr_id] = label
                allowed.attribute_high_is_good[attr_id] = hig

            high_is_good = allowed.attribute_high_is_good
            family_member_ids = set()
            for row in family:
                family_member_ids.add(row["type_id"])
                if row["type_id"] == sde.type_id:
                    continue
                if row["meta_group_id"] == EveMetaGroupId.ABYSSAL:
                    continue
                if row["meta_group_id"] not in allowed_groups:
                    continue
                if _meets_or_beats(
                    values_by_type.get(row["type_id"], {}), allowed.baseline, checked, high_is_good
                ):
                    allowed.substitutes[row["type_id"]] = row["name"]

            if item.allow_mutated:
                for source_id in family_member_ids:
                    for abyssal_id, abyssal_name in abyssal_by_source.get(source_id, []):
                        allowed.mutated_candidates[abyssal_id] = abyssal_name
                        allowed.candidate_static_values[abyssal_id] = values_by_type.get(
                            abyssal_id, {}
                        )

        for override in overrides_by_item.get(item.pk, []):
            if override.mode == FitItemOverride.Mode.INCLUDE:
                name = (
                    override_sde[override.alt_type_id].name
                    if override.alt_type_id in override_sde
                    else override.alt_type.name
                )
                allowed.substitutes[override.alt_type_id] = name
                allowed.mutated_candidates.pop(override.alt_type_id, None)
            else:
                allowed.substitutes.pop(override.alt_type_id, None)
                allowed.mutated_candidates.pop(override.alt_type_id, None)

        result[item.pk] = allowed
    return result


def collect_mutated_stat_specs(parsed_items, fit) -> list[dict]:
    """For a parsed fit, list the abyssal modules that still need rolled stats
    and exactly which attributes to ask for (with baselines for context).

    Returns [{"type_id", "name", "attributes": [
        {"attr_id", "label", "baseline", "high_is_good"}, ...]}, ...]
    """
    pending_type_ids = {
        item.type_id for item in parsed_items if item.mutated_attributes is None
    }
    if not pending_type_ids:
        return []
    abyssal = {
        row["type_id"]: row["name"]
        for row in SdeType.objects.filter(
            type_id__in=pending_type_ids, meta_group_id=EveMetaGroupId.ABYSSAL
        ).values("type_id", "name")
    }
    if not abyssal:
        return []

    allowed_sets = resolve_allowed_bulk(list(fit.items.all()))
    specs: dict[int, dict] = {}
    for allowed in allowed_sets.values():
        for type_id, name in abyssal.items():
            if type_id not in allowed.mutated_candidates:
                continue
            spec = specs.setdefault(
                type_id, {"type_id": type_id, "name": name, "attributes": {}}
            )
            for attr_id in allowed.checked_attributes:
                baseline = allowed.baseline.get(attr_id)
                if baseline is None:
                    continue
                existing = spec["attributes"].get(attr_id)
                if existing is None:
                    spec["attributes"][attr_id] = {
                        "attr_id": attr_id,
                        "label": allowed.attribute_labels.get(attr_id, str(attr_id)),
                        "baseline": baseline,
                        "high_is_good": allowed.attribute_high_is_good.get(attr_id, True),
                    }
    result = []
    for spec in specs.values():
        if spec["attributes"]:
            spec["attributes"] = sorted(
                spec["attributes"].values(), key=lambda a: a["label"]
            )
            result.append(spec)
    return sorted(result, key=lambda s: s["name"])


def _default_checked_attributes(
    type_id: int,
    family: list[dict],
    values_by_type: dict[int, dict[int, float]],
    mutable_attr_ids: dict[int, set[int]],
) -> list[int]:
    """Default comparison set: attributes that vary across the variant family,
    plus everything a mutaplasmid can roll, minus fitting-cost attributes."""
    varying: set[int] = set()
    family_ids = [row["type_id"] for row in family] or [type_id]
    all_attr_ids: set[int] = set()
    for fid in family_ids:
        all_attr_ids |= values_by_type.get(fid, {}).keys()
    for attr_id in all_attr_ids:
        seen = {values_by_type.get(fid, {}).get(attr_id) for fid in family_ids}
        if len(seen) > 1:
            varying.add(attr_id)
    for fid in family_ids:
        varying |= mutable_attr_ids.get(fid, set())
    return sorted(varying - DEFAULT_EXCLUDED_CHECK_ATTRIBUTES)


def candidate_attributes_for_item(item) -> list[dict]:
    """The 'meaningful' attributes a manager can require for a meet-or-beat item:
    family-varying + mutaplasmid-rollable, minus fitting-cost/bookkeeping, limited
    to attributes the doctrine module actually has a value for. Returns
    ``[{attr_id, label, high_is_good, baseline, selected}]`` sorted by label, where
    ``selected`` reflects the item's current explicit ``checked_attributes`` list.

    Used by the per-attribute policy editor; mirrors the default-set logic in
    ``_default_checked_attributes`` so the editor offers exactly what the engine
    would otherwise auto-pick."""
    sde = SdeType.objects.filter(type_id=item.module_type_id).first()
    if sde is None:
        return []
    parent_id = sde.variation_parent_type_id or sde.type_id
    family = list(
        SdeType.objects.filter(
            variation_parent_type_id=parent_id, published=True
        ).values("type_id", "name", "meta_group_id", "meta_level")
    )
    family_type_ids = {row["type_id"] for row in family} or {sde.type_id}

    mutable_attr_ids: dict[int, set[int]] = defaultdict(set)
    for mapping in SdeMutaplasmidMapping.objects.filter(
        source_type_id__in=family_type_ids
    ):
        for spec in mapping.mutable_attributes:
            attr_id = spec.get("attr_id")
            if attr_id:
                mutable_attr_ids[mapping.source_type_id].add(int(attr_id))

    value_type_ids = set(family_type_ids) | {sde.type_id}
    values_by_type: dict[int, dict[int, float]] = defaultdict(dict)
    for type_id, attr_id, value in SdeTypeAttribute.objects.filter(
        eve_type_id__in=value_type_ids
    ).values_list("eve_type_id", "attribute_id", "value"):
        values_by_type[type_id][attr_id] = value
    baseline_values = values_by_type.get(sde.type_id, {})

    candidate_ids = set(
        _default_checked_attributes(sde.type_id, family, values_by_type, mutable_attr_ids)
    )
    candidate_ids |= {int(a) for a in (item.checked_attributes or [])}
    # Only attributes the doctrine module itself has a baseline for are
    # comparable (the engine drops the rest).
    candidate_ids &= set(baseline_values)
    if not candidate_ids:
        return []

    attribute_meta = {
        row["attribute_id"]: (row["display_name"] or row["name"], row["high_is_good"])
        for row in SdeAttribute.objects.filter(attribute_id__in=candidate_ids).values(
            "attribute_id", "display_name", "name", "high_is_good"
        )
    }
    selected = {int(a) for a in (item.checked_attributes or [])}
    rows = []
    for attr_id in candidate_ids:
        label, high_is_good = attribute_meta.get(attr_id, (str(attr_id), True))
        rows.append(
            {
                "attr_id": attr_id,
                "label": label,
                "high_is_good": high_is_good,
                "baseline": baseline_values.get(attr_id),
                "selected": attr_id in selected,
            }
        )
    return sorted(rows, key=lambda r: r["label"])


def rollable_attributes_for_item(item) -> list[dict]:
    """Every attribute a mutaplasmid can actually roll for this module - the real
    "what changes if you make it abyssal" set - INCLUDING fitting attributes like
    CPU/PG (unlike the default auto-set, which excludes fitting costs). Each row is
    ``{attr_id, label, high_is_good, baseline, selected}`` compared against the
    standard module's baseline. Powers the required-attributes modal and validates
    the save endpoint."""
    sde = SdeType.objects.filter(type_id=item.module_type_id).first()
    if sde is None:
        return []
    parent_id = sde.variation_parent_type_id or sde.type_id
    family_type_ids = set(
        SdeType.objects.filter(
            variation_parent_type_id=parent_id, published=True
        ).values_list("type_id", flat=True)
    ) or {sde.type_id}

    rollable: set[int] = set()
    mult_by_attr: dict[int, tuple[float, float]] = {}  # attr_id -> (min_mult, max_mult)
    for mapping in SdeMutaplasmidMapping.objects.filter(
        source_type_id__in=family_type_ids
    ):
        for spec in mapping.mutable_attributes:
            attr_id = spec.get("attr_id")
            if attr_id:
                rollable.add(int(attr_id))
                lo, hi = spec.get("min"), spec.get("max")
                if lo is not None and hi is not None:
                    mult_by_attr[int(attr_id)] = (lo, hi)
    rollable |= {int(a) for a in (item.checked_attributes or [])}

    baseline_values = dict(
        SdeTypeAttribute.objects.filter(eve_type_id=sde.type_id)
        .values_list("attribute_id", "value")
    )
    # Only attributes the standard module actually has a value for are comparable.
    rollable &= set(baseline_values)
    if not rollable:
        return []

    attribute_meta = {
        row["attribute_id"]: (row["display_name"] or row["name"], row["high_is_good"])
        for row in SdeAttribute.objects.filter(attribute_id__in=rollable).values(
            "attribute_id", "display_name", "name", "high_is_good"
        )
    }
    saved_bounds = item.attribute_bounds or {}
    selected = {int(a) for a in (item.checked_attributes or [])}
    rows = []
    for attr_id in rollable:
        label, high_is_good = attribute_meta.get(attr_id, (str(attr_id), True))
        base = baseline_values.get(attr_id)
        mults = mult_by_attr.get(attr_id)
        if mults and base is not None:
            edges = sorted((base * mults[0], base * mults[1]))
            abyssal_min, abyssal_max = edges[0], edges[1]
        else:
            abyssal_min = abyssal_max = base
        bound = saved_bounds.get(str(attr_id)) or saved_bounds.get(attr_id)
        rows.append(
            {
                "attr_id": attr_id,
                "label": label,
                "high_is_good": high_is_good,
                "baseline": base,
                "abyssal_min": abyssal_min,
                "abyssal_max": abyssal_max,
                "selected": attr_id in selected,
                "bound": bound,
                "icon": attribute_icon(label),
            }
        )
    return sorted(rows, key=lambda r: r["label"])


# Keyword -> Font Awesome glyph for attribute rows in the abyssal modal. EVE's
# own per-attribute icons aren't served by the image server, so we approximate
# with FA glyphs matched on the attribute's display name (robust to attr-id
# drift across SDE builds). First substring match wins; order matters.
_ATTRIBUTE_ICON_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("cpu",), "fa-microchip"),
    (("power grid", "powergrid", "powerload"), "fa-plug"),
    (("capacitor", "cap ", "energy transfer", "neutral", "nosferatu"), "fa-battery-half"),
    (("optimal", "max range", "range"), "fa-ruler-horizontal"),
    (("falloff",), "fa-ruler"),
    (("tracking",), "fa-crosshairs"),
    (("scan resolution", "resolution"), "fa-satellite-dish"),
    (("velocity", "speed", "web"), "fa-gauge-high"),
    (("duration", "rate of fire", "cycle"), "fa-clock"),
    (("damage", "rof"), "fa-burst"),
    (("shield",), "fa-shield-halved"),
    (("armor", "repair", "hull"), "fa-wrench"),
    (("warp", "scramble", "disrupt", "point"), "fa-anchor"),
    (("hp", "hit points"), "fa-heart"),
    (("mass", "agility", "inertia"), "fa-weight-hanging"),
)


def attribute_icon(label: str | None) -> str:
    """A Font Awesome class approximating an EVE attribute's icon, by name."""
    text = (label or "").lower()
    for keywords, glyph in _ATTRIBUTE_ICON_RULES:
        if any(k in text for k in keywords):
            return glyph
    return "fa-gauge"


def abyssal_name_for_item(item) -> tuple[int | None, str | None]:
    """The EVE-standard abyssal module name + type_id reachable from this
    module's variant family (e.g. "Small Abyssal Energy Nosferatu"), taken
    straight from the SDE. The abyssal result type name is the same regardless
    of mutaplasmid grade, so the first mapping in the family is representative.
    Returns ``(None, None)`` when the family has no mutaplasmid mapping."""
    sde = SdeType.objects.filter(type_id=item.module_type_id).first()
    if sde is None:
        return None, None
    parent_id = sde.variation_parent_type_id or sde.type_id
    family_type_ids = set(
        SdeType.objects.filter(
            variation_parent_type_id=parent_id, published=True
        ).values_list("type_id", flat=True)
    ) or {sde.type_id}
    mapping = (
        SdeMutaplasmidMapping.objects.filter(source_type_id__in=family_type_ids)
        .select_related("abyssal_type")
        .order_by("abyssal_type__name")
        .first()
    )
    if mapping is None:
        return None, None
    return mapping.abyssal_type_id, mapping.abyssal_type.name


def _families_for_type_ids(
    type_ids: set[int],
) -> tuple[dict[int, list[dict]], dict[int, int]]:
    """For the variant families of the given module type ids, return
    ``(families, parent_by_type)``: ``families`` maps a family parent id to its
    member rows (``{type_id, name, meta_group_id}``); ``parent_by_type`` maps each
    requested type id to its (normalized) family parent id. One ``in_bulk`` plus
    one family query - the same pattern as ``resolve_allowed_bulk``."""
    type_ids = {int(t) for t in type_ids}
    if not type_ids:
        return {}, {}
    sde_types = SdeType.objects.in_bulk(type_ids)
    parent_by_type = {
        tid: (sde.variation_parent_type_id or sde.type_id)
        for tid, sde in sde_types.items()
    }
    families: dict[int, list[dict]] = defaultdict(list)
    for row in SdeType.objects.filter(
        variation_parent_type_id__in=set(parent_by_type.values()), published=True
    ).values("type_id", "name", "variation_parent_type_id", "meta_group_id"):
        families[row["variation_parent_type_id"]].append(row)
    return families, parent_by_type


def possible_meta_groups_bulk(type_ids) -> dict[int, set[int]]:
    """Map each module ``type_id`` to the meta-group ids of its *substitutes* - the
    non-abyssal members of its variant family OTHER than the type itself. These are
    the only meta groups worth offering as a substitution allow-list; an **empty
    set means the item has no variant substitutes** (so the editor offers no
    checkboxes). The type's own group is included only when a sibling shares it (a
    same-group variant the item could be swapped for); a group whose only member is
    the item itself is dropped, since the exact type is never its own substitute.
    Excludes abyssal (``EveMetaGroupId.ABYSSAL``, gated by ``allow_mutated``) and
    ``None`` groups. Types absent from the SDE map to an empty set."""
    families, parent_by_type = _families_for_type_ids({int(t) for t in type_ids})
    return {
        type_id: {
            row["meta_group_id"]
            for row in families.get(parent_id, ())
            if row["type_id"] != type_id
            and row["meta_group_id"] is not None
            and row["meta_group_id"] != EveMetaGroupId.ABYSSAL
        }
        for type_id, parent_id in parent_by_type.items()
    }


def possible_meta_groups_for_item(item) -> set[int]:
    """The meta groups present in this item's variant family (see
    ``possible_meta_groups_bulk``)."""
    return possible_meta_groups_bulk({item.module_type_id}).get(
        item.module_type_id, set()
    )
