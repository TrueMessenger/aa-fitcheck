"""Read-only diagnostics shared by the ``fitcheck_inventory_doctor`` CLI command
and the admin Diagnostics page.

Everything here is strictly read-only (DB + corptools cache). The per-character
inventory report makes NO ESI calls unless ``with_esi=True`` is passed (CLI only);
the web view always calls it DB-only.
"""

from __future__ import annotations

from . import corptools_source, esi_assets


def resolve_character(ident: str):
    """An EveCharacter by character_id (digits) or exact name, or None."""
    from allianceauth.eveonline.models import EveCharacter

    ident = (ident or "").strip()
    if not ident:
        return None
    if ident.isdigit():
        return EveCharacter.objects.filter(character_id=int(ident)).first()
    return EveCharacter.objects.filter(character_name=ident).first()


def corptools_version() -> str | None:
    if not corptools_source.corptools_installed():
        return None
    try:
        import corptools

        return getattr(corptools, "__version__", None)
    except Exception:  # pragma: no cover - import guard
        return None


def _asset_token_for(character_id: int):
    """A valid asset-scope token for this character under any user, or None."""
    from esi.models import Token

    return (
        Token.objects.filter(character_id=character_id)
        .require_scopes(esi_assets.ASSET_SCOPES)
        .require_valid()
        .first()
    )


def inventory_report(character_id: int, *, with_esi: bool = False) -> dict:
    """Per-layer breakdown of why a character's ships do/don't surface in My Ships.

    Read-only; no ESI unless ``with_esi`` is True. Returns a dict the CLI prints
    and the web template renders, including a derived ``verdict`` naming the layer
    that explains a "0 ships" result.
    """
    ship_set = esi_assets._ship_type_id_set()
    report: dict = {
        "character_id": character_id,
        "sde_ship_types": len(ship_set),
        "asset_source": esi_assets._asset_source(),
        "corptools": {
            "installed": corptools_source.corptools_installed(),
            "version": corptools_version(),
            "audit_found": None,
            "assets_synced_at": None,
            "ship_rows_all": None,
            "ship_rows_in_sde": None,
            "ship_rows_sde_filtered": None,
            "sample_type_ids": [],
        },
        "token_present": _asset_token_for(character_id) is not None,
        "esi": {"ran": False, "assets": None, "ships": None, "error": None},
    }
    ct = report["corptools"]

    if ct["installed"]:
        ct["audit_found"] = corptools_source._audit_for(character_id) is not None
        ct["assets_synced_at"] = corptools_source.assets_synced_at(character_id)
        all_rows = corptools_source.ship_assets_for_character(character_id, None)
        if all_rows is None:
            ct["ship_rows_all"] = None  # not servable
        else:
            ct["ship_rows_all"] = len(all_rows)
            ct["ship_rows_in_sde"] = len(
                [r for r in all_rows if r["type_id"] in ship_set]
            )
            ct["sample_type_ids"] = sorted({r["type_id"] for r in all_rows})[:15]
        filtered = corptools_source.ship_assets_for_character(character_id, ship_set)
        ct["ship_rows_sde_filtered"] = None if filtered is None else len(filtered)

    if with_esi:
        token = _asset_token_for(character_id)
        if token is None:
            report["esi"]["error"] = "no valid asset-scope token"
        else:
            try:
                assets = esi_assets._fetch_assets(token, character_id)
                ships = [
                    a for a in assets
                    if a.get("type_id") in ship_set and a.get("is_singleton")
                ]
                report["esi"] = {
                    "ran": True,
                    "assets": len(assets),
                    "ships": len(ships),
                    "error": None,
                }
            except Exception as exc:  # noqa: BLE001 - diagnostic surface
                report["esi"] = {
                    "ran": True,
                    "assets": None,
                    "ships": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }

    report["verdict"] = _verdict(report)
    return report


def _verdict(r: dict) -> str:
    ct = r["corptools"]
    if not ct["installed"]:
        served = "uses live ESI (corptools not installed)"
        return (
            f"corptools not installed - My Ships {served}; "
            + ("a valid asset token is present." if r["token_present"]
               else "NO valid asset token for this character, so live ESI returns nothing.")
        )
    if not ct["audit_found"]:
        return "corptools is installed but has no audit for this character - falls back to live ESI."
    if ct["assets_synced_at"] is None:
        return (
            "corptools audit exists but assets are NOT synced (assets_synced_at is empty) - "
            "fitcheck bypasses the cache and falls back to live ESI."
        )
    if ct["ship_rows_all"] in (None, 0):
        return (
            "corptools is serving this character but holds NO assembled (singleton) ships - "
            "packaged ships and pods do not count."
        )
    if ct["ship_rows_sde_filtered"] == 0:
        return (
            f"corptools holds {ct['ship_rows_all']} assembled ship(s), but none match the SDE "
            "ship whitelist - the SDE mirror is missing those hull type_ids (reload the SDE)."
        )
    return (
        f"corptools is serving {ct['ship_rows_sde_filtered']} ship(s) for this character - "
        "My Ships should list them."
    )


def health_summary() -> dict:
    """App-critical baseline status for the admin health panel. Read-only; no
    network (does not check the remote SDE build)."""
    from .. import __version__, checks
    from ..app_settings import FITCHECK_STRUCTURE_CACHE_TTL
    from ..constants import EveCategoryId
    from ..models import (
        Doctrine,
        DoctrineFit,
        EnforcementSettings,
        FitSubmission,
        SdeLoadRecord,
        SdeType,
        StructureNameCache,
    )

    latest_sde = SdeLoadRecord.objects.order_by("-loaded_at").first()
    cat_counts = {
        "ship": SdeType.objects.filter(category_id=EveCategoryId.SHIP).count(),
        "module": SdeType.objects.filter(category_id=EveCategoryId.MODULE).count(),
        "charge": SdeType.objects.filter(category_id=EveCategoryId.CHARGE).count(),
    }

    deploy_warnings = [
        {"id": w.id, "msg": w.msg, "hint": w.hint}
        for w in (checks.sde_mirror_loaded_check(None) + checks.structure_name_task_check(None))
    ]

    last_structure_attempt = (
        StructureNameCache.objects.filter(last_attempt_at__isnull=False)
        .order_by("-last_attempt_at")
        .values_list("last_attempt_at", flat=True)
        .first()
    )

    try:
        from django.db.models import F

        stale_pending = (
            FitSubmission.objects.filter(status="P")
            .filter(fit_version__lt=F("doctrine_fit__version"))
            .count()
        )
    except Exception:  # pragma: no cover - schema guard
        stale_pending = None

    return {
        "fitcheck_version": __version__,
        "corptools_installed": corptools_source.corptools_installed(),
        "corptools_version": corptools_version(),
        "asset_source": esi_assets._asset_source(),
        "sde_loaded": SdeType.objects.filter(category_id=EveCategoryId.SHIP).exists(),
        "sde_latest": latest_sde,
        "sde_type_total": SdeType.objects.count(),
        "sde_category_counts": cat_counts,
        "deploy_warnings": deploy_warnings,
        "structure_pending": StructureNameCache.objects.filter(resolved_at__isnull=True).count(),
        "structure_resolved": StructureNameCache.objects.filter(resolved_at__isnull=False).count(),
        "structure_inaccessible": StructureNameCache.objects.filter(accessible=False).count(),
        "structure_last_attempt": last_structure_attempt,
        "structure_cache_ttl_hours": round(FITCHECK_STRUCTURE_CACHE_TTL / 3600, 1),
        "enforcement": EnforcementSettings.current(),
        "pending_submissions": FitSubmission.objects.filter(status="P").count(),
        "stale_pending": stale_pending,
        "active_doctrines": Doctrine.objects.filter(is_active=True).count(),
        "active_fits": DoctrineFit.objects.filter(is_active=True).count(),
    }
