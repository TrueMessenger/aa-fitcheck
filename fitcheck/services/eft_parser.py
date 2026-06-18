"""EFT fitting text parser.

Sections are classified by each type's functional ``slot_kind`` (from the local
SDE mirror), not by blank-line block order, so the parser tolerates the many
EFT layout variants in the wild. Pyfa's extended mutation syntax
(``Module Name [1]`` references with trailing attribute blocks) is supported.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from django.db.models.functions import Upper

from ..constants import EveCategoryId, Section, SlotKind
from ..models import SdeAttribute, SdeType
from .fit_data import FitItem, ParsedFit, ParseError

_HEADER_RE = re.compile(r"^\[\s*(?P<hull>[^,\[\]]+?)\s*,\s*(?P<name>.*?)\s*\]$")
_EMPTY_SLOT_RE = re.compile(r"^\[\s*empty\b[^\]]*\]$", re.IGNORECASE)
_OFFLINE_RE = re.compile(r"\s*/offline\s*$", re.IGNORECASE)
_QTY_RE = re.compile(r"^(?P<name>.+?)\s+x(?P<qty>\d+)$")
_MUTATION_REF_RE = re.compile(r"^(?P<name>.+?)\s*\[(?P<ref>\d+)\]$")
_MUTATION_BLOCK_RE = re.compile(r"^\[(?P<ref>\d+)\]\s+(?P<base>.+)$")


class ResolvedType(NamedTuple):
    type_id: int
    name: str
    category_id: int
    slot_kind: str
    meta_group_id: int | None


def resolve_type_names(names: set[str]) -> dict[str, ResolvedType]:
    """Batch case-insensitive name lookup. Returns a dict keyed by uppercased name."""
    if not names:
        return {}
    rows = (
        SdeType.objects.annotate(name_upper=Upper("name"))
        .filter(name_upper__in={n.upper() for n in names}, published=True)
        .order_by("type_id")
        .values("type_id", "name", "category_id", "slot_kind", "meta_group_id")
    )
    resolved: dict[str, ResolvedType] = {}
    for row in rows:
        key = row["name"].upper()
        if key not in resolved:
            resolved[key] = ResolvedType(
                row["type_id"],
                row["name"],
                row["category_id"],
                row["slot_kind"],
                row["meta_group_id"],
            )
    return resolved


def _resolve_attribute_names(names: set[str]) -> dict[str, int]:
    """Map mutated-attribute display names (uppercased) to attribute IDs."""
    if not names:
        return {}
    upper_names = {n.upper() for n in names}
    result: dict[str, int] = {}
    rows = SdeAttribute.objects.annotate(
        dn_upper=Upper("display_name"), n_upper=Upper("name")
    ).values("attribute_id", "dn_upper", "n_upper")
    for row in rows:
        if row["dn_upper"] in upper_names:
            result.setdefault(row["dn_upper"], row["attribute_id"])
        if row["n_upper"] in upper_names:
            result.setdefault(row["n_upper"], row["attribute_id"])
    return result


class _Line(NamedTuple):
    line_no: int
    name: str
    quantity: int | None  # None = no xN suffix
    charge_name: str | None
    mutation_ref: str | None


def _section_for(resolved: ResolvedType, has_quantity: bool) -> str | None:
    """Classify a resolved line into a fit section.

    Fitted modules are one line each in EFT; an xN suffix on a module therefore
    means cargo spares (mobile-depot refits)."""
    kind = resolved.slot_kind
    if kind in (SlotKind.HIGH, SlotKind.MED, SlotKind.LOW, SlotKind.RIG, SlotKind.SUBSYSTEM):
        return Section.CARGO if has_quantity else {
            SlotKind.HIGH: Section.HIGH,
            SlotKind.MED: Section.MED,
            SlotKind.LOW: Section.LOW,
            SlotKind.RIG: Section.RIG,
            SlotKind.SUBSYSTEM: Section.SUBSYSTEM,
        }[kind]
    if kind == SlotKind.DRONE:
        return Section.DRONE_BAY
    if kind == SlotKind.FIGHTER:
        return Section.FIGHTER_BAY
    if kind == SlotKind.IMPLANT:
        return Section.IMPLANT
    if kind == SlotKind.BOOSTER:
        return Section.BOOSTER
    if kind == SlotKind.FUEL:
        return Section.FUEL_BAY
    if kind == SlotKind.CHARGE:
        return Section.CARGO
    if kind == SlotKind.SHIP:
        return None  # a ship inside the fit body is always a paste mistake
    return Section.CARGO


def parse_eft(text: str) -> ParsedFit:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    header_idx = None
    for idx, raw in enumerate(lines):
        if raw.strip():
            header_idx = idx
            break
    if header_idx is None:
        return ParsedFit(ship_type_id=None, errors=[ParseError(0, "", "empty input")])

    header_match = _HEADER_RE.match(lines[header_idx].strip())
    if not header_match:
        return ParsedFit(
            ship_type_id=None,
            errors=[
                ParseError(
                    header_idx + 1,
                    lines[header_idx].strip(),
                    "first line must be an EFT header like [Hull, Fit Name]",
                )
            ],
        )
    hull_name = header_match["hull"]
    fit_name = header_match["name"]

    # Split the body from Pyfa's trailing mutation blocks.
    body: list[tuple[int, str]] = []
    mutation_blocks: dict[str, list[tuple[int, str]]] = {}
    current_block: list[tuple[int, str]] | None = None
    for idx in range(header_idx + 1, len(lines)):
        raw = lines[idx].strip()
        block_match = _MUTATION_BLOCK_RE.match(raw)
        if block_match:
            current_block = [(idx + 1, block_match["base"])]
            mutation_blocks[block_match["ref"]] = current_block
            continue
        if current_block is not None:
            if raw:
                current_block.append((idx + 1, raw))
            else:
                current_block = None
            continue
        body.append((idx + 1, raw))

    # First pass: lex body lines.
    lexed: list[_Line] = []
    for line_no, raw in body:
        if not raw or _EMPTY_SLOT_RE.match(raw):
            continue
        raw = _OFFLINE_RE.sub("", raw)
        mutation_ref = None
        ref_match = _MUTATION_REF_RE.match(raw)
        if ref_match and ref_match["ref"] in mutation_blocks:
            raw = ref_match["name"]
            mutation_ref = ref_match["ref"]
        quantity: int | None = None
        qty_match = _QTY_RE.match(raw)
        if qty_match:
            raw = qty_match["name"]
            quantity = int(qty_match["qty"])
        charge_name = None
        lexed.append(_Line(line_no, raw, quantity, charge_name, mutation_ref))

    # Batch-resolve every candidate string (full names plus charge-split halves).
    candidates: set[str] = {hull_name}
    for line in lexed:
        candidates.add(line.name)
        if ", " in line.name:
            left, _, right = line.name.rpartition(", ")
            candidates.update((left, right))
    resolved = resolve_type_names(candidates)

    parsed = ParsedFit(ship_type_id=None, fit_name=fit_name)

    hull = resolved.get(hull_name.upper())
    if hull is None or hull.category_id != EveCategoryId.SHIP:
        parsed.errors.append(
            ParseError(header_idx + 1, hull_name, "unknown ship type in header")
        )
    else:
        parsed.ship_type_id = hull.type_id

    # Parse mutation blocks: line 1 = base type, line 2 = mutaplasmid, line 3 = attribute pairs.
    attr_names: set[str] = set()
    block_attr_pairs: dict[str, list[tuple[str, float]]] = {}
    for ref, block in mutation_blocks.items():
        pairs: list[tuple[str, float]] = []
        if len(block) >= 3:
            attr_line = block[2][1]
            for chunk in attr_line.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                name_part, _, value_part = chunk.rpartition(" ")
                try:
                    value = float(value_part)
                except ValueError:
                    parsed.warnings.append(
                        ParseError(block[2][0], chunk, "could not parse mutated attribute value")
                    )
                    continue
                pairs.append((name_part, value))
                attr_names.add(name_part)
        block_attr_pairs[ref] = pairs
    attr_ids = _resolve_attribute_names(attr_names)

    # Second pass: classify and aggregate.
    aggregated: dict[tuple, FitItem] = {}
    for line in lexed:
        rtype = resolved.get(line.name.upper())
        charge_id = None
        if rtype is None and ", " in line.name:
            left, _, right = line.name.rpartition(", ")
            left_type = resolved.get(left.upper())
            right_type = resolved.get(right.upper())
            if left_type and right_type and right_type.slot_kind == SlotKind.CHARGE:
                rtype, charge_id = left_type, right_type.type_id
        if rtype is None:
            reason = "unknown type name"
            if "abyssal" in line.name.lower():
                reason = "unknown type name (mutated modules need a Pyfa export or manual stats)"
            parsed.errors.append(ParseError(line.line_no, line.name, reason))
            continue

        section = _section_for(rtype, line.quantity is not None)
        if section is None:
            parsed.errors.append(
                ParseError(line.line_no, line.name, "unexpected ship type inside the fit body")
            )
            continue

        mutated: dict[int, float] | None = None
        mutation_source = ""
        if line.mutation_ref is not None:
            mutated = {}
            for attr_name, value in block_attr_pairs.get(line.mutation_ref, []):
                attr_id = attr_ids.get(attr_name.upper())
                if attr_id is None:
                    parsed.warnings.append(
                        ParseError(line.line_no, attr_name, "unrecognized mutated attribute name")
                    )
                    continue
                mutated[attr_id] = value
            mutation_source = "PYFA"

        quantity = line.quantity if line.quantity is not None else 1
        if mutated is not None:
            # Mutated items are unique - never aggregate across references.
            key = ("mut", line.mutation_ref, section, rtype.type_id)
        else:
            key = (section, rtype.type_id, charge_id)
        if key in aggregated:
            aggregated[key].quantity += quantity
        else:
            aggregated[key] = FitItem(
                section=section,
                type_id=rtype.type_id,
                quantity=quantity,
                charge_type_id=charge_id,
                mutated_attributes=mutated,
                mutation_source=mutation_source,
            )
    parsed.items = list(aggregated.values())
    return parsed


_EFT_SECTION_ORDER = (
    Section.LOW,
    Section.MED,
    Section.HIGH,
    Section.RIG,
    Section.SUBSYSTEM,
    Section.DRONE_BAY,
    Section.FIGHTER_BAY,
    Section.CARGO,
    Section.IMPLANT,
)

_UNIT_SECTIONS = (Section.LOW, Section.MED, Section.HIGH, Section.RIG, Section.SUBSYSTEM)


def aggregate_for_buy(items, ship_type_id: int | None = None) -> list[tuple[str, int]]:
    """Flatten a fit into a buy-list: every fitted module, charge and bay item
    pooled by type and summed, with the hull added as one unit so the pilot
    can paste the result into EVE's Multibuy. Accepts an iterable of objects
    with `module_type_id`, `quantity` and optional `charge_type_id` (i.e.
    `DoctrineFitItem` or `SubmissionItem`). Returns `[(name, qty), ...]`
    sorted by name. Charges loaded into modules contribute one per module
    unit, matching how EVE counts them - cargo entries for the same type
    add on top of that."""
    counts: dict[int, int] = {}
    if ship_type_id:
        counts[ship_type_id] = counts.get(ship_type_id, 0) + 1
    for item in items:
        type_id = getattr(item, "module_type_id", None) or getattr(item, "type_id", None)
        if not type_id:
            continue
        qty = int(getattr(item, "quantity", 1) or 1)
        counts[type_id] = counts.get(type_id, 0) + qty
        charge_id = getattr(item, "charge_type_id", None)
        if charge_id:
            counts[charge_id] = counts.get(charge_id, 0) + qty
    if not counts:
        return []
    names = resolve_render_names(counts.keys())
    return sorted(
        ((names.get(tid, f"Type {tid}"), qty) for tid, qty in counts.items()),
        key=lambda row: row[0].lower(),
    )


def resolve_render_names(type_ids) -> dict[int, str]:
    """Type names for EFT / multibuy rendering.

    Our SDE mirror only covers the fitting-relevant categories, so types outside
    it (blueprints, commodities, etc. a pilot may haul in a bay) are absent and
    would render as a bare ``Type 12345``. Fall back to eveuniverse - which the
    rest of the app already uses as the authoritative name layer - and fetch any
    still-unseen type from ESI so reviewers always see a real name."""
    type_ids = {t for t in type_ids if t}
    names = dict(
        SdeType.objects.filter(type_id__in=type_ids).values_list("type_id", "name")
    )
    missing = [t for t in type_ids if t not in names]
    if missing:
        from eveuniverse.models import EveType

        names.update(EveType.objects.filter(id__in=missing).values_list("id", "name"))
        for tid in [t for t in missing if t not in names]:
            try:
                eve_type, _ = EveType.objects.get_or_create_esi(id=tid)
                names[tid] = eve_type.name
            except Exception:  # pragma: no cover - network dependent
                pass
    return names


def render_eft(parsed: ParsedFit, fit_name: str | None = None) -> str:
    """Render a ParsedFit back to EFT text (used to show ESI imports to reviewers)."""
    type_ids = {item.type_id for item in parsed.items}
    type_ids.update(i.charge_type_id for i in parsed.items if i.charge_type_id)
    if parsed.ship_type_id:
        type_ids.add(parsed.ship_type_id)
    names = resolve_render_names(type_ids)

    ship = names.get(parsed.ship_type_id, f"Type {parsed.ship_type_id}")
    blocks: list[str] = [f"[{ship}, {fit_name or parsed.fit_name or 'Fit'}]"]
    for section in _EFT_SECTION_ORDER:
        items = parsed.items_in(section)
        if not items:
            continue
        lines = []
        for item in items:
            name = names.get(item.type_id, f"Type {item.type_id}")
            if item.charge_type_id:
                charge = names.get(item.charge_type_id, f"Type {item.charge_type_id}")
                name = f"{name}, {charge}"
            if section in _UNIT_SECTIONS:
                lines.extend([name] * item.quantity)
            else:
                lines.append(f"{name} x{item.quantity}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
