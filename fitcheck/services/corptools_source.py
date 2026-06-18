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


def assets_for_character(character_id: int) -> list[dict] | None:
    """corptools-cached assets as ESI-shaped dicts - the SAME shape
    `esi_assets._fetch_assets` returns - so the rest of the pipeline
    (`fit_items_from_flags`, `build_parsed_fit`, the member-inventory scan) is
    untouched.

    Returns None when corptools cannot supply them (not installed, character
    not audited, or the assets module never synced) so the caller falls back to
    a live ESI fetch. An empty list means "synced, and the pilot genuinely owns
    nothing" - distinct from None."""
    if not corptools_installed():
        return None
    audit = _audit_for(character_id)
    if audit is None:
        return None
    if assets_synced_at(character_id) is None:
        # Audited but the assets module never ran: don't present an empty tree
        # as if the pilot owned nothing - let the caller fall back to ESI.
        return None
    _CharacterAudit, CharacterAsset = _models()
    rows = CharacterAsset.objects.filter(character=audit).values(
        "item_id",
        "type_id",
        "location_id",
        "location_flag",
        "quantity",
        "singleton",
        "name",
    )
    return [
        {
            "item_id": r["item_id"],
            "type_id": r["type_id"],
            "location_id": r["location_id"],
            "location_flag": r["location_flag"] or "",
            "quantity": r["quantity"] or 1,
            "is_singleton": bool(r["singleton"]),
            "name": r["name"] or "",
        }
        for r in rows
    ]
