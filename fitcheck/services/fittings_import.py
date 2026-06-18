"""Soft-dependency import from the community `fittings` plugin (colcrunch).

When that app is installed alongside fitcheck, its doctrines and fittings can
be pulled in as fitcheck doctrines + fitting standards in one click. Nothing
here imports `fittings` at module load - everything resolves through the app
registry so fitcheck runs fine without it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.apps import apps as django_apps
from django.db import transaction
from django.utils import timezone

from ..models import Doctrine, DoctrineFit
from .doctrine_import import import_fit
from .eft_parser import render_eft
from .esi_assets import fit_items_from_flags
from .fit_data import ParsedFit

logger = logging.getLogger(__name__)


def fittings_installed() -> bool:
    return django_apps.is_installed("fittings")


def _plugin_models():
    return (
        django_apps.get_model("fittings", "Doctrine"),
        django_apps.get_model("fittings", "Fitting"),
    )


def _plugin_category_model():
    """The colcrunch Category model, or None if this `fittings` version lacks it."""
    try:
        return django_apps.get_model("fittings", "Category")
    except LookupError:  # pragma: no cover - depends on installed fittings version
        return None


def _hex_color(value, default: str = "#0d6efd") -> str:
    """Normalise a colcrunch colour to `#rrggbb`; fall back when it isn't hex."""
    if not value:
        return default
    body = str(value).strip().lstrip("#")
    if len(body) == 6 and all(c in "0123456789abcdefABCDEF" for c in body):
        return "#" + body.lower()
    return default


def _plugin_categories_for(plugin_doctrines=(), plugin_fits=()) -> list:
    """Distinct colcrunch Category rows attached to the given plugin doctrines
    or fittings. colcrunch names the M2M `category` on both models."""
    seen: dict = {}
    for obj in list(plugin_doctrines) + list(plugin_fits):
        manager = getattr(obj, "category", None)
        if manager is None:
            continue
        for category in manager.all():
            seen[category.pk] = category
    return list(seen.values())


def _sync_one_category(plugin_category):
    """Authoritatively mirror one colcrunch Category into a `DoctrineCategory`:
    match by `source_plugin_pk` (fallback by name), then OVERWRITE colour,
    group-gating and membership from colcrunch each run. Only categories we own
    by source link are ever touched - purely-local categories are never visited
    because we only iterate colcrunch's categories. Returns (category, created)."""
    from ..models import Doctrine, DoctrineCategory, DoctrineFit

    name = (plugin_category.name or "")[:30]
    category = (
        DoctrineCategory.objects.filter(source_plugin_pk=plugin_category.pk).first()
        or DoctrineCategory.objects.filter(name=name).first()
    )
    created = category is None
    if created:
        category = DoctrineCategory(name=name)
    category.source_plugin_pk = plugin_category.pk
    category.name = name
    category.color = _hex_color(getattr(plugin_category, "color", None), category.color)
    category.save()

    # colcrunch access = "member of ANY of these groups" -> Selected-OR.
    category.selected_groups.set(plugin_category.groups.all())
    category.required_groups.clear()

    # Membership: only the doctrines/fits we imported (matched by
    # source_plugin_pk) - never attach to rows we don't own.
    doctrine_pks = [d.pk for d in plugin_category.doctrines.all()]
    fit_pks = [f.pk for f in plugin_category.fittings.all()]
    category.doctrines.set(Doctrine.objects.filter(source_plugin_pk__in=doctrine_pks))
    category.fits.set(DoctrineFit.objects.filter(source_plugin_pk__in=fit_pks))
    return category, created


def _sync_categories(plugin_doctrines=(), plugin_fits=()) -> list[str]:
    """Sync every colcrunch Category touching these plugin doctrines/fits.
    No-op (returns []) when the installed `fittings` has no Category model.
    Returns the names of the categories synced."""
    if _plugin_category_model() is None:
        return []
    synced = []
    for plugin_category in _plugin_categories_for(plugin_doctrines, plugin_fits):
        category, _created = _sync_one_category(plugin_category)
        synced.append(category.name)
    return synced


def _fit_ship_type_id(plugin_fit) -> int | None:
    """The plugin stores the hull either as a raw id or an FK, depending on version."""
    for attr in ("ship_type_type_id", "ship_type_id"):
        value = getattr(plugin_fit, attr, None)
        if isinstance(value, int):
            return value
    return None


def _item_type_id(plugin_item) -> int | None:
    for attr in ("type_id", "type_fk_id"):
        value = getattr(plugin_item, attr, None)
        if isinstance(value, int):
            return value
    return None


def list_plugin_doctrines() -> list[dict]:
    """Plugin doctrines with their fits, plus already-imported markers."""
    if not fittings_installed():
        return []
    PluginDoctrine, _PluginFitting = _plugin_models()
    existing = set(Doctrine.objects.values_list("name", flat=True))
    result = []
    for doctrine in PluginDoctrine.objects.all().prefetch_related("fittings"):
        fits = [
            {
                "id": fit.pk,
                "name": fit.name,
                "ship_type_id": _fit_ship_type_id(fit),
            }
            for fit in doctrine.fittings.all()
        ]
        result.append(
            {
                "id": doctrine.pk,
                "name": doctrine.name,
                "fit_count": len(fits),
                "fits": fits,
                "already_imported": doctrine.name in existing,
            }
        )
    return result


def convert_plugin_fit(plugin_fit) -> ParsedFit | None:
    """Plugin fitting -> engine ParsedFit via its ESI-style location flags."""
    ship_type_id = _fit_ship_type_id(plugin_fit)
    if ship_type_id is None:
        return None
    rows = []
    for item in plugin_fit.items.all():
        type_id = _item_type_id(item)
        if type_id is None:
            continue
        rows.append((type_id, getattr(item, "flag", "") or "", getattr(item, "quantity", 1)))
    return ParsedFit(
        ship_type_id=ship_type_id,
        fit_name=plugin_fit.name,
        items=fit_items_from_flags(rows),
    )


@dataclass
class ImportReport:
    doctrines_created: list[str] = field(default_factory=list)
    fits_created: list[str] = field(default_factory=list)
    fits_linked: list[str] = field(default_factory=list)
    categories_synced: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def imported_anything(self) -> bool:
        return bool(self.doctrines_created or self.fits_created or self.fits_linked)


def _import_one_fit(plugin_fit, user, doctrine: Doctrine | None, report: ImportReport):
    parsed = convert_plugin_fit(plugin_fit)
    if parsed is None or not parsed.items:
        report.skipped.append(f"{plugin_fit.name} (could not be converted)")
        return
    # Prefer matching by source_plugin_pk - it's stable across renames.
    existing = (
        DoctrineFit.objects.filter(source_plugin_pk=plugin_fit.pk).first()
        or DoctrineFit.objects.filter(
            name=plugin_fit.name, ship_type_id=parsed.ship_type_id
        ).first()
    )
    if existing is not None:
        # Backfill the source pk if this fit was matched by name only.
        if existing.source_plugin_pk is None:
            existing.source_plugin_pk = plugin_fit.pk
            existing.save(update_fields=["source_plugin_pk"])
        if doctrine is not None and not existing.doctrines.filter(pk=doctrine.pk).exists():
            from .assignments import attach_fit_to_doctrine

            attach_fit_to_doctrine(existing, doctrine, user=user)
            report.fits_linked.append(existing.name)
        else:
            report.skipped.append(f"{plugin_fit.name} (already imported)")
        return
    fit = import_fit(
        render_eft(parsed), user, doctrine=doctrine, name=plugin_fit.name, parsed=parsed
    )
    fit.source_plugin_pk = plugin_fit.pk
    fit.save(update_fields=["source_plugin_pk"])
    report.fits_created.append(fit.name)


@transaction.atomic
def import_plugin_doctrines(user, doctrine_ids: list[int]) -> ImportReport:
    """Import selected plugin doctrines with all their fittings."""
    report = ImportReport()
    if not fittings_installed():
        return report
    PluginDoctrine, _PluginFitting = _plugin_models()
    processed_doctrines = []
    processed_fits = []
    for plugin_doctrine in PluginDoctrine.objects.filter(pk__in=doctrine_ids).prefetch_related(
        "fittings__items"
    ):
        processed_doctrines.append(plugin_doctrine)
        processed_fits.extend(plugin_doctrine.fittings.all())
        # Match by source_plugin_pk if we have one (stable across renames),
        # else fall back to name.
        doctrine = Doctrine.objects.filter(source_plugin_pk=plugin_doctrine.pk).first()
        created = False
        if doctrine is None:
            doctrine, created = Doctrine.objects.get_or_create(
                name=plugin_doctrine.name,
                defaults={
                    "description": getattr(plugin_doctrine, "description", "") or "",
                    "created_by": user,
                    "source_plugin_pk": plugin_doctrine.pk,
                },
            )
            if created:
                report.doctrines_created.append(doctrine.name)
            elif doctrine.source_plugin_pk is None:
                doctrine.source_plugin_pk = plugin_doctrine.pk
                doctrine.save(update_fields=["source_plugin_pk"])
        for plugin_fit in plugin_doctrine.fittings.all():
            _import_one_fit(plugin_fit, user, doctrine, report)
        if created and doctrine.image_type_id is None:
            first = doctrine.fits.first()
            if first:
                doctrine.image_type_id = first.ship_type_id
                doctrine.save(update_fields=["image_type_id"])
    # Authoritatively sync the colcrunch categories gating what we imported.
    report.categories_synced = _sync_categories(processed_doctrines, processed_fits)
    return report


# --------------------------------------------------------------- re-sync ---


@dataclass
class ResyncReport:
    """Counts of what changed when pulling updates from the fittings plugin."""

    name: str = ""
    fits_added: list[str] = field(default_factory=list)
    fits_updated: list[str] = field(default_factory=list)
    fits_dropped: list[str] = field(default_factory=list)
    categories_synced: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def changed_anything(self) -> bool:
        return bool(self.fits_added or self.fits_updated or self.fits_dropped)


def _refresh_fit_from_plugin(fit: DoctrineFit, plugin_fit, user) -> bool:
    """Replace `fit`'s items + eft_source from the plugin fit. Preserves the
    fit's per-item policy + overrides, compliance_policy FK, strict_extras,
    default_policy and doctrine assignments - only the BOM gets refreshed.
    Returns True if the contents actually changed (we compare module_type_id
    sets), False otherwise. Per-item policy and FitItemOverride rows are carried
    forward by (section, module_type_id) so rules survive a refresh, and
    silently drop when their module disappears."""
    from ..models import DoctrineFitItem
    from .assignments import capture_assignment_policies, rebuild_assignment_snapshots
    from .doctrine_import import _materialise_items
    from .fit_edit import apply_captured_policy, capture_fit_items

    parsed = convert_plugin_fit(plugin_fit)
    if parsed is None or not parsed.items:
        return False

    # Plugin items come in one-per-slot (LoSlot0, LoSlot1, ...) while we
    # store them merged. Sum into (section, type_id) buckets on both sides
    # before comparing so a same-content fit registers as unchanged.
    def _bucket(rows):
        out: dict[tuple, int] = {}
        for section, type_id, qty in rows:
            key = (section, type_id)
            out[key] = out.get(key, 0) + (qty or 1)
        return out

    old_signature = _bucket(fit.items.values_list("section", "module_type_id", "quantity"))
    new_signature = _bucket((i.section, i.type_id, i.quantity) for i in parsed.items)
    if old_signature == new_signature and fit.ship_type_id == parsed.ship_type_id:
        return False

    # Snapshot per-item policy + overrides so they carry forward onto the
    # rebuilt items (same shared helper the manual BOM-edit path uses).
    captured = capture_fit_items(fit)
    # And the per-doctrine snapshots, which the source-item delete CASCADEs away.
    assignment_captures = capture_assignment_policies(fit)

    # Replace the EFT source + re-import the items via the existing pipeline.
    fit.eft_source = render_eft(parsed)
    if fit.ship_type_id != parsed.ship_type_id:
        from eveuniverse.models import EveType

        eve_type, _ = EveType.objects.get_or_create_esi(id=parsed.ship_type_id)
        fit.ship_type = eve_type
    fit.last_imported_by = user
    fit.bom_updated_at = timezone.now()
    DoctrineFitItem.objects.filter(fit=fit).delete()

    _materialise_items(fit, parsed)
    apply_captured_policy(fit, captured)
    rebuild_assignment_snapshots(fit, assignment_captures)
    fit.save(
        update_fields=["eft_source", "ship_type", "last_imported_by", "bom_updated_at"]
    )
    fit.bump_version()
    return True


@transaction.atomic
def resync_doctrine_from_plugin(doctrine: Doctrine, user) -> ResyncReport:
    """Pull updates for one doctrine from the colcrunch `fittings` plugin.

    Re-uses the source_plugin_pk on the doctrine and each fit to match rows
    so renames don't break the link. Preserves our policy data (per-item
    overrides, compliance_policy, default_policy, doctrine targeting); only
    the fit's BOM and the doctrine's membership get refreshed."""
    report = ResyncReport(name=doctrine.name)
    if not fittings_installed():
        report.error = "fittings plugin not installed"
        return report
    if doctrine.source_plugin_pk is None:
        report.error = "doctrine was not imported from the fittings plugin"
        return report
    PluginDoctrine, PluginFitting = _plugin_models()
    plugin_doctrine = PluginDoctrine.objects.filter(pk=doctrine.source_plugin_pk).first()
    if plugin_doctrine is None:
        report.error = "source doctrine no longer exists in the fittings plugin"
        return report

    plugin_fits_by_pk = {pf.pk: pf for pf in plugin_doctrine.fittings.all()}
    our_fits_by_pk = {f.source_plugin_pk: f for f in doctrine.fits.all() if f.source_plugin_pk}

    # New plugin-side fits land as fresh imports linked to this doctrine.
    for plugin_pk, plugin_fit in plugin_fits_by_pk.items():
        if plugin_pk in our_fits_by_pk:
            continue
        sub_report = ImportReport()
        _import_one_fit(plugin_fit, user, doctrine, sub_report)
        report.fits_added.extend(sub_report.fits_created)

    # Existing fits: refresh BOM, leaving policies/overrides intact.
    for plugin_pk, fit in our_fits_by_pk.items():
        plugin_fit = plugin_fits_by_pk.get(plugin_pk)
        if plugin_fit is None:
            # Plugin dropped this fit from the doctrine. Detach (but keep
            # the standalone DoctrineFit alive - users may still want it).
            from .assignments import detach_fit_from_doctrine

            detach_fit_from_doctrine(fit, doctrine)
            report.fits_dropped.append(fit.name)
            continue
        if _refresh_fit_from_plugin(fit, plugin_fit, user):
            report.fits_updated.append(fit.name)
        else:
            report.unchanged.append(fit.name)

    # Refresh doctrine metadata that's safe to overwrite.
    if hasattr(plugin_doctrine, "description"):
        plugin_desc = getattr(plugin_doctrine, "description", "") or ""
        if plugin_desc and plugin_desc != doctrine.description:
            doctrine.description = plugin_desc
            doctrine.save(update_fields=["description"])
    # Authoritatively re-sync the colcrunch categories gating this doctrine and
    # its fittings (overwrites colour, group-gating and membership each run).
    report.categories_synced = _sync_categories(
        [plugin_doctrine], list(plugin_fits_by_pk.values())
    )
    return report


@transaction.atomic
def import_plugin_baseline_fits(user) -> ImportReport:
    """Import every plugin fitting that belongs to no doctrine, as standalone standards."""
    report = ImportReport()
    if not fittings_installed():
        return report
    _PluginDoctrine, PluginFitting = _plugin_models()
    orphans = list(
        PluginFitting.objects.filter(doctrines__isnull=True).prefetch_related("items")
    )
    for plugin_fit in orphans:
        _import_one_fit(plugin_fit, user, None, report)
    # Sync any colcrunch categories gating these standalone fittings.
    report.categories_synced = _sync_categories(plugin_fits=orphans)
    return report
