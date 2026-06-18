"""Tests for the optional corptools (Corp Tools) asset read-through.

corptools isn't installed in the test env, so its models are hand-rolled
duck-typed fakes matching the slice corptools_source reads:
CharacterAudit.objects.filter(character__character_id=).first() with
get_update_time("assets"), and CharacterAsset.objects.filter(character=)
.values(...). The adapter must emit the SAME flat dict shape esi_assets
._fetch_assets returns so the rest of the pipeline is unchanged.
"""

from unittest import mock

from django.test import TestCase

from ..services import corptools_source, esi_assets
from .testdata.sde_fixtures import T, create_sde_testdata


class _Qs(list):
    def first(self):
        return self[0] if self else None

    def values(self, *fields):
        return [{f: row[f] for f in fields} for row in self]


class _FakeManager:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, **_kwargs):
        return _Qs(self._rows)


class FakeAudit:
    def __init__(self, assets_synced="2026-06-18T00:00:00"):
        self._assets_synced = assets_synced

    def get_update_time(self, key):
        return self._assets_synced if key == "assets" else None


def _fake_models(audit_rows, asset_rows):
    class AuditModel:
        objects = _FakeManager(audit_rows)

    class AssetModel:
        objects = _FakeManager(asset_rows)

    return (AuditModel, AssetModel)


def _patched_corptools(audit_rows, asset_rows):
    models = _fake_models(audit_rows, asset_rows)
    return mock.patch.multiple(
        "fitcheck.services.corptools_source",
        corptools_installed=lambda: True,
        _models=lambda: models,
    )


ASSET_ROW = {
    "item_id": 1001,
    "type_id": T.HEAT_SINK_II,
    "location_id": 9000,
    "location_flag": "LoSlot0",
    "quantity": 1,
    "singleton": True,
    "name": "My Harbinger",
}


class TestCorptoolsAdapter(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_maps_rows_to_esi_asset_dict_shape(self):
        with _patched_corptools([FakeAudit()], [dict(ASSET_ROW)]):
            out = corptools_source.assets_for_character(123)
        self.assertEqual(
            out,
            [
                {
                    "item_id": 1001,
                    "type_id": T.HEAT_SINK_II,
                    "location_id": 9000,
                    "location_flag": "LoSlot0",  # raw ESI flag, unchanged
                    "quantity": 1,
                    "is_singleton": True,
                    "name": "My Harbinger",
                }
            ],
        )

    def test_none_when_not_installed(self):
        with mock.patch.object(corptools_source, "corptools_installed", lambda: False):
            self.assertIsNone(corptools_source.assets_for_character(123))

    def test_none_when_character_not_audited(self):
        with _patched_corptools([], []):
            self.assertIsNone(corptools_source.assets_for_character(123))

    def test_none_when_assets_never_synced(self):
        # Audited, but the assets module never ran -> don't pretend the pilot
        # owns nothing; let the caller fall back to live ESI.
        with _patched_corptools([FakeAudit(assets_synced=None)], [dict(ASSET_ROW)]):
            self.assertIsNone(corptools_source.assets_for_character(123))


class TestResolveAssets(TestCase):
    """esi_assets.resolve_assets: corptools-first / ESI-fallback per source."""

    def test_auto_prefers_corptools_without_a_token(self):
        cached = [dict(ASSET_ROW)]
        with mock.patch.object(esi_assets, "_asset_source", lambda: "auto"), \
             mock.patch("fitcheck.services.corptools_source.assets_for_character",
                        return_value=cached), \
             mock.patch.object(esi_assets, "_fetch_assets") as fetch:
            out = esi_assets.resolve_assets(123, token=None)
        self.assertEqual(out, cached)
        fetch.assert_not_called()  # no live ESI call, no token needed

    def test_auto_falls_back_to_esi_when_corptools_empty(self):
        live = [dict(ASSET_ROW)]
        with mock.patch.object(esi_assets, "_asset_source", lambda: "auto"), \
             mock.patch("fitcheck.services.corptools_source.assets_for_character",
                        return_value=None), \
             mock.patch.object(esi_assets, "_fetch_assets", return_value=live) as fetch:
            out = esi_assets.resolve_assets(123, token="tok")
        self.assertEqual(out, live)
        fetch.assert_called_once()

    def test_auto_returns_none_when_no_token_and_no_cache(self):
        with mock.patch.object(esi_assets, "_asset_source", lambda: "auto"), \
             mock.patch("fitcheck.services.corptools_source.assets_for_character",
                        return_value=None), \
             mock.patch.object(esi_assets, "_fetch_assets") as fetch:
            self.assertIsNone(esi_assets.resolve_assets(123, token=None))
        fetch.assert_not_called()

    def test_esi_source_ignores_corptools(self):
        live = [dict(ASSET_ROW)]
        with mock.patch.object(esi_assets, "_asset_source", lambda: "esi"), \
             mock.patch("fitcheck.services.corptools_source.assets_for_character") as cached, \
             mock.patch.object(esi_assets, "_fetch_assets", return_value=live) as fetch:
            out = esi_assets.resolve_assets(123, token="tok")
        self.assertEqual(out, live)
        cached.assert_not_called()
        fetch.assert_called_once()

    def test_corptools_source_never_falls_through_to_esi(self):
        with mock.patch.object(esi_assets, "_asset_source", lambda: "corptools"), \
             mock.patch("fitcheck.services.corptools_source.assets_for_character",
                        return_value=None), \
             mock.patch.object(esi_assets, "_fetch_assets") as fetch:
            self.assertIsNone(esi_assets.resolve_assets(123, token="tok"))
        fetch.assert_not_called()


class TestMemberScanWithoutToken(TestCase):
    """The headline win: an alliance member scan reads corptools' cached assets
    with NO fitcheck token - the character isn't flagged 'without token'."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_corptools_served_character_is_scanned_tokenless(self):
        from allianceauth.eveonline.models import EveCharacter

        char = EveCharacter.objects.create(
            character_id=777, character_name="Capsuleer",
            corporation_id=1, corporation_name="C", corporation_ticker="C",
            alliance_id=1, alliance_name="A", alliance_ticker="A",
            security_status=0,
        )
        # ESI-shaped dicts (the adapter's output): note is_singleton, flat location_id.
        ship_assets = [{
            "item_id": 5000, "type_id": T.HARBINGER, "location_id": 60003760,
            "location_flag": "Hangar", "quantity": 1, "is_singleton": True,
            "name": "My Harb",
        }]
        with mock.patch.object(esi_assets, "tokens_by_character", return_value={}), \
             mock.patch("fitcheck.services.corptools_source.ship_assets_for_character",
                        return_value=ship_assets), \
             mock.patch.object(esi_assets, "_ship_group_names",
                               return_value={T.HARBINGER: "Battlecruiser"}):
            inventory = esi_assets.get_inventory_for_characters(
                [char], hull_type_id=T.HARBINGER
            )

        self.assertEqual([s.type_id for s in inventory.ships], [T.HARBINGER])
        self.assertEqual(inventory.ships[0].ship_name, "My Harb")  # cached name used
        self.assertEqual(inventory.characters_without_token, [])  # not flagged ungranted


class TestNarrowAssetReads(TestCase):
    """Phase-1 (ships only) and Phase-2 (ship contents) read a NARROW slice
    instead of the whole asset tree, but emit the same ESI dict shape and keep
    the None-fallback semantics so the caller still drops through to live ESI.

    (corptools isn't installed in tests, so the fakes ignore the DB filters and
    return the rows given - these lock the shape + None handling, not the SQL.)"""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_ship_assets_returns_mapped_ship_rows(self):
        ship_row = {
            "item_id": 5000, "type_id": T.HARBINGER, "location_id": 60003760,
            "location_flag": "Hangar", "quantity": 1, "singleton": True,
            "name": "My Harb",
        }
        with _patched_corptools([FakeAudit()], [ship_row]):
            out = corptools_source.ship_assets_for_character(123, [T.HARBINGER])
        self.assertEqual(out, [{
            "item_id": 5000, "type_id": T.HARBINGER, "location_id": 60003760,
            "location_flag": "Hangar", "quantity": 1, "is_singleton": True,
            "name": "My Harb",
        }])

    def test_ship_assets_none_when_not_synced(self):
        with _patched_corptools([FakeAudit(assets_synced=None)], [dict(ASSET_ROW)]):
            self.assertIsNone(
                corptools_source.ship_assets_for_character(123, [T.HARBINGER])
            )

    def test_ship_assets_none_when_not_installed(self):
        with mock.patch.object(corptools_source, "corptools_installed", lambda: False):
            self.assertIsNone(corptools_source.ship_assets_for_character(123, None))

    def test_ship_contents_returns_ship_and_its_contents(self):
        ship_row = {
            "item_id": 5000, "type_id": T.HARBINGER, "location_id": 60003760,
            "location_flag": "Hangar", "quantity": 1, "singleton": True, "name": "Harb",
        }
        module_row = {
            "item_id": 5001, "type_id": T.HEAT_SINK_II, "location_id": 5000,
            "location_flag": "LoSlot0", "quantity": 1, "singleton": False, "name": "",
        }
        with _patched_corptools([FakeAudit()], [ship_row, module_row]):
            out = corptools_source.ship_contents_for_character(123, [5000])
        self.assertEqual({r["item_id"] for r in out}, {5000, 5001})

    def test_ship_contents_empty_list_for_no_ids(self):
        # Servable audit but no ships asked for -> empty (NOT None, which would
        # wrongly trigger an ESI fallback).
        with _patched_corptools([FakeAudit()], [dict(ASSET_ROW)]):
            self.assertEqual(
                corptools_source.ship_contents_for_character(123, []), []
            )

    def test_ship_contents_none_when_not_installed(self):
        with mock.patch.object(corptools_source, "corptools_installed", lambda: False):
            self.assertIsNone(
                corptools_source.ship_contents_for_character(123, [5000])
            )


class TestExistingToken(TestCase):
    """existing_token reuses a token the player already granted (any AA app)."""

    def test_returns_token_from_scope_filtered_lookup(self):
        sentinel = object()
        chain = mock.MagicMock()
        chain.require_scopes.return_value.require_valid.return_value.first.return_value = sentinel
        with mock.patch("esi.models.Token.objects.filter", return_value=chain) as flt:
            result = esi_assets.existing_token(
                user=mock.Mock(), character_id=42, scopes=esi_assets.ASSET_SCOPES
            )
        self.assertIs(result, sentinel)
        flt.assert_called_once()
        chain.require_scopes.assert_called_once_with(esi_assets.ASSET_SCOPES)
