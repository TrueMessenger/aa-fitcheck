"""Shared constants: EVE category/attribute/effect IDs, sections and ESI flag maps."""

from django.db import models
from django.utils.translation import gettext_lazy as _


# EVE market group: the "Booster" subtree (combat boosters + clone mappers) under
# "Implants & Boosters". Types whose market group descends from this are boosters,
# even when they lack the booster-slot dogma attribute (e.g. clone mappers).
BOOSTER_MARKET_GROUP_ROOT = 977

# EVE inventory group 423 ("Ice Product") holds the jump-fuel isotopes
# (Helium/Hydrogen/Nitrogen/Oxygen Isotopes) plus ozone/heavy water. We classify
# the racial isotopes (name ends with "Isotopes") as SlotKind.FUEL -> Section.FUEL_BAY
# so they land in a capital's fuel bay rather than generic cargo.
ICE_PRODUCT_GROUP_ID = 423


class EveCategoryId:
    MATERIAL = 4  # fuel isotopes, ozone - common cargo consumables
    SHIP = 6
    MODULE = 7
    CHARGE = 8
    DRONE = 18
    IMPLANT = 20
    DEPLOYABLE = 22
    SUBSYSTEM = 32
    FIGHTER = 87


class EveGroupId:
    """EVE inventory group IDs used for eligibility filtering."""

    FRIGATE = 25
    ASSAULT_FRIGATE = 324
    ELECTRONIC_ATTACK_SHIP = 893
    LOGISTICS_FRIGATE = 1527


# Ship groups whose hulls fit inside a Frigate Escape Bay. Used to populate the
# FEB frigate picker on the fit settings page (services/forms).
FEB_ELIGIBLE_GROUP_IDS = frozenset(
    {
        EveGroupId.FRIGATE,
        EveGroupId.ASSAULT_FRIGATE,
        EveGroupId.ELECTRONIC_ATTACK_SHIP,
        EveGroupId.LOGISTICS_FRIGATE,
    }
)

# Allowed FEB exception hulls listed by name. All three are currently group 25
# (Frigate) and so already match FEB_ELIGIBLE_GROUP_IDS, but they are named
# explicitly so the rule survives any future CCP regroup.
FEB_ELIGIBLE_EXCEPTION_NAMES = frozenset({"Astero", "Metamorphosis", "Venture"})


# Ship groups whose HULLS carry a Frigate Escape Bay (battleship-class only:
# T1 / Navy / Pirate battleships = group 27, Black Ops = 898, Marauders = 900).
# Supercapitals, capitals, battlecruisers, destroyers and frigates have no FEB,
# so the FEB picker is hidden for those hulls. The canonical signal is the
# `frigateEscapeBayCapacity` dogma attribute (3020), but the local SDE mirror
# loads no ship dogma attributes, so we key on the ship group instead.
FEB_CAPABLE_HULL_GROUP_IDS = frozenset({27, 898, 900})


# Categories the local SDE mirror loads.
LOADED_CATEGORY_IDS = frozenset(
    {
        EveCategoryId.MATERIAL,
        EveCategoryId.SHIP,
        EveCategoryId.MODULE,
        EveCategoryId.CHARGE,
        EveCategoryId.DRONE,
        EveCategoryId.IMPLANT,
        EveCategoryId.DEPLOYABLE,
        EveCategoryId.SUBSYSTEM,
        EveCategoryId.FIGHTER,
    }
)


class EveMetaGroupId:
    TECH_I = 1
    TECH_II = 2
    STORYLINE = 3
    FACTION = 4
    OFFICER = 5
    DEADSPACE = 6
    TECH_III = 14
    ABYSSAL = 15
    PREMIUM = 17
    LIMITED_TIME = 19
    STRUCTURE_TECH_I = 52
    STRUCTURE_TECH_II = 53


class EveDogmaAttributeId:
    LOW_SLOTS = 12
    MED_SLOTS = 13
    HIGH_SLOTS = 14
    CPU_OUTPUT = 48
    POWER_OUTPUT = 11
    CPU_USAGE = 50
    POWER_USAGE = 30
    META_LEVEL = 633
    RIG_SLOTS = 1137
    TECH_LEVEL = 422
    META_GROUP = 1692
    BOOSTERNESS = 1087  # booster-slot attribute - present on boosters, not implants


# Skill-requirement attributes (which skill + level a module needs to online). These
# are bookkeeping, not quality, so they must never decide a meet-or-beat comparison.
_SKILL_REQUIREMENT_ATTRIBUTES = frozenset(
    {
        182, 183, 184, 1285, 1289, 1290,  # requiredSkill1..6
        277, 278, 279, 1286, 1287, 1288,  # requiredSkill1..6Level
    }
)

# Attributes never compared under MEET_OR_BEAT by default (fitting cost / bookkeeping).
DEFAULT_EXCLUDED_CHECK_ATTRIBUTES = frozenset(
    {
        EveDogmaAttributeId.CPU_USAGE,
        EveDogmaAttributeId.POWER_USAGE,
        EveDogmaAttributeId.META_LEVEL,
        EveDogmaAttributeId.TECH_LEVEL,
        EveDogmaAttributeId.META_GROUP,
    }
    | _SKILL_REQUIREMENT_ATTRIBUTES
)


class EveDogmaEffectId:
    LOW_POWER = 11
    HIGH_POWER = 12
    MED_POWER = 13
    RIG_SLOT = 2663
    SUBSYSTEM = 3772


class SlotKind(models.TextChoices):
    """Functional classification of a type, derived from SDE effects + category."""

    HIGH = "HIGH", _("High slot module")
    MED = "MED", _("Mid slot module")
    LOW = "LOW", _("Low slot module")
    RIG = "RIG", _("Rig")
    SUBSYSTEM = "SUBSYS", _("Subsystem")
    DRONE = "DRONE", _("Drone")
    FIGHTER = "FIGHTER", _("Fighter")
    CHARGE = "CHARGE", _("Charge")
    SHIP = "SHIP", _("Ship")
    IMPLANT = "IMPLANT", _("Implant")
    BOOSTER = "BOOSTER", _("Booster")
    FUEL = "FUEL", _("Fuel")
    OTHER = "OTHER", _("Other")


class Section(models.TextChoices):
    """Where an item sits in a fit. Slot sections are exact-quantity;
    bay/cargo sections are at-least-quantity."""

    HIGH = "HIGH", _("High slots")
    MED = "MED", _("Mid slots")
    LOW = "LOW", _("Low slots")
    RIG = "RIG", _("Rigs")
    SUBSYSTEM = "SUBSYS", _("Subsystems")
    DRONE_BAY = "DRONE", _("Drone bay")
    FIGHTER_BAY = "FIGHTER", _("Fighter bay")
    CARGO = "CARGO", _("Cargo")
    FUEL_BAY = "FUEL", _("Fuel bay")
    FEB = "FEB", _("Frigate Escape Bay")
    IMPLANT = "IMPLANT", _("Implants")
    BOOSTER = "BOOSTER", _("Boosters")


# Sections where quantity must match exactly and order never matters.
SLOT_SECTIONS = (
    Section.HIGH,
    Section.MED,
    Section.LOW,
    Section.RIG,
    Section.SUBSYSTEM,
)

# Sections checked as "at least N" (with optional per-item leeway) by the
# compliance engine's _check_quantity_sections loop. FUEL_BAY rides this path
# but is warn-only (see WARN_QUANTITY_SECTIONS in compliance.py).
QUANTITY_SECTIONS = (
    Section.DRONE_BAY,
    Section.FIGHTER_BAY,
    Section.CARGO,
    Section.FUEL_BAY,
)

# Sections that expose a Qty % Pass field in the policy form and compute a
# required_quantity. Boosters get the field too (handled warn-only in
# _check_boosters), but are NOT in QUANTITY_SECTIONS to avoid double-checking.
LEEWAY_SECTIONS = QUANTITY_SECTIONS + (Section.BOOSTER,)

SECTION_ORDER = {
    Section.HIGH: 0,
    Section.MED: 1,
    Section.LOW: 2,
    Section.RIG: 3,
    Section.SUBSYSTEM: 4,
    Section.DRONE_BAY: 5,
    Section.FIGHTER_BAY: 6,
    Section.CARGO: 7,
    Section.FUEL_BAY: 8,
    Section.FEB: 9,
    Section.IMPLANT: 10,
    Section.BOOSTER: 11,
}

SLOT_KIND_TO_SECTION = {
    SlotKind.HIGH: Section.HIGH,
    SlotKind.MED: Section.MED,
    SlotKind.LOW: Section.LOW,
    SlotKind.RIG: Section.RIG,
    SlotKind.SUBSYSTEM: Section.SUBSYSTEM,
    SlotKind.DRONE: Section.DRONE_BAY,
    SlotKind.FIGHTER: Section.FIGHTER_BAY,
    SlotKind.CHARGE: Section.CARGO,
    SlotKind.IMPLANT: Section.IMPLANT,
    SlotKind.BOOSTER: Section.BOOSTER,
    SlotKind.FUEL: Section.FUEL_BAY,
    SlotKind.OTHER: Section.CARGO,
}


# Inverse of SLOT_KIND_TO_SECTION for the override picker: given a doctrine
# row's section, which slot kinds are admissible as exceptions?
# CARGO is special - cargo can hold any module-shape type plus charges plus
# "other", so its lookup spans most of the catalog.
_CARGO_KINDS = (
    SlotKind.HIGH, SlotKind.MED, SlotKind.LOW, SlotKind.RIG,
    SlotKind.DRONE, SlotKind.FIGHTER, SlotKind.CHARGE, SlotKind.OTHER,
)
SECTION_TO_SLOT_KINDS: dict[str, tuple] = {
    Section.HIGH: (SlotKind.HIGH,),
    Section.MED: (SlotKind.MED,),
    Section.LOW: (SlotKind.LOW,),
    Section.RIG: (SlotKind.RIG,),
    Section.SUBSYSTEM: (SlotKind.SUBSYSTEM,),
    Section.DRONE_BAY: (SlotKind.DRONE,),
    Section.FIGHTER_BAY: (SlotKind.FIGHTER,),
    Section.IMPLANT: (SlotKind.IMPLANT,),
    Section.BOOSTER: (SlotKind.BOOSTER,),
    Section.FUEL_BAY: (SlotKind.FUEL,),
    Section.CARGO: _CARGO_KINDS,
}


def esi_flag_to_section(flag: str) -> str | None:
    """Map an ESI fitting/assets location flag to a Section, or None if unsupported."""
    if flag.startswith("HiSlot"):
        return Section.HIGH
    if flag.startswith("MedSlot"):
        return Section.MED
    if flag.startswith("LoSlot"):
        return Section.LOW
    if flag.startswith("RigSlot"):
        return Section.RIG
    if flag.startswith("SubSystemSlot"):
        return Section.SUBSYSTEM
    if flag == "DroneBay":
        return Section.DRONE_BAY
    if flag == "FighterBay" or flag.startswith("FighterTube"):
        return Section.FIGHTER_BAY
    if flag == "SpecializedFuelBay":
        return Section.FUEL_BAY
    # FleetHangar (carriers, supercarriers, freighters, Orca, Rorqual) holds
    # exactly the kind of refit modules the cargo-demand logic cares about.
    if flag == "Cargo" or flag == "FleetHangar":
        return Section.CARGO
    return None


# Pinned ESI compatibility date; bump deliberately with each release.
ESI_COMPATIBILITY_DATE = "2026-06-01"
