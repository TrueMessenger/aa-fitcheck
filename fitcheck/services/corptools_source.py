"""Optional read-through to aa-corptools (Corp Tools) cached character data.

When the `corptools` plugin is installed alongside fitcheck and has already
synced a character's assets, fitcheck reads them straight from the local DB
instead of calling ESI live. The asset tree is the heaviest ESI call and the
least time-sensitive check, so serving it from corptools' cache removes a per
check ESI round-trip (and, on an alliance-wide member scan, removes the need
for fitcheck to hold a token at all - the player already granted the scope to
corptools).

Nothing here imports `corptools` at module load: everything resolves through
the Django app registry, so fitcheck runs fine when corptools is absent.

corptools schema (verified against corptools/models/assets.py + audits.py):
- `CharacterAudit.character` -> allianceauth EveCharacter (OneToOne);
  `update_timestamps` JSON, read via `get_update_time("assets")`.
- `CharacterAsset` stores the raw ESI shape: `item_id`, `type_id` (int),
  `location_id` (flat ESI id), `location_flag` (raw ESI flag), `quantity`,
  `singleton`, `name`. corptools only remaps two exotic location flags
  ("unknown location_flag (NNN)"), none of which fitcheck classifies on, so
  the flags arrive as fitcheck's slot/section logic expects.
"""

from __future__ import annotations

import logging

from django.apps import apps as django_apps

logger = logging.getLogger(__name__)


def corptools_installed() -> bool:
    return django_apps.is_installed("corptools")


def _models():
    return (
        django_apps.get_model("corptools", "CharacterAudit"),
        django_apps.get_model("corptools", "CharacterAsset"),
    )


def _audit_for(character_id: int):
    CharacterAudit, _CharacterAsset = _models()
    return CharacterAudit.objects.filter(
        character__character_id=character_id
    ).first()


def assets_synced_at(character_id: int):
    """When corptools last refreshed this character's assets, or None if the
    character isn't audited (or corptools never ran its assets module)."""
    if not corptools_installed():
        return None
    audit = _audit_for(character_id)
    if audit is None:
        return None
    try:
        return audit.get_update_time("assets")
    except Exception:  # pragma: no cover - guards corptools API drift
        return None


# The CharacterAsset columns we read, in the order the ESI-shaped dict needs them.
_ASSET_FIELDS = (
    "item_id",
    "type_id",
    "location_id",
    "location_flag",
    "quantity",
    "singleton",
    "name",
)


def _map_asset_row(r: dict) -> dict:
    """One corptools CharacterAsset .values() row -> the ESI-shaped dict the rest
    of the pipeline (`fit_items_from_flags`, `build_parsed_fit`) consumes."""
    return {
        "item_id": r["item_id"],
        "type_id": r["type_id"],
        "location_id": r["location_id"],
        "location_flag": r["location_flag"] or "",
        "quantity": r["quantity"] or 1,
        "is_singleton": bool(r["singleton"]),
        "name": r["name"] or "",
    }


def _servable_audit(character_id: int):
    """The CharacterAudit whose assets corptools can serve, or None when it
    can't (not installed, character not audited, or assets never synced). Shared
    guard for every read path so the None-fallback semantics stay identical."""
    if not corptools_installed():
        return None
    audit = _audit_for(character_id)
    if audit is None:
        return None
    if assets_synced_at(character_id) is None:
        # Audited but the assets module never ran: don't present an empty tree
        # as if the pilot owned nothing - let the caller fall back to ESI.
        return None
    return audit


def assets_for_character(character_id: int) -> list[dict] | None:
    """corptools-cached assets as ESI-shaped dicts - the SAME shape
    `esi_assets._fetch_assets` returns - so the rest of the pipeline
    (`fit_items_from_flags`, `build_parsed_fit`, the member-inventory scan) is
    untouched.

    Returns None when corptools cannot supply them (not installed, character
    not audited, or the assets module never synced) so the caller falls back to
    a live ESI fetch. An empty list means "synced, and the pilot genuinely owns
    nothing" - distinct from None.

    NOTE: this loads the character's WHOLE asset tree. For listing ships or
    grading a chosen ship, prefer the narrow `ship_assets_for_character` /
    `ship_contents_for_character` below - on an alliance with thousands of
    audited pilots the full table is millions of rows."""
    audit = _servable_audit(character_id)
    if audit is None:
        return None
    _CharacterAudit, CharacterAsset = _models()
    rows = CharacterAsset.objects.filter(character=audit).values(*_ASSET_FIELDS)
    return [_map_asset_row(r) for r in rows]


def ship_assets_for_character(
    character_id: int, ship_type_ids=None
) -> list[dict] | None:
    """Phase 1 (narrow): just the assembled ships (singletons) this character
    owns, optionally restricted to `ship_type_ids` (a doctrine cares about one
    hull). A handful of rows instead of the whole asset tree - the DB filters
    server-side, so the millions-of-rows table never lands in Python.

    Same None-vs-empty semantics as `assets_for_character`."""
    audit = _servable_audit(character_id)
    if audit is None:
        return None
    _CharacterAudit, CharacterAsset = _models()
    lookups = {"character": audit, "singleton": True}
    if ship_type_ids is not None:
        lookups["type_id__in"] = list(ship_type_ids)
    rows = CharacterAsset.objects.filter(**lookups).values(*_ASSET_FIELDS)
    return [_map_asset_row(r) for r in rows]


def ship_contents_for_character(
    character_id: int, ship_item_ids
) -> list[dict] | None:
    """Phase 2 (narrow): the rows needed to grade the given ship(s) - each ship
    row itself plus whatever is fitted/contained directly in it (location_id ==
    the ship's item_id). That is exactly the slice `build_parsed_fit` reads, so
    we never materialise the rest of the pilot's hangar.

    Two indexed single-field queries (ships by item_id, contents by location_id)
    rather than one OR'd query - keeps the read trivially narrow. Same
    None-vs-empty semantics as `assets_for_character`."""
    audit = _servable_audit(character_id)
    if audit is None:
        return None
    ids = list(ship_item_ids)
    if not ids:
        return []
    _CharacterAudit, CharacterAsset = _models()
    ship_rows = CharacterAsset.objects.filter(
        character=audit, item_id__in=ids
    ).values(*_ASSET_FIELDS)
    child_rows = CharacterAsset.objects.filter(
        character=audit, location_id__in=ids
    ).values(*_ASSET_FIELDS)
    # Dedupe by item_id (a ship is never its own content, but be defensive).
    by_item: dict[int, dict] = {}
    for r in list(ship_rows) + list(child_rows):
        by_item[r["item_id"]] = _map_asset_row(r)
    return list(by_item.values())
