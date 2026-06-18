"""Create fitting standards from EFT text (admin side). A fitting stands alone;
optionally link it to one or more doctrines at import time."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from eveuniverse.models import EveType

from ..models import Doctrine, DoctrineFit, DoctrineFitItem
from .eft_parser import parse_eft
from .fit_data import ParsedFit


class DoctrineImportError(Exception):
    def __init__(self, message: str, errors=None):
        super().__init__(message)
        self.errors = errors or []


def _get_or_create_eve_type(type_id: int) -> EveType:
    """Local row if we have it; ESI only for types eveuniverse has never seen."""
    eve_type = EveType.objects.filter(id=type_id).first()
    if eve_type is None:
        eve_type, _ = EveType.objects.get_or_create_esi(id=type_id)
    return eve_type


@transaction.atomic
def import_fit(
    eft_text: str,
    user: User | None = None,
    *,
    doctrine: Doctrine | None = None,
    name: str | None = None,
    parsed: ParsedFit | None = None,
) -> DoctrineFit:
    """Import an EFT paste as a fitting standard. Raises DoctrineImportError on bad input."""
    parsed = parsed or parse_eft(eft_text)
    if parsed.errors or parsed.ship_type_id is None:
        raise DoctrineImportError(
            "The fit could not be parsed.",
            errors=[f"Line {e.line_no}: '{e.text}' - {e.reason}" for e in parsed.errors],
        )

    fit = DoctrineFit.objects.create(
        name=name or parsed.fit_name or "Unnamed fit",
        ship_type=_get_or_create_eve_type(parsed.ship_type_id),
        eft_source=eft_text,
        default_policy=DoctrineFit._meta.get_field("default_policy").default,
        last_imported_by=user,
        bom_updated_at=timezone.now(),
    )
    _materialise_items(fit, parsed)
    # Attach AFTER items materialise so the assignment can snapshot a full
    # set of source policies, not an empty one.
    if doctrine is not None:
        from .assignments import attach_fit_to_doctrine

        attach_fit_to_doctrine(fit, doctrine, user=user)
    return fit


def _materialise_items(fit: DoctrineFit, parsed: ParsedFit) -> None:
    """Build DoctrineFitItem rows for `fit` from a ParsedFit. Used by the
    EFT-paste import path AND by the colcrunch re-sync refresh path."""
    items: list[DoctrineFitItem] = []
    for item in parsed.items:
        items.append(
            DoctrineFitItem(
                fit=fit,
                section=item.section,
                module_type=_get_or_create_eve_type(item.type_id),
                quantity=item.quantity,
                charge_type=(
                    _get_or_create_eve_type(item.charge_type_id)
                    if item.charge_type_id
                    else None
                ),
                policy=fit.default_policy,
            )
        )
    _merge_and_create(items)


def _merge_and_create(items: list[DoctrineFitItem]) -> None:
    """Aggregate duplicate (section, type) rows the parser kept apart (e.g. the
    same module with different charges) before hitting the unique constraint."""
    merged: dict[tuple, DoctrineFitItem] = {}
    for item in items:
        key = (item.fit_id, item.section, item.module_type_id)
        if key in merged:
            merged[key].quantity += item.quantity
            if merged[key].charge_type_id is None:
                merged[key].charge_type = item.charge_type
        else:
            merged[key] = item
    DoctrineFitItem.objects.bulk_create(merged.values())
