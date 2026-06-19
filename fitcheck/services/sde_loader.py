"""Load the slice of the EVE static data export the engine needs.

Network path: download CCP's official JSONL bundle, skip when the build is
unchanged (ETag), stream-parse only the files we use. ``load_from_data`` takes
plain iterables so tests run without network and a Fuzzwork CSV reader can be
added behind the same interface.
"""

from __future__ import annotations

import json
import logging
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path

import requests
from django.db import transaction

from .. import __version__
from ..app_settings import (
    FITCHECK_ESI_CONTACT,
    FITCHECK_SDE_LATEST_URL,
    FITCHECK_SDE_SOURCE_URL,
)
from ..constants import (
    BOOSTER_MARKET_GROUP_ROOT,
    EveCategoryId,
    EveDogmaAttributeId,
    EveDogmaEffectId,
    ICE_PRODUCT_GROUP_ID,
    LOADED_CATEGORY_IDS,
    Section,
    SlotKind,
)
from ..models import (
    SdeAttribute,
    SdeLoadRecord,
    SdeMutaplasmidMapping,
    SdeType,
    SdeTypeAttribute,
)

logger = logging.getLogger(__name__)

_BATCH_SIZE = 1000

_EFFECT_TO_SLOT_KIND = {
    EveDogmaEffectId.HIGH_POWER: SlotKind.HIGH,
    EveDogmaEffectId.MED_POWER: SlotKind.MED,
    EveDogmaEffectId.LOW_POWER: SlotKind.LOW,
    EveDogmaEffectId.RIG_SLOT: SlotKind.RIG,
    EveDogmaEffectId.SUBSYSTEM: SlotKind.SUBSYSTEM,
}

_CATEGORY_TO_SLOT_KIND = {
    EveCategoryId.SHIP: SlotKind.SHIP,
    EveCategoryId.CHARGE: SlotKind.CHARGE,
    EveCategoryId.DRONE: SlotKind.DRONE,
    EveCategoryId.FIGHTER: SlotKind.FIGHTER,
    EveCategoryId.IMPLANT: SlotKind.IMPLANT,
}


def _user_agent() -> str:
    contact = FITCHECK_ESI_CONTACT or "contact-not-configured"
    return f"aa-fitcheck/{__version__} (+https://github.com/TrueMessenger/aa-fitcheck; contact: {contact})"


def _localized(value) -> str:
    if isinstance(value, dict):
        return value.get("en") or next(iter(value.values()), "")
    return value or ""


def current_remote_build(url: str | None = None) -> str | None:
    """Cheap build check via CCP's documented latest-version pointer,
    falling back to the archive's ETag/Last-Modified."""
    try:
        response = requests.get(
            FITCHECK_SDE_LATEST_URL, headers={"User-Agent": _user_agent()}, timeout=30
        )
        response.raise_for_status()
        build_number = json.loads(response.text.strip().splitlines()[0]).get("buildNumber")
        if build_number:
            return str(build_number)
    except (requests.RequestException, ValueError, IndexError) as exc:
        logger.warning("SDE latest-version check failed: %s", exc)
    try:
        response = requests.head(
            url or FITCHECK_SDE_SOURCE_URL,
            headers={"User-Agent": _user_agent()},
            timeout=30,
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("SDE build check failed: %s", exc)
        return None
    return response.headers.get("ETag") or response.headers.get("Last-Modified")


def load_sde(force: bool = False, url: str | None = None) -> SdeLoadRecord | None:
    """Download and load the SDE. Returns the new load record, or None if skipped."""
    url = url or FITCHECK_SDE_SOURCE_URL
    build = current_remote_build(url) or "unknown"
    latest = SdeLoadRecord.objects.order_by("-loaded_at").first()
    if not force and latest and latest.sde_build == build and build != "unknown":
        logger.info("SDE build %s already loaded - skipping.", build)
        return None

    logger.info("Downloading SDE from %s", url)
    with tempfile.TemporaryDirectory() as tmp_dir:
        archive_path = Path(tmp_dir) / "sde.zip"
        with requests.get(
            url, headers={"User-Agent": _user_agent()}, timeout=600, stream=True
        ) as response:
            response.raise_for_status()
            if build == "unknown":
                build = (
                    response.headers.get("ETag")
                    or response.headers.get("Last-Modified")
                    or build
                )
            with open(archive_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    fh.write(chunk)

        with zipfile.ZipFile(archive_path) as archive:
            members = {Path(name).name: name for name in archive.namelist()}

            def jsonl(filename: str) -> Iterable[dict]:
                member = members.get(filename)
                if member is None:
                    logger.warning("SDE archive is missing %s", filename)
                    return
                with archive.open(member) as fh:
                    for raw_line in fh:
                        raw_line = raw_line.strip()
                        if raw_line:
                            yield json.loads(raw_line)

            return load_from_data(
                types=jsonl("types.jsonl"),
                groups=jsonl("groups.jsonl"),
                market_groups=jsonl("marketGroups.jsonl"),
                type_dogma=jsonl("typeDogma.jsonl"),
                dogma_attributes=jsonl("dogmaAttributes.jsonl"),
                dogma_units=jsonl("dogmaUnits.jsonl"),
                dynamic_items=jsonl("dynamicItemAttributes.jsonl"),
                build=build,
            )


def _booster_market_groups(market_groups: Iterable[dict]) -> set[int]:
    """Market group ids in the Booster subtree (root 977), i.e. every group whose
    parent chain reaches the root. Catches boosters + clone mappers that don't
    carry the booster-slot dogma attribute."""
    parent_of: dict[int, int | None] = {
        row["_key"]: row.get("parentGroupID") for row in market_groups
    }
    in_subtree: dict[int, bool] = {}

    def reaches_root(gid: int | None) -> bool:
        if gid is None:
            return False
        if gid == BOOSTER_MARKET_GROUP_ROOT:
            return True
        if gid in in_subtree:
            return in_subtree[gid]
        in_subtree[gid] = False  # guard against cycles
        result = reaches_root(parent_of.get(gid))
        in_subtree[gid] = result
        return result

    return {gid for gid in parent_of if reaches_root(gid)}


def _resection_implant_booster_items() -> None:
    """Move stored doctrine/submission/finding rows to the correct IMPLANT vs
    BOOSTER section after types are (re)classified, so existing fits and graded
    submissions display boosters in their own section.

    Includes AssignmentItemPolicy: per-(doctrine, fit) snapshots are what the
    doctrine-grading path actually reads, so a booster left in IMPLANT there
    grades under the wrong section even when the source DoctrineFitItem is
    correct."""
    from ..models import (
        AssignmentItemPolicy,
        ComplianceFinding,
        DoctrineFitItem,
        SubmissionItem,
    )

    booster_ids = set(
        SdeType.objects.filter(slot_kind=SlotKind.BOOSTER).values_list("type_id", flat=True)
    )
    implant_ids = set(
        SdeType.objects.filter(slot_kind=SlotKind.IMPLANT).values_list("type_id", flat=True)
    )
    moves = [
        (DoctrineFitItem, "module_type_id"),
        (AssignmentItemPolicy, "module_type_id"),
        (SubmissionItem, "eve_type_id"),
        (ComplianceFinding, "expected_type_id"),
    ]
    for model, type_field in moves:
        model.objects.filter(
            section=Section.IMPLANT, **{f"{type_field}__in": booster_ids}
        ).update(section=Section.BOOSTER)
        model.objects.filter(
            section=Section.BOOSTER, **{f"{type_field}__in": implant_ids}
        ).update(section=Section.IMPLANT)


def _resection_fuel_items() -> None:
    """Move stored DOCTRINE rows from CARGO to FUEL_BAY for types now classified
    as fuel (racial isotopes), so a doctrine's fuel requirement created before the
    fuel classification grades as a fuel-bay demand.

    FORWARD-ONLY: no reverse FUEL_BAY->CARGO move, because Section.FUEL_BAY is
    populated by the ESI "SpecializedFuelBay" location flag independent of
    slot_kind, so it legitimately holds non-isotope fuel-bay contents (ozone,
    heavy water) a reverse sweep would wrongly evict. SubmissionItem is
    deliberately excluded: a pilot's fuel genuinely carried in cargo stays in
    cargo, where the engine credits it as carried-refit toward the fuel demand."""
    from ..models import AssignmentItemPolicy, ComplianceFinding, DoctrineFitItem

    fuel_ids = set(
        SdeType.objects.filter(slot_kind=SlotKind.FUEL).values_list("type_id", flat=True)
    )
    if not fuel_ids:
        return
    moves = [
        (DoctrineFitItem, "module_type_id"),
        (AssignmentItemPolicy, "module_type_id"),
        (ComplianceFinding, "expected_type_id"),
    ]
    for model, type_field in moves:
        model.objects.filter(
            section=Section.CARGO, **{f"{type_field}__in": fuel_ids}
        ).update(section=Section.FUEL_BAY)


@transaction.atomic
def load_from_data(
    *,
    types: Iterable[dict],
    groups: Iterable[dict],
    type_dogma: Iterable[dict],
    dogma_attributes: Iterable[dict],
    dogma_units: Iterable[dict],
    dynamic_items: Iterable[dict],
    market_groups: Iterable[dict] = (),
    build: str = "test",
) -> SdeLoadRecord:
    group_to_category = {row["_key"]: row.get("categoryID") for row in groups}
    booster_market_groups = _booster_market_groups(market_groups)

    unit_names = {row["_key"]: _localized(row.get("displayName") or row.get("name")) for row in dogma_units}

    attribute_objs = []
    for row in dogma_attributes:
        attribute_objs.append(
            SdeAttribute(
                attribute_id=row["_key"],
                name=row.get("name", ""),
                display_name=_localized(row.get("displayName")),
                high_is_good=bool(row.get("highIsGood", True)),
                unit_name=unit_names.get(row.get("unitID"), ""),
                published=bool(row.get("published", True)),
            )
        )
    SdeAttribute.objects.bulk_create(
        attribute_objs,
        update_conflicts=True,
        unique_fields=["attribute_id"],
        update_fields=["name", "display_name", "high_is_good", "unit_name", "published"],
        batch_size=_BATCH_SIZE,
    )

    # Pass 1: types in our categories.
    type_rows: dict[int, SdeType] = {}
    for row in types:
        category_id = group_to_category.get(row.get("groupID"))
        if category_id not in LOADED_CATEGORY_IDS:
            continue
        type_id = row["_key"]
        type_rows[type_id] = SdeType(
            type_id=type_id,
            name=_localized(row.get("name"))[:200],
            group_id=row.get("groupID") or 0,
            category_id=category_id,
            variation_parent_type_id=row.get("variationParentTypeID") or type_id,
            meta_group_id=row.get("metaGroupID"),
            meta_level=None,
            market_group_id=row.get("marketGroupID"),
            slot_kind=_CATEGORY_TO_SLOT_KIND.get(category_id, SlotKind.OTHER),
            published=bool(row.get("published", False)),
        )

    # Pass 2: dogma - meta level, slot kind from effects, attribute values.
    attr_value_objs: list[SdeTypeAttribute] = []
    known_attribute_ids = {a.attribute_id for a in attribute_objs} or set(
        SdeAttribute.objects.values_list("attribute_id", flat=True)
    )
    for row in type_dogma:
        sde_type = type_rows.get(row["_key"])
        if sde_type is None:
            continue
        for effect in row.get("dogmaEffects") or []:
            slot_kind = _EFFECT_TO_SLOT_KIND.get(effect.get("effectID"))
            if slot_kind and sde_type.slot_kind == SlotKind.OTHER:
                sde_type.slot_kind = slot_kind
        for attr in row.get("dogmaAttributes") or []:
            attr_id = attr.get("attributeID")
            value = attr.get("value")
            if attr_id == EveDogmaAttributeId.META_LEVEL and value is not None:
                sde_type.meta_level = int(value)
            # Boosters share the Implant category (20) but carry the booster-slot
            # attribute; reclassify them so they're not treated as implants.
            if (
                attr_id == EveDogmaAttributeId.BOOSTERNESS
                and sde_type.category_id == EveCategoryId.IMPLANT
            ):
                sde_type.slot_kind = SlotKind.BOOSTER
            if (
                sde_type.category_id != EveCategoryId.SHIP
                and attr_id in known_attribute_ids
                and value is not None
            ):
                attr_value_objs.append(
                    SdeTypeAttribute(
                        eve_type_id=sde_type.type_id, attribute_id=attr_id, value=value
                    )
                )

    # Boosters & clone mappers share the Implant category but live under the
    # Booster market subtree; reclassify any that the dogma-attribute pass missed.
    if booster_market_groups:
        for sde_type in type_rows.values():
            if (
                sde_type.category_id == EveCategoryId.IMPLANT
                and sde_type.market_group_id in booster_market_groups
            ):
                sde_type.slot_kind = SlotKind.BOOSTER

    # Racial isotopes are a capital's Specialized Fuel Bay contents; classify them
    # as FUEL so they land in the Fuel Bay section, not generic cargo. Group 423
    # ("Ice Product") also holds ozone/heavy water, so narrow by the name suffix.
    for sde_type in type_rows.values():
        if (
            sde_type.group_id == ICE_PRODUCT_GROUP_ID
            and sde_type.slot_kind == SlotKind.OTHER
            and sde_type.name.endswith("Isotopes")
        ):
            sde_type.slot_kind = SlotKind.FUEL

    SdeType.objects.bulk_create(
        list(type_rows.values()),
        update_conflicts=True,
        unique_fields=["type_id"],
        update_fields=[
            "name",
            "group_id",
            "category_id",
            "variation_parent_type_id",
            "meta_group_id",
            "meta_level",
            "market_group_id",
            "slot_kind",
            "published",
        ],
        batch_size=_BATCH_SIZE,
    )
    SdeType.objects.exclude(type_id__in=type_rows.keys()).delete()

    SdeTypeAttribute.objects.all().delete()
    SdeTypeAttribute.objects.bulk_create(attr_value_objs, batch_size=_BATCH_SIZE)

    # Mutaplasmid mappings: keyed by mutaplasmid type, mapping sources -> abyssal result.
    mapping_objs = []
    for row in dynamic_items:
        mutator_type_id = row["_key"]
        mutable = [
            {
                "attr_id": spec["_key"],
                "min": spec.get("min"),
                "max": spec.get("max"),
                "high_is_good": spec.get("highIsGood", True),
            }
            for spec in row.get("attributeIDs") or []
        ]
        for mapping in row.get("inputOutputMapping") or []:
            resulting = mapping.get("resultingType")
            if resulting not in type_rows:
                continue
            for source in mapping.get("applicableTypes") or []:
                if source not in type_rows:
                    continue
                mapping_objs.append(
                    SdeMutaplasmidMapping(
                        abyssal_type_id=resulting,
                        source_type_id=source,
                        mutator_type_id=mutator_type_id,
                        mutable_attributes=mutable,
                    )
                )
    SdeMutaplasmidMapping.objects.all().delete()
    SdeMutaplasmidMapping.objects.bulk_create(mapping_objs, batch_size=_BATCH_SIZE)

    # Existing doctrine/submission rows may carry a stale IMPLANT section for
    # types now classified as boosters (or vice versa) - fix them up. Likewise,
    # doctrine fuel requirements stored before isotopes were classified as fuel
    # still sit in CARGO - move them to the Fuel Bay.
    _resection_implant_booster_items()
    _resection_fuel_items()

    record = SdeLoadRecord.objects.create(sde_build=build, type_count=len(type_rows))
    logger.info("SDE load complete: %s types, build %s", len(type_rows), build)
    return record
