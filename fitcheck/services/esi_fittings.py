"""Save a doctrine fit straight into a pilot's in-game Fittings panel.

We POST the fit through ESI's `/characters/{character_id}/fittings/` endpoint.
EVE's saved-fittings format expects one item per fitted slot (HiSlot0, HiSlot1,
...) plus aggregated quantities for bays - the helpers here translate from our
section/quantity shape (which mirrors EFT) into that flag-indexed layout.
"""

from __future__ import annotations

import logging

from ..constants import Section
from ..models import DoctrineFitItem
from .esi_assets import (
    FITTINGS_READ_SCOPES,
    FITTINGS_WRITE_SCOPES,
    _to_dict,
    esi_client,
    fit_items_from_flags,
)
from .fit_data import ParsedFit

logger = logging.getLogger(__name__)


class NoFittingsTokenError(RuntimeError):
    """User has not yet granted the fittings-write scope for this character."""


# Mapping from our slot sections to EVE's flag prefix. Each fitted unit in a
# slot section becomes one item entry at `{prefix}{index}`. Bay sections
# (drone bay, fighter bay, cargo) collapse to a single flag with a summed
# quantity. Implants are intentionally skipped - EVE saved fittings don't
# store implant choices, they live on the pilot's clone.
_SLOT_FLAG_PREFIX: dict[str, str] = {
    Section.HIGH: "HiSlot",
    Section.MED: "MedSlot",
    Section.LOW: "LoSlot",
    Section.RIG: "RigSlot",
    Section.SUBSYSTEM: "SubSystemSlot",
}

_BAY_FLAG: dict[str, str] = {
    Section.DRONE_BAY: "DroneBay",
    Section.FIGHTER_BAY: "FighterBay",
    Section.CARGO: "Cargo",
}


def build_esi_fitting_payload(fit) -> dict:
    """Translate a DoctrineFit into the body ESI's POST fittings expects.

    Returns a dict ready to hand to django-esi as `body=...`. Slot modules
    take one entry per unit; charges loaded into slot modules become a second
    entry at the same slot flag (matching EVE's saved-fitting convention).
    Bay sections roll up to one entry per type with summed quantity.
    """
    items_qs = (
        DoctrineFitItem.objects.filter(fit=fit)
        .select_related("module_type")
        .order_by("section", "module_type__name")
    )

    payload_items: list[dict] = []
    slot_index: dict[str, int] = {prefix: 0 for prefix in _SLOT_FLAG_PREFIX.values()}

    for item in items_qs:
        section = item.section
        if section == Section.IMPLANT:
            continue
        prefix = _SLOT_FLAG_PREFIX.get(section)
        if prefix is not None:
            for _ in range(item.quantity):
                flag = f"{prefix}{slot_index[prefix]}"
                payload_items.append(
                    {"flag": flag, "quantity": 1, "type_id": item.module_type_id}
                )
                if item.charge_type_id:
                    payload_items.append(
                        {"flag": flag, "quantity": 1, "type_id": item.charge_type_id}
                    )
                slot_index[prefix] += 1
            continue
        bay_flag = _BAY_FLAG.get(section)
        if bay_flag is None:
            continue
        payload_items.append(
            {"flag": bay_flag, "quantity": item.quantity, "type_id": item.module_type_id}
        )

    description = (fit.description or fit.name or "")[:500] or fit.name[:500]
    return {
        "name": (fit.name or "Untitled")[:50],
        "description": description,
        "ship_type_id": fit.ship_type_id,
        "items": payload_items,
    }


def save_fit_to_eve(user, character_id: int, fit) -> int | None:
    """POST the doctrine fit to EVE's saved fittings for one character.

    Returns the new fitting_id on success. Raises NoFittingsTokenError if the
    pilot hasn't granted the write scope for this character yet (the caller
    should redirect them through the SSO grant flow).
    """
    from esi.models import Token

    token = (
        Token.objects.filter(user=user, character_id=character_id)
        .require_scopes(FITTINGS_WRITE_SCOPES)
        .require_valid()
        .first()
    )
    if token is None:
        raise NoFittingsTokenError()

    payload = build_esi_fitting_payload(fit)
    operation = esi_client().client.Fittings.PostCharactersCharacterIdFittings(
        character_id=character_id, body=payload, token=token
    )
    result = _to_dict(operation.results(use_etag=False))
    fitting_id = result.get("fitting_id") if isinstance(result, dict) else None
    logger.info(
        "Saved fit %s as ESI fitting_id=%s for character %s",
        fit.pk, fitting_id, character_id,
    )
    return fitting_id


def fetch_saved_fittings(user, character_id: int) -> list[dict] | None:
    """Read a character's in-game saved fittings via ESI.

    Returns the raw fitting dicts (fitting_id, name, ship_type_id, items[]), or
    None when the character has no fittings-read token (so the caller can tell
    'no token' apart from 'no saved fittings' and offer the SSO grant)."""
    from esi.models import Token

    token = (
        Token.objects.filter(user=user, character_id=character_id)
        .require_scopes(FITTINGS_READ_SCOPES)
        .require_valid()
        .first()
    )
    if token is None:
        return None
    rows = esi_client().client.Fittings.GetCharactersCharacterIdFittings(
        character_id=character_id, token=token
    ).results(use_etag=False)
    return [_to_dict(row) for row in rows]


def parsed_fit_from_saved(fitting: dict) -> ParsedFit:
    """Build an engine ParsedFit from one ESI saved-fitting dict. Saved fittings
    carry no implants (those live on the clone), so pilot_implant_type_ids is
    populated separately from the active clone when available."""
    rows = [
        (_to_dict(it)["type_id"], _to_dict(it)["flag"], _to_dict(it).get("quantity", 1) or 1)
        for it in fitting.get("items", [])
    ]
    return ParsedFit(
        ship_type_id=fitting.get("ship_type_id"),
        fit_name=fitting.get("name", ""),
        items=fit_items_from_flags(rows),
    )
