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
