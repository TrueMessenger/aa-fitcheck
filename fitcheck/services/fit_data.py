"""Engine-neutral representation of a fit.

Every intake path (EFT paste, ESI fittings, future killmail adapter) produces a
``ParsedFit``; the compliance engine only ever consumes this shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FitItem:
    section: str
    type_id: int
    quantity: int = 1
    charge_type_id: int | None = None
    # {attr_id: rolled_value} for abyssal modules with known rolls.
    mutated_attributes: dict[int, float] | None = None
    mutation_source: str = ""
    # ESI asset item_id when this item was built from a pilot's assets; lets the
    # mutated-roll verifier match rolls to the exact fitted module (two abyssal
    # modules of the same type can carry different rolls).
    source_item_id: int | None = None
    # True when this is an abyssal module whose roll lookup was skipped by the
    # per-ship abyssal-lookups cap (Settings -> Scan & Result Limits), so
    # mutated_attributes stays None for a reason other than an ESI miss.
    mutation_capped: bool = False


@dataclass
class ParseError:
    line_no: int
    text: str
    reason: str


@dataclass
class ParsedFit:
    ship_type_id: int | None
    fit_name: str = ""
    items: list[FitItem] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    # Non-blocking issues (e.g. an unrecognized mutated attribute name).
    warnings: list[ParseError] = field(default_factory=list)
    # Implant type IDs the pilot verifiably has (active clone + inventory).
    # None = unknown/unverifiable (EFT paste, missing ESI scopes).
    pilot_implant_type_ids: set[int] | None = None
    # Informational only: the EVE type_id of the ship currently inside the
    # parent ship's Frigate Escape Bay (battleships, navy battleships, black
    # ops, marauders). None = bay empty, hull lacks an FEB, or unknown.
    frigate_escape_bay_type_id: int | None = None
    # ESI asset item_id of the ship this was built from (inventory path), so a
    # later Re-check can re-pull the same ship's latest fit. None for EFT pastes.
    source_ship_item_id: int | None = None
    # Count of abyssal roll lookups skipped by the per-ship cap on this ship
    # (0 = none skipped). Lets callers surface truncation instead of it
    # silently reading as "no rolled stats provided".
    abyssal_capped: int = 0

    @property
    def has_blocking_errors(self) -> bool:
        return bool(self.errors) or self.ship_type_id is None

    def items_in(self, section: str) -> list[FitItem]:
        return [item for item in self.items if item.section == section]
