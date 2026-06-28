"""Pilot ship inventory via ESI assets.

Lists the ships a member actually owns (across every character joined to their
Auth account) and rebuilds the fitted state of a selected ship from the asset
tree - no manual EFT pasting. Mutated module rolls are verified through the
public dynamic-items endpoint whenever the asset's item_id allows it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .. import __version__
from ..constants import (
    ESI_COMPATIBILITY_DATE,
    EveCategoryId,
    EveMetaGroupId,
    SLOT_SECTIONS,
    Section,
    SlotKind,
    esi_flag_to_section,
)
from ..models import SdeType, SubmissionItem
from .fit_data import FitItem, ParsedFit

logger = logging.getLogger(__name__)

# django-esi raises this when ESI returns 420 (error limited). Guarded import so
# the module still loads on older django-esi versions that lack the class.
try:
    from esi.exceptions import ESIErrorLimitException as _ESIErrorLimit
except Exception:  # pragma: no cover - version dependent
    _ESIErrorLimit = None


def is_error_limited(exc: Exception) -> bool:
    """True when ESI signalled its error/rate limit (HTTP 420/429). When this
    happens during a bulk scan we MUST stop issuing requests - continuing only
    deepens the error budget and risks an application ban (ESI etiquette)."""
    if _ESIErrorLimit is not None and isinstance(exc, _ESIErrorLimit):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status in (420, 429)


class ESIBulkAborted(Exception):
    """Raised to unwind a bulk scan immediately when ESI is error-limited."""


ASSET_SCOPES = ["esi-assets.read_assets.v1"]
# Bundled into the asset-token grant so one consent also covers naming the
# private structures (Citadels) the pilot's ships sit in.
STRUCTURE_SCOPES = ["esi-universe.read_structures.v1"]
ASSET_GRANT_SCOPES = ASSET_SCOPES + STRUCTURE_SCOPES
FITTINGS_WRITE_SCOPES = ["esi-fittings.write_fittings.v1"]
FITTINGS_READ_SCOPES = ["esi-fittings.read_fittings.v1"]
CLONES_SCOPES = ["esi-clones.read_implants.v1"]
# Every ESI scope a pilot's audit features can use, requested in ONE SSO consent
# (see views.member.grant_all_esi) instead of one prompt per feature: assets +
# structures (My Ships inventory & location names), implants (verify plugged-in
# implants), and fittings-write (Save-to-EVE). Scopes already shared from another
# Auth app or served by corptools are reused, so the grant only asks for what's
# genuinely missing (see existing_token / get_ship_inventory).
PILOT_GRANT_SCOPES = ASSET_GRANT_SCOPES + CLONES_SCOPES + FITTINGS_WRITE_SCOPES

_NAME_CHUNK = 990  # ESI caps assets/names and universe/names around 1000 ids
# Per-ship ceiling on dynamic-item (abyssal roll) lookups. Each is its own ESI
# call, so an unbounded fan-out across a bulk scan could burn the error budget;
# cap it and log what was skipped rather than silently truncating.
_MAX_DYNAMIC_ITEM_LOOKUPS = 25

# Tags we need from the ESI spec - django-esi 9.x requires this to keep
# the generated client small (and raises AttributeError under DEBUG=False if missing).
_ESI_TAGS = ["Assets", "Universe", "Dogma", "Fittings", "Clones"]

_provider = None


def esi_client():
    """Lazy singleton so importing this module never needs ESI settings."""
    global _provider
    if _provider is None:
        from esi.openapi_clients import ESIClientProvider

        _provider = ESIClientProvider(
            compatibility_date=ESI_COMPATIBILITY_DATE,
            ua_appname="AaFitcheck",
            ua_version=__version__,
            ua_url="https://github.com/TrueMessenger/aa-fitcheck",
            tags=_ESI_TAGS,
        )
    return _provider


@dataclass
class OwnedShip:
    character_id: int
    character_name: str
    item_id: int
    type_id: int
    type_name: str
    group_name: str
    ship_name: str
    location_name: str
    system_name: str = ""
    region_name: str = ""


@dataclass
class ShipInventory:
    ships: list[OwnedShip] = field(default_factory=list)
    # Characters on the account that have no usable assets token yet.
    characters_without_token: list = field(default_factory=list)
    # Characters whose asset fetch errored (name -> reason).
    errors: dict[str, str] = field(default_factory=dict)
    # Set when a scan was cut short by the ESI error limit.
    error_limited: bool = False


def _chunked(seq, size=_NAME_CHUNK):
    seq = list(seq)
    for start in range(0, len(seq), size):
        yield seq[start : start + size]


def _to_dict(obj):
    """ESI responses come back as pydantic model objects in django-esi 9.x;
    older versions returned dicts. Normalize to dict so the rest of the file
    can keep using subscript access uniformly."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return obj


def user_tokens_by_character(user) -> tuple[dict[int, object], list]:
    """{character_id: valid Token} plus the characters lacking one."""
    from esi.models import Token

    ownerships = list(user.character_ownerships.select_related("character"))
    tokens: dict[int, object] = {}
    missing = []
    for ownership in ownerships:
        character = ownership.character
        token = (
            Token.objects.filter(user=user, character_id=character.character_id)
            .require_scopes(ASSET_SCOPES)
            .require_valid()
            .first()
        )
        if token:
            tokens[character.character_id] = token
        else:
            missing.append(character)
    return tokens, missing


def tokens_by_character(character_ids) -> dict[int, object]:
    """Like user_tokens_by_character, but keyed by character_id across every
    user in the system. One valid asset-scope token per character (whichever
    user owns it). Powers the proactive member-inventory feature, where the
    requester needs to reach into other pilots' assets without owning them."""
    from esi.models import Token

    char_ids = list(character_ids)
    if not char_ids:
        return {}
    qs = (
        Token.objects.filter(character_id__in=char_ids)
        .require_scopes(ASSET_SCOPES)
        .require_valid()
    )
    # If multiple users authorised the same character (e.g. they switched
    # accounts), any valid token works; keep the first.
    out: dict[int, object] = {}
    for token in qs:
        out.setdefault(token.character_id, token)
    return out


def existing_token(user, character_id: int, scopes):
    """A valid token the player ALREADY granted (to fitcheck or any other AA
    app - tokens are shared) carrying `scopes`, or None. Lets a view reuse a
    grant corptools/another app obtained instead of re-prompting for consent."""
    from esi.models import Token

    return (
        Token.objects.filter(user=user, character_id=character_id)
        .require_scopes(scopes)
        .require_valid()
        .first()
    )


def _asset_source() -> str:
    """Where the asset tree comes from: 'auto' (corptools cache when synced,
    else live ESI - the default), 'esi' (always live), or 'corptools'
    (cache-only, never fall through to ESI)."""
    from django.conf import settings

    return getattr(settings, "FITCHECK_ASSET_SOURCE", "auto") or "auto"


def resolve_assets(character_id: int, token=None) -> list[dict] | None:
    """One character's assets as ESI-shaped dicts: corptools cache first (no
    token needed) when enabled, else a live ESI fetch (needs `token`). Returns
    None when no source can supply them, so callers fall back / mark the
    character as ungranted."""
    from . import corptools_source

    source = _asset_source()
    if source in ("auto", "corptools"):
        cached = corptools_source.assets_for_character(character_id)
        if cached is not None:
            return cached
        if source == "corptools":
            return None
    if token is None:
        return None
    return _fetch_assets(token, character_id)


def _ship_type_id_set(hull_type_id: int | None = None) -> set[int]:
    """The SHIP-category type_ids a scan should pre-filter to. When `hull_type_id`
    is given (the doctrine case) it's just that hull - no SDE mirror needed, the
    caller already knows the type it wants. With no hull it's every ship type the
    SDE mirror knows; that set is EMPTY until `fitcheck_load_sde` has run, which
    the callers handle by classifying owned assets via eveuniverse instead (see
    `ship_type_ids_among`)."""
    if hull_type_id is not None:
        return {hull_type_id}
    return set(
        SdeType.objects.filter(category_id=EveCategoryId.SHIP).values_list(
            "type_id", flat=True
        )
    )


def _eveuniverse_ship_type_ids(type_ids: set[int]) -> set[int]:
    """Which of `type_ids` are SHIP-category, per eveuniverse, loading any unseen
    type from ESI on demand. Lets the inventory listing work before fitcheck's own
    SDE mirror is populated (eveuniverse is a hard dependency and already cached)."""
    from eveuniverse.models import EveType

    ships = set(
        EveType.objects.filter(
            id__in=type_ids, eve_group__eve_category_id=EveCategoryId.SHIP
        ).values_list("id", flat=True)
    )
    seen = set(EveType.objects.filter(id__in=type_ids).values_list("id", flat=True))
    for type_id in type_ids - seen:
        try:
            eve_type, _ = EveType.objects.get_or_create_esi(id=type_id)
        except Exception:  # pragma: no cover - network dependent
            continue
        if eve_type.eve_group.eve_category_id == EveCategoryId.SHIP:
            ships.add(type_id)
    return ships


def ship_type_ids_among(type_ids) -> set[int]:
    """Which of `type_ids` are ships. The local SDE mirror is authoritative for
    types it has rows for; anything the mirror doesn't know (e.g. before
    `fitcheck_load_sde` has run) is resolved through eveuniverse. So ship listing
    degrades gracefully on a fresh install instead of silently returning nothing."""
    type_ids = {int(t) for t in type_ids}
    if not type_ids:
        return set()
    known = set(
        SdeType.objects.filter(type_id__in=type_ids).values_list("type_id", flat=True)
    )
    ships = set(
        SdeType.objects.filter(
            type_id__in=type_ids, category_id=EveCategoryId.SHIP
        ).values_list("type_id", flat=True)
    )
    unknown = type_ids - known
    if unknown:
        ships |= _eveuniverse_ship_type_ids(unknown)
    return ships


def resolve_ship_list(character_id: int, token, ship_type_ids) -> list[dict] | None:
    """Phase 1: the assembled ships a character owns, as ESI-shaped dicts -
    NOT the whole asset tree.

    corptools serves a narrow query (ships only); a live ESI fetch has no
    server-side filter, so it pulls the tree once and keeps only the ship rows
    (the rest is discarded, never stashed). Returns None when no source can
    supply the character, so the caller marks them ungranted."""
    from . import corptools_source

    # No known ship-type whitelist (the SDE mirror has no ships loaded yet): pull
    # every assembled (singleton) asset and classify each owned type via
    # eveuniverse, so My Ships works before fitcheck_load_sde has run.
    if not ship_type_ids:
        return _resolve_ships_by_classification(character_id, token)

    source = _asset_source()
    if source in ("auto", "corptools"):
        ships = corptools_source.ship_assets_for_character(character_id, ship_type_ids)
        if ships is not None:
            return ships
        if source == "corptools":
            return None
    if token is None:
        return None
    assets = _fetch_assets(token, character_id)
    return [
        a for a in assets
        if a["type_id"] in ship_type_ids and a.get("is_singleton")
    ]


def _resolve_ships_by_classification(character_id: int, token) -> list[dict] | None:
    """Fallback ship list when there's no SDE ship whitelist: fetch the
    character's assembled (singleton) assets from corptools or live ESI, then keep
    only the types eveuniverse classifies as ships. Same None-vs-empty / source
    semantics as the whitelist path."""
    from . import corptools_source

    source = _asset_source()
    singletons: list[dict] | None = None
    if source in ("auto", "corptools"):
        # ship_type_ids=None -> every singleton, no server-side type filter.
        singletons = corptools_source.ship_assets_for_character(character_id, None)
        if singletons is None and source == "corptools":
            return None
    if singletons is None:
        if token is None:
            return None
        singletons = [a for a in _fetch_assets(token, character_id) if a.get("is_singleton")]
    ship_ids = ship_type_ids_among({s["type_id"] for s in singletons})
    return [s for s in singletons if s["type_id"] in ship_ids]


def resolve_contents(
    character_id: int, ship_item_ids, token=None
) -> list[dict] | None:
    """Phase 2: the rows needed to grade the given ship(s) - each ship row plus
    its direct contents.

    corptools serves the narrow two-query slice; live ESI has no server-side
    filter, so it returns the character's tree once (one fetch per character, not
    per ship) and lets `build_parsed_fit` slice out each ship's contents. Returns
    None when no source can supply the character."""
    from . import corptools_source

    source = _asset_source()
    if source in ("auto", "corptools"):
        narrow = corptools_source.ship_contents_for_character(character_id, ship_item_ids)
        if narrow is not None:
            return narrow
        if source == "corptools":
            return None
    if token is None:
        return None
    return _fetch_assets(token, character_id)


def _build_owned_ships(
    character_id: int, char_name: str, ships: list[dict], token, structure_tokens: list
) -> list[OwnedShip]:
    """Turn a character's ship rows into OwnedShip listing entries (names +
    location). Shared by the self-inventory and member-inventory scans.

    May raise on an ESI error-limit while resolving names/locations; callers
    catch it and set `error_limited`. Location resolution walks only the ship
    rows we hold: a docked ship (location_id is a station/structure/system)
    resolves exactly; the rarer ship nested inside a carrier/container degrades
    to a generic label rather than dragging in the whole tree."""
    by_item_id = {s["item_id"]: s for s in ships}
    type_ids = {s["type_id"] for s in ships}
    type_names = dict(
        SdeType.objects.filter(type_id__in=type_ids).values_list("type_id", "name")
    )
    group_names = _ship_group_names(type_ids)
    # Names for types the SDE mirror doesn't carry (e.g. before fitcheck_load_sde):
    # _ship_group_names just loaded these into eveuniverse, so read them back.
    missing_names = type_ids - set(type_names)
    if missing_names:
        from eveuniverse.models import EveType

        type_names.update(
            EveType.objects.filter(id__in=missing_names).values_list("id", "name")
        )
    root_ids = {_root_location(s, by_item_id) for s in ships}
    if token is not None:
        ship_names = _fetch_asset_names(token, character_id, [s["item_id"] for s in ships])
        location_details = _resolve_locations(root_ids, structure_tokens)
    else:
        # corptools-served character: no fitcheck token to resolve ESI names/
        # locations with. Use the cached custom ship name.
        ship_names = {s["item_id"]: s.get("name", "") for s in ships}
        location_details = {}
    out: list[OwnedShip] = []
    for ship in ships:
        root = _root_location(ship, by_item_id)
        out.append(
            OwnedShip(
                character_id=character_id,
                character_name=char_name,
                item_id=ship["item_id"],
                type_id=ship["type_id"],
                type_name=type_names.get(ship["type_id"], f"Type {ship['type_id']}"),
                group_name=group_names.get(ship["type_id"], ""),
                ship_name=ship_names.get(ship["item_id"], ""),
                location_name=(location_details.get(root) or {}).get("name", f"Structure {root}"),
                system_name=(location_details.get(root) or {}).get("system", ""),
                region_name=(location_details.get(root) or {}).get("region", ""),
            )
        )
    return out


def get_inventory_for_characters(characters, hull_type_id: int | None = None) -> ShipInventory:
    """Same shape as `get_ship_inventory(user)` but iterates a set of EveCharacter
    rows we already pulled (alliance- or corp-scoped). `hull_type_id` pre-filters
    the asset scan to ships of that type only - a doctrine fitting only cares
    about one hull, so we skip the rest. Missing tokens land in the same
    `characters_without_token` bucket as the self-inventory flow."""
    inventory = ShipInventory()
    char_by_id = {c.character_id: c for c in characters}
    tokens = tokens_by_character(char_by_id.keys())
    structure_tokens = _structure_tokens(char_by_id.keys())
    # Empty only when no hull was given AND the SDE mirror has no ships loaded;
    # resolve_ship_list then classifies owned assets via eveuniverse. A hull-scoped
    # scan always has {hull_type_id} here, so it never needs the mirror.
    ship_type_ids = _ship_type_id_set(hull_type_id)
    for character_id, character in char_by_id.items():
        token = tokens.get(character_id)
        char_name = character.character_name if character else str(character_id)
        try:
            ships = resolve_ship_list(character_id, token, ship_type_ids)
        except Exception as exc:  # pragma: no cover - network dependent
            if is_error_limited(exc):
                logger.error("ESI error limited during member scan; aborting at %s", char_name)
                inventory.error_limited = True
                break
            logger.warning("Asset fetch failed for %s: %s", char_name, exc)
            inventory.errors[char_name] = str(exc)
            continue
        if ships is None:
            # Neither a granted token nor a corptools cache can supply this
            # character's assets - nothing to scan.
            inventory.characters_without_token.append(character)
            continue
        if not ships:
            continue
        try:
            inventory.ships.extend(
                _build_owned_ships(character_id, char_name, ships, token, structure_tokens)
            )
        except Exception as exc:  # pragma: no cover - network dependent
            if is_error_limited(exc):
                logger.error("ESI error limited resolving names/locations; aborting member scan")
                inventory.error_limited = True
                break
            raise
    inventory.ships.sort(key=lambda s: (s.character_name, s.type_name))
    return inventory


def _fetch_assets(token, character_id: int) -> list[dict]:
    # use_etag=False keeps django-esi from raising HTTPNotModified on a 304 cache
    # hit - we always want the latest snapshot rebuilt into ParsedFit objects.
    operation = esi_client().client.Assets.GetCharactersCharacterIdAssets(
        character_id=character_id, token=token
    )
    return [_to_dict(row) for row in operation.results(use_etag=False)]


def _fetch_asset_names(token, character_id: int, item_ids: list[int]) -> dict[int, str]:
    names: dict[int, str] = {}
    for chunk in _chunked(item_ids):
        try:
            # django-esi 9.x: POST body goes through `body=`, not the field name.
            rows = esi_client().client.Assets.PostCharactersCharacterIdAssetsNames(
                character_id=character_id,
                body=chunk,
                token=token,
            ).results(use_etag=False)
        except Exception as exc:  # pragma: no cover - network dependent
            if is_error_limited(exc):
                raise
            logger.warning(
                "Asset name lookup failed for character %s: %s: %s",
                character_id,
                type(exc).__name__,
                exc,
            )
            continue
        for row in rows:
            row = _to_dict(row)
            if row.get("name") and row["name"] != "None":
                names[row["item_id"]] = row["name"]
    return names


def _resolve_public_names(ids: set[int]) -> dict[int, str]:
    """Stations/systems via the public names endpoint; private structures stay opaque."""
    resolved: dict[int, str] = {}
    for chunk in _chunked([i for i in ids if i < 10**10]):
        try:
            # django-esi 9.x: POST body goes through `body=`, not the field name.
            rows = esi_client().client.Universe.PostUniverseNames(body=chunk).results(
                use_etag=False
            )
        except Exception as exc:  # pragma: no cover - mixed public/private ids 404 in bulk
            logger.info(
                "Public name resolution failed: %s: %s", type(exc).__name__, exc
            )
            continue
        for row in rows:
            row = _to_dict(row)
            resolved[row["id"]] = row["name"]
    return resolved


def _structure_tokens(character_ids) -> list:
    """Valid structure-scoped tokens for the given characters (any owner)."""
    from esi.models import Token

    char_ids = [c for c in character_ids if c]
    if not char_ids:
        return []
    return list(
        Token.objects.filter(character_id__in=char_ids)
        .require_scopes(STRUCTURE_SCOPES)
        .require_valid()
    )


def _resolve_structure(structure_id: int, tokens: list) -> tuple[str | None, int | None]:
    """(name, solar_system_id) for a private structure, or (None, None) when no
    token resolves it (no structure scope, or no docking access -> 403)."""
    for token in tokens:
        try:
            data = _to_dict(
                esi_client().client.Universe.GetUniverseStructuresStructureId(
                    structure_id=structure_id, token=token
                ).result(use_etag=False)
            )
            return data.get("name"), data.get("solar_system_id")
        except Exception as exc:  # pragma: no cover - 403/no-access or network dependent
            if is_error_limited(exc):
                raise
            continue
    return None, None


def _system_region(solar_system_id: int) -> tuple[str, str]:
    """(system_name, region_name) via eveuniverse, fetched + cached on demand."""
    if not solar_system_id:
        return "", ""
    from eveuniverse.models import EveSolarSystem

    try:
        system, _ = EveSolarSystem.objects.get_or_create_esi(id=solar_system_id)
        return system.name, system.eve_constellation.eve_region.name
    except Exception as exc:  # pragma: no cover - network dependent
        if is_error_limited(exc):
            raise
        return "", ""


def _resolve_locations(root_ids: set[int], structure_tokens: list) -> dict[int, dict]:
    """Map each root location id to {name, system, region}.

    NPC stations and solar systems resolve via eveuniverse (no token needed);
    private structures (Citadels, id >= 1e12) need a structure-scoped token and
    docking access, falling back to the bare id when unavailable."""
    from eveuniverse.models import EveStation

    out: dict[int, dict] = {}
    for loc_id in root_ids:
        name: str | None = None
        system_id: int | None = None
        if loc_id >= 10**12:
            name, system_id = _resolve_structure(loc_id, structure_tokens)
        elif 60_000_000 <= loc_id < 64_000_000:
            try:
                station, _ = EveStation.objects.get_or_create_esi(id=loc_id)
                name, system_id = station.name, station.eve_solar_system_id
            except Exception as exc:  # pragma: no cover - network dependent
                if is_error_limited(exc):
                    raise
                pass
        elif 30_000_000 <= loc_id < 33_000_000:
            system_id = loc_id  # the asset sits directly in a solar system
        system_name, region_name = _system_region(system_id) if system_id else ("", "")
        if name is None:
            name = system_name or f"Structure {loc_id}"
        out[loc_id] = {"name": name, "system": system_name, "region": region_name}
    return out


def _root_location(asset: dict, by_item_id: dict[int, dict]) -> int:
    """Walk containers up to the outermost location (station/structure/system)."""
    current = asset
    seen = set()
    while current["location_id"] in by_item_id and current["location_id"] not in seen:
        seen.add(current["location_id"])
        current = by_item_id[current["location_id"]]
    return current["location_id"]


def get_ship_inventory(user) -> ShipInventory:
    """Every assembled ship owned by any of the user's joined characters."""
    inventory = ShipInventory()
    tokens, _missing = user_tokens_by_character(user)

    characters = {
        o.character.character_id: o.character
        for o in user.character_ownerships.select_related("character")
    }
    structure_tokens = _structure_tokens(characters.keys())
    ship_type_ids = _ship_type_id_set()

    for character_id, character in characters.items():
        token = tokens.get(character_id)
        char_name = character.character_name if character else str(character_id)
        try:
            ships = resolve_ship_list(character_id, token, ship_type_ids)
        except Exception as exc:  # pragma: no cover - network dependent
            if is_error_limited(exc):
                logger.error("ESI error limited during inventory scan; aborting")
                inventory.error_limited = True
                break
            logger.warning("Asset fetch failed for %s: %s", char_name, exc)
            inventory.errors[char_name] = str(exc)
            continue
        if ships is None:
            # No granted token and no corptools cache for this character.
            inventory.characters_without_token.append(character)
            continue
        if not ships:
            continue
        try:
            inventory.ships.extend(
                _build_owned_ships(character_id, char_name, ships, token, structure_tokens)
            )
        except Exception as exc:  # pragma: no cover - network dependent
            if is_error_limited(exc):
                logger.error("ESI error limited resolving names/locations; aborting inventory scan")
                inventory.error_limited = True
                break
            raise
    inventory.ships.sort(key=lambda s: (s.character_name, s.group_name, s.type_name))
    return inventory


def _ship_group_names(type_ids: set[int]) -> dict[int, str]:
    """Ship class names (Battleship, Force Auxiliary, ...) via eveuniverse,
    loading unseen types from ESI on demand."""
    from eveuniverse.models import EveType

    names: dict[int, str] = {}
    known = EveType.objects.filter(id__in=type_ids).select_related("eve_group")
    for eve_type in known:
        names[eve_type.id] = eve_type.eve_group.name
    for type_id in type_ids - set(names):
        try:
            eve_type, _ = EveType.objects.get_or_create_esi(id=type_id)
            names[type_id] = eve_type.eve_group.name
        except Exception:  # pragma: no cover - network dependent
            names[type_id] = ""
    return names


def fit_items_from_flags(rows) -> list[FitItem]:
    """Convert (type_id, location_flag, quantity[, item_id]) rows into FitItems.

    Each row may be a 3-tuple ``(type_id, flag, quantity)`` or a 4-tuple that
    also carries the ESI asset ``item_id`` (kept on the FitItem so mutated-roll
    verification can match the exact fitted module). Charges sitting in slot
    flags (loaded ammo/scripts) are pooled into cargo, matching how the engine
    pools loaded charges on both sides.
    """
    slot_kinds = dict(
        SdeType.objects.filter(type_id__in={r[0] for r in rows}).values_list(
            "type_id", "slot_kind"
        )
    )
    items: list[FitItem] = []
    for row in rows:
        type_id, flag, quantity = row[0], row[1], row[2]
        item_id = row[3] if len(row) > 3 else None
        section = esi_flag_to_section(flag)
        if section is None:
            continue
        if section in SLOT_SECTIONS and slot_kinds.get(type_id) == SlotKind.CHARGE:
            section = Section.CARGO
        items.append(
            FitItem(
                section=section,
                type_id=type_id,
                quantity=quantity or 1,
                source_item_id=item_id,
            )
        )
    return items


def _verify_mutated_items(items: list[FitItem], asset_rows: list[dict]) -> None:
    """ESI-verify abyssal module rolls using the public dynamic-items endpoint.

    Rolls are matched to the exact fitted module by asset item_id, so two
    abyssal modules of the same type can carry independent rolls.
    """
    abyssal_ids = set(
        SdeType.objects.filter(
            type_id__in={i.type_id for i in items},
            meta_group_id=EveMetaGroupId.ABYSSAL,
        ).values_list("type_id", flat=True)
    )
    if not abyssal_ids:
        return
    to_verify = [r for r in asset_rows if r["type_id"] in abyssal_ids]
    if len(to_verify) > _MAX_DYNAMIC_ITEM_LOOKUPS:
        logger.warning(
            "Verifying only %s of %s abyssal modules on this ship (capping ESI fan-out); "
            "the rest stay unverified.",
            _MAX_DYNAMIC_ITEM_LOOKUPS, len(to_verify),
        )
        to_verify = to_verify[:_MAX_DYNAMIC_ITEM_LOOKUPS]
    rolls_by_item_id: dict[int, dict[int, float]] = {}
    for row in to_verify:
        try:
            data = esi_client().client.Dogma.GetDogmaDynamicItemsTypeIdItemId(
                type_id=row["type_id"], item_id=row["item_id"]
            ).result(use_etag=False)
        except Exception as exc:  # pragma: no cover - network dependent
            # Stop the whole scan cleanly if ESI is error-limited rather than
            # spending more of the budget; the bulk caller surfaces error_limited.
            if is_error_limited(exc):
                raise
            logger.info("Dynamic item lookup failed for item %s", row["item_id"])
            continue
        data = _to_dict(data)
        rolls_by_item_id[row["item_id"]] = {
            attr["attribute_id"]: attr["value"]
            for attr in (_to_dict(a) for a in data.get("dogma_attributes", []))
        }
    for item in items:
        rolls = rolls_by_item_id.get(item.source_item_id)
        if rolls is not None and item.mutated_attributes is None:
            item.mutated_attributes = rolls
            item.mutation_source = SubmissionItem.MutationSource.ESI_VERIFIED


def get_active_implants(character_id: int) -> set[int] | None:
    """Implant type_ids plugged into the character's active clone, via ESI.

    Returns None when no clones-scoped token exists for the character - the
    compliance engine then treats implants as unverifiable (a warning, not a
    miss) unless the site implant mode is set to Reject."""
    from esi.models import Token

    token = (
        Token.objects.filter(character_id=character_id)
        .require_scopes(CLONES_SCOPES)
        .require_valid()
        .first()
    )
    if token is None:
        return None
    try:
        rows = esi_client().client.Clones.GetCharactersCharacterIdImplants(
            character_id=character_id, token=token
        ).results(use_etag=False)
    except Exception as exc:  # pragma: no cover - network dependent
        if is_error_limited(exc):
            raise
        logger.warning("Implant fetch failed for character %s: %s", character_id, exc)
        return None
    return {int(r) for r in rows}


def build_parsed_fit(
    user,
    character_id: int,
    ship_item_id: int,
    *,
    assets: list[dict] | None = None,
    token=None,
    fit_name: str | None = None,
    fetch_implants: bool = False,
) -> ParsedFit | None:
    """Rebuild a ship's fitted state from the owner's asset tree.

    A bulk scan that already fetched the character's assets (and token, and ship
    name) passes them in so this does NOT re-fetch the whole asset tree per ship
    - the single biggest ESI-call saving on an alliance-wide scan."""
    if token is None:
        tokens, _missing = user_tokens_by_character(user)
        token = tokens.get(character_id)
    if assets is None:
        assets = resolve_assets(character_id, token)
    if assets is None:
        return None
    by_item_id = {a["item_id"]: a for a in assets}
    ship = by_item_id.get(ship_item_id)
    if ship is None:
        return None

    fitted = [a for a in assets if a["location_id"] == ship_item_id]
    rows = [
        (a["type_id"], a.get("location_flag", ""), a.get("quantity", 1), a["item_id"])
        for a in fitted
    ]
    items = fit_items_from_flags(rows)
    _verify_mutated_items(items, fitted)

    # Frigate Escape Bay: battleships / navy BS / black ops / marauders carry
    # at most one frigate in this bay. Informational only - not fed to the
    # compliance engine. If CCP changes the flag name, this just stays None.
    feb_type_id = next(
        (a["type_id"] for a in fitted if a.get("location_flag") == "FrigateEscapeBay"),
        None,
    )
    if feb_type_id is not None:
        logger.debug(
            "Detected FrigateEscapeBay contents for ship %s: type_id=%s",
            ship_item_id, feb_type_id,
        )

    if fit_name is None:
        if token is not None:
            ship_names = _fetch_asset_names(token, character_id, [ship_item_id])
            fit_name = ship_names.get(ship_item_id, f"Ship {ship_item_id}")
        else:
            # corptools-served: fall back to the cached custom ship name.
            fit_name = ship.get("name") or f"Ship {ship_item_id}"
    return ParsedFit(
        ship_type_id=ship["type_id"],
        fit_name=fit_name,
        items=items,
        frigate_escape_bay_type_id=feb_type_id,
        source_ship_item_id=ship_item_id,
        pilot_implant_type_ids=get_active_implants(character_id) if fetch_implants else None,
    )
