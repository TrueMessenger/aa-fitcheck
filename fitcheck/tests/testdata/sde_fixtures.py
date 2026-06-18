"""Hand-built SDE fixtures: realistic variant families with real-ish IDs.

Families:
- Heat Sink: I (T1 parent), Basic, II (T2), Imperial Navy + Ammatar Navy (faction)
- Cap Recharger: I (T1 parent), II (T2), Eutectic Compact (meta 1)
- Stasis Webifier: I (parent), II (T2), Abyssal (meta group 15) via mutaplasmid
- Hobgoblin drones: I (parent), II
- Templar fighters: I (parent), II
- Multifrequency L crystals: T1 (parent), Imperial Navy
- Hulls: Harbinger, Oracle, Hel
- Implant: Zainou 'Gnome' Shield Management SM-705
- Consumables: Nitrogen Isotopes (fuel), Nanite Repair Paste
"""

from eveuniverse.models import EveCategory, EveGroup, EveType

from fitcheck.constants import EveCategoryId, EveMetaGroupId, SlotKind
from fitcheck.models import (
    SdeAttribute,
    SdeMutaplasmidMapping,
    SdeType,
    SdeTypeAttribute,
)


class Attrs:
    DMG_MOD = 64  # damage modifier bonus - higher is better
    ROF_BONUS = 73  # rate of fire bonus - lower (more negative) is better
    CAP_RECHARGE = 144  # capacitor recharge rate bonus - lower (more negative) is better
    WEB_STRENGTH = 20  # max velocity modifier - lower (more negative) is better
    WEB_RANGE = 54  # optimal range - higher is better
    CPU_USAGE = 50  # excluded from default checks


class T:
    """Type IDs used across the test suite."""

    HARBINGER = 24696
    ORACLE = 4302
    HEL = 22852
    NIGHTMARE = 17736  # pirate battleship (group 27) - has a Frigate Escape Bay
    # FEB-eligible frigates (fit inside a Frigate Escape Bay).
    RIFTER = 587  # Frigate (group 25)
    WOLF = 11371  # Assault Frigate (group 324)
    ASTERO = 33468  # Frigate (group 25), also a named FEB exception hull

    HEAT_SINK_I = 2363
    HEAT_SINK_BASIC = 1893
    HEAT_SINK_II = 2364
    HEAT_SINK_IMPERIAL = 15810
    HEAT_SINK_AMMATAR = 17528

    CAP_RECHARGER_I = 1181
    CAP_RECHARGER_II = 2032
    CAP_RECHARGER_COMPACT = 8419

    WEB_I = 526
    WEB_II = 527
    WEB_ABYSSAL = 47702
    WEB_MUTAPLASMID = 47700

    PULSE_LASER_II = 6742

    HOBGOBLIN_I = 2454
    HOBGOBLIN_II = 2456

    TEMPLAR_I = 47140
    TEMPLAR_II = 47141

    MULTIFREQ_L = 262
    MULTIFREQ_L_NAVY = 23047

    IMPLANT_SM705 = 27082
    BOOSTER_STANDARD = 15457  # Standard Blue Pill - category 20 (Implant), but a booster

    NITROGEN_ISOTOPES = 17888
    HELIUM_ISOTOPES = 16274  # classified SlotKind.FUEL -> Section.FUEL_BAY
    NANITE_PASTE = 28668


def _sde_type(
    type_id,
    name,
    category,
    slot_kind,
    *,
    group=0,
    parent=None,
    meta_group=EveMetaGroupId.TECH_I,
    meta_level=0,
    attrs=None,
):
    row = SdeType.objects.create(
        type_id=type_id,
        name=name,
        group_id=group or category * 10,
        category_id=category,
        variation_parent_type_id=parent or type_id,
        meta_group_id=meta_group,
        meta_level=meta_level,
        slot_kind=slot_kind,
        published=True,
    )
    for attr_id, value in (attrs or {}).items():
        SdeTypeAttribute.objects.create(eve_type_id=type_id, attribute_id=attr_id, value=value)
    return row


def create_sde_testdata():
    for attr_id, name, hig in [
        (Attrs.DMG_MOD, "Damage Modifier", True),
        (Attrs.ROF_BONUS, "Rate of Fire Bonus", False),
        (Attrs.CAP_RECHARGE, "Recharge Rate Bonus", False),
        (Attrs.WEB_STRENGTH, "Maximum Velocity Bonus", False),
        (Attrs.WEB_RANGE, "Optimal Range", True),
        (Attrs.CPU_USAGE, "CPU usage", False),
    ]:
        SdeAttribute.objects.create(
            attribute_id=attr_id, name=name, display_name=name, high_is_good=hig
        )

    ships = EveCategoryId.SHIP
    module = EveCategoryId.MODULE

    _sde_type(T.HARBINGER, "Harbinger", ships, SlotKind.SHIP)
    _sde_type(T.ORACLE, "Oracle", ships, SlotKind.SHIP)
    _sde_type(T.HEL, "Hel", ships, SlotKind.SHIP)
    # Battleship-class hull (group 27) - carries a Frigate Escape Bay, so the
    # FEB picker shows for it (Harbinger's fixture group 60 does not).
    _sde_type(T.NIGHTMARE, "Nightmare", ships, SlotKind.SHIP, group=27)
    # FEB-eligible frigates the picker/quick-add expand over: a Frigate (25), an
    # Assault Frigate (324), and an exception-named Frigate (Astero, group 25).
    _sde_type(T.RIFTER, "Rifter", ships, SlotKind.SHIP, group=25)
    _sde_type(T.WOLF, "Wolf", ships, SlotKind.SHIP, group=324)
    _sde_type(T.ASTERO, "Astero", ships, SlotKind.SHIP, group=25)

    hs = dict(category=module, slot_kind=SlotKind.LOW, parent=T.HEAT_SINK_I)
    _sde_type(
        T.HEAT_SINK_I, "Heat Sink I", module, SlotKind.LOW,
        meta_level=0, attrs={Attrs.DMG_MOD: 8, Attrs.ROF_BONUS: -9.5, Attrs.CPU_USAGE: 25},
    )
    SdeType.objects.filter(pk=T.HEAT_SINK_I).update(variation_parent_type_id=T.HEAT_SINK_I)
    _sde_type(
        T.HEAT_SINK_BASIC, "Basic Heat Sink", hs["category"], hs["slot_kind"],
        parent=hs["parent"], meta_level=0,
        attrs={Attrs.DMG_MOD: 5, Attrs.ROF_BONUS: -8, Attrs.CPU_USAGE: 20},
    )
    _sde_type(
        T.HEAT_SINK_II, "Heat Sink II", hs["category"], hs["slot_kind"],
        parent=hs["parent"], meta_group=EveMetaGroupId.TECH_II, meta_level=5,
        attrs={Attrs.DMG_MOD: 12, Attrs.ROF_BONUS: -10.5, Attrs.CPU_USAGE: 30},
    )
    _sde_type(
        T.HEAT_SINK_IMPERIAL, "Imperial Navy Heat Sink", hs["category"], hs["slot_kind"],
        parent=hs["parent"], meta_group=EveMetaGroupId.FACTION, meta_level=8,
        attrs={Attrs.DMG_MOD: 13.5, Attrs.ROF_BONUS: -12, Attrs.CPU_USAGE: 24},
    )
    _sde_type(
        T.HEAT_SINK_AMMATAR, "Ammatar Navy Heat Sink", hs["category"], hs["slot_kind"],
        parent=hs["parent"], meta_group=EveMetaGroupId.FACTION, meta_level=8,
        attrs={Attrs.DMG_MOD: 13.5, Attrs.ROF_BONUS: -12, Attrs.CPU_USAGE: 24},
    )

    _sde_type(
        T.CAP_RECHARGER_I, "Cap Recharger I", module, SlotKind.MED,
        meta_level=0, attrs={Attrs.CAP_RECHARGE: -15},
    )
    _sde_type(
        T.CAP_RECHARGER_II, "Cap Recharger II", module, SlotKind.MED,
        parent=T.CAP_RECHARGER_I, meta_group=EveMetaGroupId.TECH_II, meta_level=5,
        attrs={Attrs.CAP_RECHARGE: -20},
    )
    _sde_type(
        T.CAP_RECHARGER_COMPACT, "Eutectic Compact Cap Recharger", module, SlotKind.MED,
        parent=T.CAP_RECHARGER_I, meta_level=1, attrs={Attrs.CAP_RECHARGE: -17},
    )

    _sde_type(
        T.WEB_I, "Stasis Webifier I", module, SlotKind.MED,
        meta_level=0, attrs={Attrs.WEB_STRENGTH: -50, Attrs.WEB_RANGE: 10000},
    )
    _sde_type(
        T.WEB_II, "Stasis Webifier II", module, SlotKind.MED,
        parent=T.WEB_I, meta_group=EveMetaGroupId.TECH_II, meta_level=5,
        attrs={Attrs.WEB_STRENGTH: -60, Attrs.WEB_RANGE: 14000},
    )
    _sde_type(
        T.WEB_ABYSSAL, "Abyssal Stasis Webifier", module, SlotKind.MED,
        parent=T.WEB_ABYSSAL, meta_group=EveMetaGroupId.ABYSSAL, meta_level=None,
        attrs={Attrs.WEB_STRENGTH: -55, Attrs.WEB_RANGE: 12000},
    )
    SdeMutaplasmidMapping.objects.create(
        abyssal_type_id=T.WEB_ABYSSAL,
        source_type_id=T.WEB_II,
        mutator_type_id=T.WEB_MUTAPLASMID,
        mutable_attributes=[
            {"attr_id": Attrs.WEB_STRENGTH, "min": 0.8, "max": 1.2, "high_is_good": False},
            {"attr_id": Attrs.WEB_RANGE, "min": 0.8, "max": 1.2, "high_is_good": True},
        ],
    )

    _sde_type(
        T.PULSE_LASER_II, "Focused Medium Pulse Laser II", module, SlotKind.HIGH,
        meta_group=EveMetaGroupId.TECH_II, meta_level=5,
    )

    _sde_type(T.HOBGOBLIN_I, "Hobgoblin I", EveCategoryId.DRONE, SlotKind.DRONE, meta_level=0)
    _sde_type(
        T.HOBGOBLIN_II, "Hobgoblin II", EveCategoryId.DRONE, SlotKind.DRONE,
        parent=T.HOBGOBLIN_I, meta_group=EveMetaGroupId.TECH_II, meta_level=5,
    )

    _sde_type(T.TEMPLAR_I, "Templar I", EveCategoryId.FIGHTER, SlotKind.FIGHTER, meta_level=1)
    _sde_type(
        T.TEMPLAR_II, "Templar II", EveCategoryId.FIGHTER, SlotKind.FIGHTER,
        parent=T.TEMPLAR_I, meta_group=EveMetaGroupId.TECH_II, meta_level=5,
    )

    _sde_type(T.MULTIFREQ_L, "Multifrequency L", EveCategoryId.CHARGE, SlotKind.CHARGE)
    _sde_type(
        T.MULTIFREQ_L_NAVY, "Imperial Navy Multifrequency L", EveCategoryId.CHARGE,
        SlotKind.CHARGE, parent=T.MULTIFREQ_L, meta_group=EveMetaGroupId.FACTION, meta_level=4,
    )

    _sde_type(
        T.IMPLANT_SM705, "Zainou 'Gnome' Shield Management SM-705",
        EveCategoryId.IMPLANT, SlotKind.IMPLANT, meta_level=0,
    )
    # Booster: shares the Implant category but is classified BOOSTER (the loader
    # detects this via the booster-slot attribute; the fixture sets it directly).
    _sde_type(
        T.BOOSTER_STANDARD, "Standard Blue Pill Booster",
        EveCategoryId.IMPLANT, SlotKind.BOOSTER, meta_level=0,
    )

    _sde_type(
        T.NITROGEN_ISOTOPES, "Nitrogen Isotopes", EveCategoryId.MATERIAL, SlotKind.OTHER
    )
    # Fuel-bay isotope: the loader classifies racial isotopes as FUEL; the fixture
    # sets it directly so fuel-bay compliance can be exercised without a full load.
    _sde_type(
        T.HELIUM_ISOTOPES, "Helium Isotopes", EveCategoryId.MATERIAL, SlotKind.FUEL,
    )
    _sde_type(T.NANITE_PASTE, "Nanite Repair Paste", EveCategoryId.CHARGE, SlotKind.CHARGE)

    _create_eveuniverse_mirror()


def _create_eveuniverse_mirror():
    """eveuniverse EveType rows matching the SdeType fixtures (for model FKs)."""
    categories: dict[int, EveCategory] = {}
    groups: dict[int, EveGroup] = {}
    for sde in SdeType.objects.all():
        if sde.category_id not in categories:
            categories[sde.category_id] = EveCategory.objects.create(
                id=sde.category_id, name=f"Category {sde.category_id}", published=True
            )
        if sde.group_id not in groups:
            groups[sde.group_id] = EveGroup.objects.create(
                id=sde.group_id,
                name=f"Group {sde.group_id}",
                eve_category=categories[sde.category_id],
                published=True,
            )
        EveType.objects.create(
            id=sde.type_id, name=sde.name, eve_group=groups[sde.group_id], published=True
        )
