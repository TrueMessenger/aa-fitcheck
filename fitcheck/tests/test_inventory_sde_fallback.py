"""Resilience when the local SDE mirror is empty (a fresh install before
`fitcheck_load_sde` has run): ship listing falls back to eveuniverse, the mirror
self-heals via a one-off background load, and the views warn / show the hull name
instead of silently rendering "0 ships" / a bare type_id.
"""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from ..constants import EveCategoryId
from ..models import SdeType
from ..services import esi_assets
from ..services.sde_loader import (
    _AUTOLOAD_LOCK_KEY,
    ensure_sde_loading,
    sde_ship_data_loaded,
)
from .testdata.factories import create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


def _delete_sde_ships():
    SdeType.objects.filter(category_id=EveCategoryId.SHIP).delete()


class HullTypeSetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_hull_set_is_mirror_independent(self):
        _delete_sde_ships()
        # A hull-scoped scan already knows the type it wants - no SDE mirror needed.
        self.assertEqual(esi_assets._ship_type_id_set(T.NIGHTMARE), {T.NIGHTMARE})
        # No hull + empty mirror -> empty (callers classify owned assets instead).
        self.assertEqual(esi_assets._ship_type_id_set(), set())


class ShipClassificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_classifies_via_eveuniverse_when_mirror_lacks_type(self):
        from eveuniverse.models import EveGroup, EveType

        # A ship eveuniverse knows but the fitcheck SDE mirror does not.
        ship_group = EveGroup.objects.get(id=27)  # Nightmare's group, category SHIP
        EveType.objects.create(
            id=999001, name="Eveuni Frigate", eve_group=ship_group, published=True
        )
        result = esi_assets.ship_type_ids_among({999001, T.HEAT_SINK_II})
        self.assertIn(999001, result)  # ship resolved via eveuniverse fallback
        self.assertNotIn(T.HEAT_SINK_II, result)  # a module the mirror knows

    def test_mirror_is_authoritative_for_known_types(self):
        # Both types are in the mirror; eveuniverse is never consulted.
        self.assertEqual(
            esi_assets.ship_type_ids_among({T.NIGHTMARE, T.HEAT_SINK_II}),
            {T.NIGHTMARE},
        )

    def test_empty_input(self):
        self.assertEqual(esi_assets.ship_type_ids_among([]), set())


class SdeAutoloadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        cache.delete(_AUTOLOAD_LOCK_KEY)

    def test_loaded_when_ships_present(self):
        self.assertTrue(sde_ship_data_loaded())
        with mock.patch("fitcheck.tasks.update_sde_data.delay") as delay:
            self.assertTrue(ensure_sde_loading())
        delay.assert_not_called()

    def test_empty_mirror_enqueues_exactly_one_load(self):
        _delete_sde_ships()
        self.assertFalse(sde_ship_data_loaded())
        with mock.patch("fitcheck.tasks.update_sde_data.delay") as delay:
            self.assertFalse(ensure_sde_loading())  # first: enqueues
            self.assertFalse(ensure_sde_loading())  # second: lock held, skipped
        delay.assert_called_once()

    def test_held_lock_prevents_enqueue(self):
        _delete_sde_ships()
        cache.set(_AUTOLOAD_LOCK_KEY, "1", 600)
        with mock.patch("fitcheck.tasks.update_sde_data.delay") as delay:
            self.assertFalse(ensure_sde_loading())
        delay.assert_not_called()


class InventoryFallbackScanTests(TestCase):
    """End-to-end: with the SDE mirror empty, a scan still lists ships by
    classifying the owned (singleton) assets through eveuniverse."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_empty_mirror_lists_ships_via_eveuniverse(self):
        from allianceauth.eveonline.models import EveCharacter

        _delete_sde_ships()  # mirror knows no ships; eveuniverse still does
        char = EveCharacter.objects.create(
            character_id=777, character_name="Capsuleer",
            corporation_id=1, corporation_name="C", corporation_ticker="C",
            alliance_id=1, alliance_name="A", alliance_ticker="A",
            security_status=0,
        )
        singletons = [{
            "item_id": 5000, "type_id": T.HARBINGER, "location_id": 60003760,
            "location_flag": "Hangar", "quantity": 1, "is_singleton": True,
            "name": "My Harb",
        }]
        with mock.patch.object(esi_assets, "tokens_by_character", return_value={}), \
             mock.patch(
                 "fitcheck.services.corptools_source.ship_assets_for_character",
                 return_value=singletons,
             ), \
             mock.patch.object(
                 esi_assets, "_ship_group_names",
                 return_value={T.HARBINGER: "Battlecruiser"},
             ):
            inventory = esi_assets.get_inventory_for_characters([char], hull_type_id=None)
        self.assertEqual([s.type_id for s in inventory.ships], [T.HARBINGER])
        self.assertEqual(inventory.ships[0].type_name, "Harbinger")  # eveuniverse name


class InventoryViewMessagingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Brawl")
        cls.member = create_user("member")

    def setUp(self):
        cache.delete(_AUTOLOAD_LOCK_KEY)

    def _empty_inventory(self):
        from ..services.esi_assets import ShipInventory

        return mock.patch(
            "fitcheck.services.esi_assets.get_ship_inventory",
            return_value=ShipInventory(),
        )

    def test_prefilter_alert_shows_hull_name_when_unowned(self):
        """The pilot owns no Nightmare, but the alert resolves and shows the hull
        name instead of leaking the bare type_id (the old '12032' bug)."""
        self.client.force_login(self.member)
        with self._empty_inventory():
            resp = self.client.get(
                reverse("fitcheck:ship_inventory"), {"type_id": T.NIGHTMARE}
            )
        self.assertContains(resp, "<strong>Nightmare</strong>")
        self.assertNotContains(resp, f"<strong>{T.NIGHTMARE}</strong>")
        self.assertContains(resp, "own a Nightmare")  # tailored empty state

    def test_empty_mirror_shows_loading_banner_and_autoloads(self):
        _delete_sde_ships()
        self.client.force_login(self.member)
        with self._empty_inventory(), mock.patch(
            "fitcheck.tasks.update_sde_data.delay"
        ) as delay:
            resp = self.client.get(reverse("fitcheck:ship_inventory"))
        self.assertContains(resp, "game data is still loading")
        delay.assert_called_once()

    def test_loaded_mirror_has_no_banner(self):
        self.client.force_login(self.member)
        with self._empty_inventory(), mock.patch(
            "fitcheck.tasks.update_sde_data.delay"
        ) as delay:
            resp = self.client.get(reverse("fitcheck:ship_inventory"))
        self.assertNotContains(resp, "game data is still loading")
        delay.assert_not_called()
