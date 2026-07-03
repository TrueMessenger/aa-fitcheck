"""Compact old->new BOM comparison for stale submissions.

When a fitting's BOM is replaced (Update Fit / plugin resync) the superseded
version is captured as an ``ArchivedFitVersion``; a submission graded before
the change can therefore show the pilot exactly which modules moved. A
policy-only version bump creates no archive - there is nothing to diff, and
callers fall back to a generic "the fit's rules changed" notice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..constants import Section
from ..models import ArchivedFitVersion, DoctrineFit, FitSubmission, SdeType


@dataclass
class DiffEntry:
    """One module-level difference between the archived and current BOM."""

    section: str
    section_label: str
    name: str
    old_qty: int | None = None
    new_qty: int | None = None
    old_charge: str | None = None
    new_charge: str | None = None


@dataclass
class BomDiff:
    old_version: int
    new_version: int
    added: list[DiffEntry] = field(default_factory=list)
    removed: list[DiffEntry] = field(default_factory=list)
    changed: list[DiffEntry] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def _section_label(section: str) -> str:
    try:
        return str(Section(section).label)
    except ValueError:
        return section


def _charge_names(type_ids: set[int]) -> dict[int, str]:
    """Charge names from the local SDE mirror (charges are in the loaded slice)."""
    if not type_ids:
        return {}
    names = dict(
        SdeType.objects.filter(type_id__in=type_ids).values_list("type_id", "name")
    )
    return {tid: names.get(tid, f"Type {tid}") for tid in type_ids}


def bom_diff(old_items: list[dict], fit: DoctrineFit) -> BomDiff:
    """Compare an archive's ``policy_snapshot["items"]`` against ``fit``'s
    current BOM, keyed by ``(section, type_id)``: added / removed modules,
    quantity changes, and loaded-charge swaps on a surviving module."""
    old: dict[tuple[str, int], dict] = {
        (row.get("section", ""), row.get("type_id")): row for row in old_items
    }
    new: dict[tuple[str, int], dict] = {
        (item.section, item.module_type_id): {
            "name": item.module_type.name,
            "qty": item.quantity,
            "charge_type_id": item.charge_type_id,
        }
        for item in fit.items.select_related("module_type")
    }

    charge_ids = {
        row.get("charge_type_id")
        for key in old.keys() & new.keys()
        for row in (old[key], new[key])
        if old[key].get("charge_type_id") != new[key].get("charge_type_id")
        and row.get("charge_type_id")
    }
    charge_name = _charge_names(charge_ids)

    diff = BomDiff(old_version=0, new_version=fit.version)
    for key in sorted(new.keys() - old.keys()):
        section, type_id = key
        row = new[key]
        diff.added.append(
            DiffEntry(
                section=section,
                section_label=_section_label(section),
                name=row["name"],
                new_qty=row["qty"],
            )
        )
    for key in sorted(old.keys() - new.keys()):
        section, type_id = key
        row = old[key]
        diff.removed.append(
            DiffEntry(
                section=section,
                section_label=_section_label(section),
                name=row.get("name") or f"Type {type_id}",
                old_qty=row.get("qty"),
            )
        )
    for key in sorted(old.keys() & new.keys()):
        section, type_id = key
        old_row, new_row = old[key], new[key]
        qty_changed = (old_row.get("qty") or 0) != new_row["qty"]
        old_charge_id = old_row.get("charge_type_id")
        new_charge_id = new_row.get("charge_type_id")
        charge_changed = old_charge_id != new_charge_id
        if not (qty_changed or charge_changed):
            continue
        diff.changed.append(
            DiffEntry(
                section=section,
                section_label=_section_label(section),
                name=new_row["name"],
                old_qty=old_row.get("qty") if qty_changed else None,
                new_qty=new_row["qty"] if qty_changed else None,
                old_charge=(
                    charge_name.get(old_charge_id) if charge_changed and old_charge_id else None
                ),
                new_charge=(
                    charge_name.get(new_charge_id) if charge_changed and new_charge_id else None
                ),
            )
        )
    return diff


def archive_for_version(fit: DoctrineFit, fit_version: int) -> ArchivedFitVersion | None:
    """The archive holding the BOM a submission at ``fit_version`` was graded
    against: the earliest archive captured at or after that version (the BOM
    is unchanged across the policy-only bumps in between). ``None`` when the
    BOM never changed since - policy-only staleness."""
    return (
        fit.archives.filter(version__gte=fit_version).order_by("version").first()
    )


def diff_for_submission(submission: FitSubmission) -> BomDiff | None:
    """Old->new module diff for a stale submission, or ``None`` when no
    archived BOM covers it (the staleness came from policy edits alone)."""
    fit = submission.doctrine_fit
    archive = archive_for_version(fit, submission.fit_version)
    if archive is None:
        return None
    diff = bom_diff(archive.policy_snapshot.get("items", []), fit)
    diff.old_version = submission.fit_version
    return diff
