"""Edit a published fitting's module list (BOM) with safe versioning.

When a manager replaces a fit's EFT, we:
  1. archive the about-to-be-replaced version (view-only retrieval),
  2. rebuild the items from the new EFT, and
  3. carry the old per-item policy + overrides forward onto matching modules
     (keyed by ``(section, module_type_id)``) so only changed modules need
     fresh exception management.

The carry-forward helpers are shared with the colcrunch re-sync path
(`fittings_import._refresh_fit_from_plugin`) so both preserve per-item policy,
not just overrides.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from ..models import ArchivedFitVersion, DoctrineFit, DoctrineFitItem, FitItemOverride
from .assignments import capture_assignment_policies, rebuild_assignment_snapshots
from .doctrine_import import (
    DoctrineImportError,
    _get_or_create_eve_type,
    _materialise_items,
)
from .eft_parser import parse_eft
from .fit_data import ParsedFit

# Per-item policy fields carried across a BOM change. Excludes section/type/qty
# (the BOM itself) and charge (re-derived from the new EFT).
POLICY_FIELDS = (
    "policy",
    "min_meta_level",
    "allowed_meta_groups",
    "checked_attributes",
    "attribute_bounds",
    "allow_mutated",
    "min_quantity_pct",
    "notes",
)


@dataclass
class UpdateResult:
    """What happened to per-module policy across a BOM update, for user feedback."""

    carried: list[str] = field(default_factory=list)  # kept old policy
    added: list[str] = field(default_factory=list)  # new modules, default policy
    dropped: list[str] = field(default_factory=list)  # removed modules


def capture_fit_items(fit: DoctrineFit) -> dict[tuple, dict]:
    """Snapshot every item's policy + overrides keyed by ``(section, type_id)``.

    Used both to populate the archive snapshot and to carry policy forward."""
    captured: dict[tuple, dict] = {}
    for item in fit.items.select_related("module_type").prefetch_related("overrides"):
        key = (item.section, item.module_type_id)
        captured[key] = {
            "section": item.section,
            "type_id": item.module_type_id,
            "name": item.module_type.name,
            "qty": item.quantity,
            "charge_type_id": item.charge_type_id,
            "policy_fields": {f: getattr(item, f) for f in POLICY_FIELDS},
            "overrides": [
                {"alt_type_id": o.alt_type_id, "mode": o.mode} for o in item.overrides.all()
            ],
        }
    return captured


def apply_captured_policy(fit: DoctrineFit, captured: dict[tuple, dict]) -> tuple[list, list]:
    """Re-apply a captured policy map onto `fit`'s current items by
    ``(section, type_id)``. Returns ``(carried_keys, added_keys)``."""
    new_items = list(fit.items.select_related("module_type"))
    carried_keys: list[tuple] = []
    added_keys: list[tuple] = []
    to_update: list[DoctrineFitItem] = []
    for item in new_items:
        key = (item.section, item.module_type_id)
        saved = captured.get(key)
        if saved is None:
            added_keys.append(key)
            continue
        for fname, value in saved["policy_fields"].items():
            setattr(item, fname, value)
        to_update.append(item)
        carried_keys.append(key)
    if to_update:
        DoctrineFitItem.objects.bulk_update(to_update, list(POLICY_FIELDS))

    # Re-create overrides for modules that survived.
    by_key = {(i.section, i.module_type_id): i for i in new_items}
    for key in carried_keys:
        host = by_key[key]
        for ovr in captured[key]["overrides"]:
            FitItemOverride.objects.update_or_create(
                item=host, alt_type_id=ovr["alt_type_id"], defaults={"mode": ovr["mode"]}
            )
    return carried_keys, added_keys


def _snapshot_json(captured: dict[tuple, dict]) -> dict:
    """JSON-serialisable archive payload built from a captured policy map."""
    items = []
    for saved in captured.values():
        row = {
            "section": saved["section"],
            "type_id": saved["type_id"],
            "name": saved["name"],
            "qty": saved["qty"],
            "charge_type_id": saved["charge_type_id"],
            "overrides": saved["overrides"],
        }
        row.update(saved["policy_fields"])
        items.append(row)
    return {"items": items}


@transaction.atomic
def update_fit_bom(
    fit: DoctrineFit,
    new_eft: str,
    user: User | None = None,
    *,
    parsed: ParsedFit | None = None,
) -> UpdateResult:
    """Replace `fit`'s BOM from `new_eft`, archiving the old version and carrying
    per-item policy forward. Raises DoctrineImportError on unparseable input."""
    parsed = parsed or parse_eft(new_eft)
    if parsed.errors or parsed.ship_type_id is None:
        raise DoctrineImportError(
            "The fit could not be parsed.",
            errors=[f"Line {e.line_no}: '{e.text}' - {e.reason}" for e in parsed.errors],
        )

    captured = capture_fit_items(fit)
    # Per-doctrine snapshots FK to the source items via CASCADE, so the BOM
    # delete below wipes them; capture them now to replay after the rebuild.
    assignment_captures = capture_assignment_policies(fit)

    # Archive the version we're about to overwrite (view-only retrieval).
    ArchivedFitVersion.objects.create(
        fit=fit,
        version=fit.version,
        eft_source=fit.eft_source,
        ship_type_id=fit.ship_type_id,
        policy_snapshot=_snapshot_json(captured),
        archived_by=user,
    )

    # Rebuild the BOM from the new EFT.
    fit.eft_source = new_eft
    fit.ship_type = _get_or_create_eve_type(parsed.ship_type_id)
    fit.last_imported_by = user
    fit.bom_updated_at = timezone.now()
    DoctrineFitItem.objects.filter(fit=fit).delete()
    _materialise_items(fit, parsed)

    carried_keys, added_keys = apply_captured_policy(fit, captured)
    # Rebuild every per-doctrine snapshot from the new source items, carrying
    # each assignment's own exceptions forward by (section, type).
    rebuild_assignment_snapshots(fit, assignment_captures)

    fit.save(
        update_fields=["eft_source", "ship_type", "last_imported_by", "bom_updated_at"]
    )
    fit.bump_version()  # marks existing pending submissions stale

    surviving = set(carried_keys)
    dropped = [
        saved["name"] for key, saved in captured.items() if key not in surviving
    ]
    name_of = {key: saved["name"] for key, saved in captured.items()}
    new_by_key = {(i.section, i.module_type_id): i for i in fit.items.select_related("module_type")}
    return UpdateResult(
        carried=sorted(name_of[k] for k in carried_keys),
        added=sorted(new_by_key[k].module_type.name for k in added_keys),
        dropped=sorted(dropped),
    )
