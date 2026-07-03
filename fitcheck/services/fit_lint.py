"""Import-time sanity lint for fitting standards. Warn-only by design: a
doctrine that exceeds the hull's slot layout is almost always a mangled EFT
paste, but the manager stays in charge - nothing is rejected."""

from __future__ import annotations

from collections import Counter

from django.utils.translation import gettext

from ..constants import STRATEGIC_CRUISER_GROUP_ID, EveDogmaAttributeId, Section
from ..models import DoctrineFit, SdeType, SdeTypeAttribute

# Slot sections checked against a fixed hull attribute, in the order
# warnings are returned.
_SECTION_ATTRIBUTE_IDS = {
    Section.HIGH: EveDogmaAttributeId.HIGH_SLOTS,
    Section.MED: EveDogmaAttributeId.MED_SLOTS,
    Section.LOW: EveDogmaAttributeId.LOW_SLOTS,
    Section.RIG: EveDogmaAttributeId.RIG_SLOTS,
}


def slot_layout_warnings(fit: DoctrineFit) -> list[str]:
    """Compare the fit's High/Med/Low/Rig module counts against the hull's
    slot-layout attributes in the SDE mirror. Returns one warning per section
    where the fit has more modules than the hull has slots.

    Silently returns [] when the mirror doesn't have the hull's slot
    attributes yet (predates the ship-attribute exception, or an unknown
    hull) - never false-warn off missing data. Strategic Cruisers are exempt
    entirely: their slot counts come from fitted subsystems, not a fixed
    hull attribute.
    """
    rows = SdeTypeAttribute.objects.filter(
        eve_type_id=fit.ship_type_id, attribute_id__in=_SECTION_ATTRIBUTE_IDS.values()
    ).values_list("attribute_id", "value")
    if not rows:
        return []
    hull_slots = {attr_id: int(value) for attr_id, value in rows}

    hull_group_id = (
        SdeType.objects.filter(type_id=fit.ship_type_id)
        .values_list("group_id", flat=True)
        .first()
    )
    if hull_group_id == STRATEGIC_CRUISER_GROUP_ID:
        return []

    fitted_counts = Counter()
    for section, quantity in fit.items.values_list("section", "quantity"):
        fitted_counts[section] += quantity

    warnings: list[str] = []
    for section, attr_id in _SECTION_ATTRIBUTE_IDS.items():
        slots = hull_slots.get(attr_id)
        if slots is None:
            continue
        fitted = fitted_counts.get(section, 0)
        if fitted > slots:
            warnings.append(
                gettext(
                    "%(section)s: the fit has %(fitted)d modules but a %(hull)s has "
                    "%(slots)d slots - check the import for typos."
                )
                % {
                    "section": Section(section).label,
                    "fitted": fitted,
                    "hull": fit.ship_type.name,
                    "slots": slots,
                }
            )
    return warnings
