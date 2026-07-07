"""Create fitting standards from EFT text (admin side). A fitting stands alone;
optionally link it to one or more doctrines at import time."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from eveuniverse.models import EveType

from ..models import CompliancePolicy, Doctrine, DoctrineFit, DoctrineFitItem
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
    policy: CompliancePolicy | None = None,
) -> DoctrineFit:
    """Import an EFT paste as a fitting standard. Raises DoctrineImportError on bad input.

    When `policy` is given, it's applied to the fit's items right after they're
    materialised - and, critically, BEFORE `doctrine` attachment clones them into
    a per-assignment snapshot, so the doctrine's snapshot carries the chosen
    preset instead of the bare seed policy (#98)."""
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
        last_imported_by=user,
        bom_updated_at=timezone.now(),
    )
    _materialise_items(fit, parsed)
    if policy is not None:
        from .policies import apply_policy_to_fit

        apply_policy_to_fit(fit, policy)
    # Attach AFTER items materialise (and any chosen policy is applied) so the
    # assignment snapshots the fit's fully-resolved per-item policies rather
    # than an empty or seed-only set.
    if doctrine is not None:
        from .assignments import attach_fit_to_doctrine

        attach_fit_to_doctrine(fit, doctrine, user=user)
    return fit


def _materialise_items(fit: DoctrineFit, parsed: ParsedFit) -> None:
    """Build DoctrineFitItem rows for `fit` from a ParsedFit. Used by the
    EFT-paste import path AND by the colcrunch re-sync refresh path.

    New items seed their policy fields from `fit.compliance_policy`'s slot
    rules (falling back to plain VARIANTS substitution when the fit has no
    applied preset yet, or the preset has no rule for a section) - see
    `services.policies.seed_fields_for_section`. On a fresh EFT-paste import
    this runs before any `policy` argument is applied, so it only matters for
    BOM re-imports where the fit already carries a preset."""
    from .policies import seed_fields_for_section

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
                **seed_fields_for_section(fit.compliance_policy, item.section),
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
