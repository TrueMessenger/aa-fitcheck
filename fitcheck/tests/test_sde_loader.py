from django.test import TestCase

from ..constants import SlotKind
from ..models import SdeAttribute, SdeLoadRecord, SdeMutaplasmidMapping, SdeType, SdeTypeAttribute
from ..services.sde_loader import load_from_data

GROUPS = [
    {"_key": 25, "categoryID": 6},  # frigates
    {"_key": 65, "categoryID": 7},  # webs
    {"_key": 99, "categoryID": 4},  # materials
    {"_key": 12, "categoryID": 2},  # not loaded (celestial)
]

TYPES = [
    {"_key": 587, "name": {"en": "Rifter"}, "groupID": 25, "published": True},
    {
        "_key": 526,
        "name": {"en": "Stasis Webifier I"},
        "groupID": 65,
        "metaGroupID": 1,
        "published": True,
    },
    {
        "_key": 527,
        "name": {"en": "Stasis Webifier II"},
        "groupID": 65,
        "metaGroupID": 2,
        "variationParentTypeID": 526,
        "published": True,
    },
    {
        "_key": 47702,
        "name": {"en": "Abyssal Stasis Webifier"},
        "groupID": 65,
        "metaGroupID": 15,
        "published": True,
    },
    {"_key": 16273, "name": {"en": "Liquid Ozone"}, "groupID": 99, "published": True},
    {"_key": 5, "name": {"en": "A Planet"}, "groupID": 12, "published": True},
]

TYPE_DOGMA = [
    {
        "_key": 527,
        "dogmaAttributes": [
            {"attributeID": 633, "value": 5},
            {"attributeID": 20, "value": -60},
        ],
        "dogmaEffects": [{"effectID": 13}],  # medPower
    },
    {
        "_key": 526,
        "dogmaAttributes": [{"attributeID": 20, "value": -50}],
        "dogmaEffects": [{"effectID": 13}],
    },
]

DOGMA_ATTRIBUTES = [
    {
        "_key": 20,
        "name": "speedFactor",
        "displayName": {"en": "Maximum Velocity Bonus"},
        "highIsGood": False,
        "unitID": 124,
        "published": True,
    },
    {"_key": 633, "name": "metaLevelOld", "highIsGood": True, "published": True},
]

DOGMA_UNITS = [{"_key": 124, "name": "modifierPercent", "displayName": {"en": "%"}}]

DYNAMIC_ITEMS = [
    {
        "_key": 47700,
        "attributeIDs": [{"_key": 20, "highIsGood": False, "min": 0.8, "max": 1.2}],
        "inputOutputMapping": [{"applicableTypes": [526, 527], "resultingType": 47702}],
    }
]


def _load(build="build-1"):
    return load_from_data(
        types=iter(TYPES),
        groups=iter(GROUPS),
        type_dogma=iter(TYPE_DOGMA),
        dogma_attributes=iter(DOGMA_ATTRIBUTES),
        dogma_units=iter(DOGMA_UNITS),
        dynamic_items=iter(DYNAMIC_ITEMS),
        build=build,
    )


class TestSdeLoader(TestCase):
    def test_loads_types_with_classification(self):
        record = _load()
        self.assertEqual(record.type_count, 5)  # planet's category is filtered out
        self.assertFalse(SdeType.objects.filter(type_id=5).exists())

        rifter = SdeType.objects.get(type_id=587)
        self.assertEqual(rifter.slot_kind, SlotKind.SHIP)

        web2 = SdeType.objects.get(type_id=527)
        self.assertEqual(web2.slot_kind, SlotKind.MED)  # from medPower effect
        self.assertEqual(web2.meta_level, 5)  # from attribute 633
        self.assertEqual(web2.variation_parent_type_id, 526)

        # Parent normalization: family parents point at themselves.
        web1 = SdeType.objects.get(type_id=526)
        self.assertEqual(web1.variation_parent_type_id, 526)

    def test_attribute_metadata_and_values(self):
        _load()
        attr = SdeAttribute.objects.get(attribute_id=20)
        self.assertFalse(attr.high_is_good)
        self.assertEqual(attr.display_name, "Maximum Velocity Bonus")
        self.assertEqual(attr.unit_name, "%")

        value = SdeTypeAttribute.objects.get(eve_type_id=527, attribute_id=20)
        self.assertEqual(value.value, -60)

    def test_mutaplasmid_mappings(self):
        _load()
        mappings = SdeMutaplasmidMapping.objects.filter(abyssal_type_id=47702)
        self.assertEqual(mappings.count(), 2)  # one per applicable source type
        mapping = mappings.get(source_type_id=527)
        self.assertEqual(mapping.mutator_type_id, 47700)
        self.assertEqual(mapping.mutable_attributes[0]["attr_id"], 20)

    def test_reload_is_idempotent_and_removes_stale_types(self):
        _load("build-1")
        _load("build-2")
        self.assertEqual(SdeType.objects.count(), 5)
        self.assertEqual(SdeLoadRecord.objects.count(), 2)
        self.assertEqual(
            SdeMutaplasmidMapping.objects.filter(abyssal_type_id=47702).count(), 2
        )


class TestBoosterClassification(TestCase):
    """Boosters share the Implant category (20) but carry the booster-slot
    attribute (1087); the loader must classify them BOOSTER, leaving real
    implants as IMPLANT."""

    def test_booster_detected_via_booster_slot_attribute(self):
        load_from_data(
            groups=iter([{"_key": 740, "categoryID": 20}]),
            types=iter([
                {"_key": 15457, "name": {"en": "Standard Blue Pill Booster"},
                 "groupID": 740, "published": True},
                {"_key": 27082, "name": {"en": "Zainou 'Gnome' SM-705"},
                 "groupID": 740, "published": True},
            ]),
            type_dogma=iter([
                {"_key": 15457, "dogmaAttributes": [{"attributeID": 1087, "value": 1}]},
                {"_key": 27082, "dogmaAttributes": [{"attributeID": 331, "value": 5}]},
            ]),
            dogma_attributes=iter([
                {"_key": 1087, "name": "boosterness", "published": True},
                {"_key": 331, "name": "implantSlot", "published": True},
            ]),
            dogma_units=iter([]),
            dynamic_items=iter([]),
            build="boost",
        )
        self.assertEqual(SdeType.objects.get(type_id=15457).slot_kind, SlotKind.BOOSTER)
        self.assertEqual(SdeType.objects.get(type_id=27082).slot_kind, SlotKind.IMPLANT)

    def test_booster_detected_via_market_group(self):
        """Clone mappers / boosters without the booster-slot attribute are caught
        by the Booster market subtree (root 977)."""
        load_from_data(
            groups=iter([{"_key": 740, "categoryID": 20}]),
            market_groups=iter([
                {"_key": 977, "parentGroupID": 24},
                {"_key": 980, "parentGroupID": 977},  # a Booster sub-group
            ]),
            types=iter([
                {"_key": 28668, "name": {"en": "Clone Mapper"}, "groupID": 740,
                 "marketGroupID": 980, "published": True},
            ]),
            type_dogma=iter([]),  # no boosterness attribute
            dogma_attributes=iter([]),
            dogma_units=iter([]),
            dynamic_items=iter([]),
            build="mg",
        )
        self.assertEqual(SdeType.objects.get(type_id=28668).slot_kind, SlotKind.BOOSTER)


class TestBoosterResection(TestCase):
    """A reload re-points stored doctrine items from the Implant section to the
    Booster section when their type is now classified as a booster."""

    @classmethod
    def setUpTestData(cls):
        from .testdata.sde_fixtures import create_sde_testdata

        create_sde_testdata()

    def test_resection_moves_booster_items(self):
        from ..constants import Section
        from ..services.sde_loader import _resection_implant_booster_items
        from .testdata.factories import add_item, create_doctrine, create_fit
        from .testdata.sde_fixtures import T

        fit = create_fit(create_doctrine(), T.HARBINGER, name="boost")
        # The fixture types a Blue Pill as BOOSTER; simulate a stale IMPLANT row.
        item = add_item(fit, Section.IMPLANT, T.BOOSTER_STANDARD, 1)
        _resection_implant_booster_items()
        item.refresh_from_db()
        self.assertEqual(item.section, Section.BOOSTER)

    def test_resection_moves_assignment_snapshot_booster_items(self):
        """The per-(doctrine, fit) AssignmentItemPolicy snapshot is what doctrine
        grading reads, so resection must move its booster rows too - otherwise a
        submission grades the booster under Implants even when the source fit
        item is already correct (the live submission #76 symptom)."""
        from ..constants import Section
        from ..models import AssignmentItemPolicy
        from ..services.assignments import attach_fit_to_doctrine
        from ..services.sde_loader import _resection_implant_booster_items
        from .testdata.factories import add_item, create_doctrine, create_fit, create_user
        from .testdata.sde_fixtures import T

        user = create_user("resect")
        fit = create_fit(None, T.HARBINGER, name="boost-asg")
        add_item(fit, Section.IMPLANT, T.BOOSTER_STANDARD, 1)  # stale IMPLANT row
        attach_fit_to_doctrine(fit, create_doctrine(name="d-asg"), user=user)
        snap = AssignmentItemPolicy.objects.get(module_type_id=T.BOOSTER_STANDARD)
        self.assertEqual(snap.section, Section.IMPLANT)  # cloned stale section
        _resection_implant_booster_items()
        snap.refresh_from_db()
        self.assertEqual(snap.section, Section.BOOSTER)
