"""Regression: a corptools-cached assembled ship must surface in My Ships.

Unlike test_corptools_source.py (duck-typed fakes whose filter() ignores
kwargs), this uses the REAL stub models registered under app_label "corptools",
so the actual ORM filtering (singleton / type_id__in / character) runs against
real tables - the layer where a corptools-read regression would otherwise pass
unnoticed.
"""

from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from django.contrib.auth.models import User
from django.test import TestCase

from ..services import corptools_source, esi_assets
from .testdata.fake_corptools.models import CharacterAsset, CharacterAudit
from .testdata.sde_fixtures import T, create_sde_testdata


class CorptoolsRealReadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.user = User.objects.create_user("repro-pilot", password="x")
        cls.char = EveCharacter.objects.create(
            character_id=90000111,
            character_name="Repro Pilot",
            corporation_id=2001,
            corporation_name="Test Corp",
            corporation_ticker="TEST",
        )
        CharacterOwnership.objects.create(
            user=cls.user, character=cls.char, owner_hash="repro-hash-1"
        )
        audit = CharacterAudit.objects.create(character=cls.char)
        audit.set_update_time("assets")  # corptools marks the assets section synced
        audit.save()
        CharacterAsset.objects.create(
            character=audit,
            singleton=True,  # assembled / unpackaged hull
            item_id=5001,
            location_flag="Hangar",
            location_id=60003760,
            location_type="station",
            quantity=1,
            type_id=T.HARBINGER,
            name="My Brawler",
        )

    def test_ship_assets_for_character_returns_the_ship(self):
        with mock.patch.object(corptools_source, "corptools_installed", lambda: True):
            ships = corptools_source.ship_assets_for_character(
                self.char.character_id, {T.HARBINGER}
            )
        self.assertIsNotNone(ships, "corptools should serve this synced character")
        self.assertEqual([s["type_id"] for s in ships], [T.HARBINGER])

    def test_get_ship_inventory_surfaces_corptools_ship(self):
        with mock.patch.object(corptools_source, "corptools_installed", lambda: True):
            inv = esi_assets.get_ship_inventory(self.user)
        self.assertEqual([s.type_id for s in inv.ships], [T.HARBINGER])

    def test_inventory_doctor_command_reports_corptools_ship(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with mock.patch.object(corptools_source, "corptools_installed", lambda: True):
            call_command(
                "fitcheck_inventory_doctor",
                str(self.char.character_id),
                stdout=out,
            )
        text = out.getvalue()
        self.assertIn("ship_assets (no type filter)", text)
        self.assertIn("1 singleton rows", text)
        self.assertIn("ship_assets (SDE-filtered)", text)


class BulkShipAssetsTests(TestCase):
    """The bulk member-scan read: real ORM grouping/filtering plus the
    servable-with-zero-ships vs not-servable distinction (present-empty vs
    absent key) that decides whether the caller may fall back to live ESI."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

        def make_char(cid, name):
            return EveCharacter.objects.create(
                character_id=cid, character_name=name,
                corporation_id=2001, corporation_name="Test Corp",
                corporation_ticker="TEST",
            )

        cls.with_ship = make_char(90000201, "Synced With Ship")
        cls.zero_ships = make_char(90000202, "Synced Zero Ships")
        cls.never_synced = make_char(90000203, "Audited Never Synced")
        cls.unaudited = make_char(90000204, "Unaudited")

        audit = CharacterAudit.objects.create(character=cls.with_ship)
        audit.set_update_time("assets")
        audit.save()
        CharacterAsset.objects.create(
            character=audit, singleton=True, item_id=6001,
            location_flag="Hangar", location_id=60003760,
            location_type="station", quantity=1,
            type_id=T.HARBINGER, name="Bulk Harb",
        )
        # Same audit also holds a PACKAGED hull and an off-whitelist singleton -
        # both must be filtered out server-side.
        CharacterAsset.objects.create(
            character=audit, singleton=False, item_id=6002,
            location_flag="Hangar", location_id=60003760,
            location_type="station", quantity=1,
            type_id=T.HARBINGER, name="",
        )
        CharacterAsset.objects.create(
            character=audit, singleton=True, item_id=6003,
            location_flag="Hangar", location_id=60003760,
            location_type="station", quantity=1,
            type_id=T.HEAT_SINK_II, name="",
        )

        zero_audit = CharacterAudit.objects.create(character=cls.zero_ships)
        zero_audit.set_update_time("assets")
        zero_audit.save()

        CharacterAudit.objects.create(character=cls.never_synced)  # no sync time

        cls.all_ids = [
            cls.with_ship.character_id,
            cls.zero_ships.character_id,
            cls.never_synced.character_id,
            cls.unaudited.character_id,
        ]

    def test_bulk_groups_and_distinguishes_servable(self):
        with mock.patch.object(corptools_source, "corptools_installed", lambda: True):
            out = corptools_source.bulk_ship_assets_for_characters(
                self.all_ids, {T.HARBINGER}
            )
        # Servable characters are present; the zero-ship one maps to [].
        self.assertEqual(
            set(out),
            {self.with_ship.character_id, self.zero_ships.character_id},
        )
        self.assertEqual(
            [s["item_id"] for s in out[self.with_ship.character_id]], [6001]
        )
        self.assertEqual(out[self.zero_ships.character_id], [])

    def test_bulk_filters_singleton_and_type(self):
        with mock.patch.object(corptools_source, "corptools_installed", lambda: True):
            out = corptools_source.bulk_ship_assets_for_characters(
                [self.with_ship.character_id], {T.HARBINGER}
            )
        rows = out[self.with_ship.character_id]
        # The packaged Harbinger (6002) and the non-ship singleton (6003) are
        # excluded by the DB-side filters; the ESI dict shape is preserved.
        self.assertEqual([s["item_id"] for s in rows], [6001])
        self.assertTrue(rows[0]["is_singleton"])
        self.assertEqual(rows[0]["name"], "Bulk Harb")

    def test_bulk_is_two_queries(self):
        with mock.patch.object(corptools_source, "corptools_installed", lambda: True):
            with self.assertNumQueries(2):
                corptools_source.bulk_ship_assets_for_characters(
                    self.all_ids, {T.HARBINGER}
                )

    def test_bulk_empty_when_not_installed_or_no_ids(self):
        with mock.patch.object(corptools_source, "corptools_installed", lambda: False):
            self.assertEqual(
                corptools_source.bulk_ship_assets_for_characters(
                    self.all_ids, {T.HARBINGER}
                ),
                {},
            )
        with mock.patch.object(corptools_source, "corptools_installed", lambda: True):
            self.assertEqual(
                corptools_source.bulk_ship_assets_for_characters([], {T.HARBINGER}),
                {},
            )
