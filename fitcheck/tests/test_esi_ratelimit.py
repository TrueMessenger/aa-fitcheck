"""ESI rate-limit safety for bulk member scans:

1. Phase 1 (listing) reads only the ship rows and does NOT retain the whole
   asset tree; Phase 2 (grading) resolves a character's contents once and feeds
   that slice to build_parsed_fit, which does not re-fetch when given assets.
2. A scan aborts immediately when ESI signals its error limit (HTTP 420/429),
   rather than hammering on and risking an application ban.
"""

from unittest import mock

from django.test import TestCase

from allianceauth.eveonline.models import EveCharacter
from ..services import esi_assets
from ..services.esi_assets import (
    build_parsed_fit,
    get_inventory_for_characters,
    is_error_limited,
)
from .testdata.sde_fixtures import T, create_sde_testdata

# Minimal ESI-shaped asset rows: a Harbinger hull with one fitted Heat Sink II.
ASSETS = [
    {"item_id": 1, "type_id": T.HARBINGER, "location_id": 60003760,
     "location_flag": "Hangar", "quantity": 1, "is_singleton": True},
    {"item_id": 2, "type_id": T.HEAT_SINK_II, "location_id": 1,
     "location_flag": "LoSlot0", "quantity": 1, "is_singleton": False},
]


class _Resp:
    status_code = 420


class _ErrorLimited(Exception):
    response = _Resp()


class EsiRateLimitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.char = EveCharacter.objects.create(
            character_id=12345, character_name="Scout", corporation_id=2001,
            corporation_name="Corp", corporation_ticker="CRP", security_status=0,
        )

    def test_is_error_limited_detects_420_429(self):
        self.assertTrue(is_error_limited(_ErrorLimited()))
        self.assertFalse(is_error_limited(ValueError("nope")))

    def test_phase1_lists_ships_with_one_fetch_and_no_tree_stash(self):
        """Phase 1 lists ships from a single asset fetch and does NOT retain the
        tree - the old per-character whole-tree stash is gone (that was the
        alliance-scale memory blowup)."""
        token = object()
        with mock.patch.object(esi_assets, "tokens_by_character", return_value={12345: token}), \
                mock.patch.object(esi_assets, "_fetch_assets", return_value=ASSETS) as fetch, \
                mock.patch.object(esi_assets, "_fetch_asset_names", return_value={1: "My Harb"}), \
                mock.patch.object(esi_assets, "_resolve_locations", return_value={}):
            inv = get_inventory_for_characters([self.char], hull_type_id=T.HARBINGER)
        self.assertEqual(len(inv.ships), 1)
        self.assertEqual(fetch.call_count, 1)  # one fetch to list
        self.assertFalse(hasattr(inv, "assets_by_character"))  # nothing stashed

    def test_phase2_grades_from_one_fetch_per_character(self):
        """Phase 2 resolves a character's contents ONCE (resolve_contents) and
        feeds that slice to build_parsed_fit, which does not re-fetch when given
        assets - so grading a selected ship is one fetch, not one-per-ship."""
        token = object()
        with mock.patch.object(esi_assets, "_fetch_assets", return_value=ASSETS) as fetch, \
                mock.patch.object(esi_assets, "_verify_mutated_items"):
            contents = esi_assets.resolve_contents(12345, [1], token)
            self.assertEqual(fetch.call_count, 1)  # one fetch for the character
            parsed = build_parsed_fit(
                None, 12345, 1, assets=contents, token=token, fit_name="My Harb",
            )
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.ship_type_id, T.HARBINGER)
            self.assertEqual(fetch.call_count, 1)  # build reused the slice

    def test_scan_aborts_on_error_limit(self):
        with mock.patch.object(esi_assets, "tokens_by_character", return_value={12345: object()}), \
                mock.patch.object(esi_assets, "_fetch_assets", side_effect=_ErrorLimited()):
            inv = get_inventory_for_characters([self.char], hull_type_id=T.HARBINGER)
            self.assertTrue(inv.error_limited)
            self.assertEqual(inv.ships, [])

    def test_member_scan_does_no_live_name_resolution(self):
        """The bulk member scan no longer resolves ship names / private-structure
        locations live (that was the ESI-error-limit storm) - it reads names from
        the local cache instead. So an error-limit in `_fetch_asset_names` cannot
        even occur on this path: it is never called."""
        with mock.patch.object(esi_assets, "tokens_by_character", return_value={12345: object()}), \
                mock.patch.object(esi_assets, "_fetch_assets", return_value=ASSETS), \
                mock.patch.object(esi_assets, "_fetch_asset_names", side_effect=_ErrorLimited()) as fan, \
                mock.patch.object(esi_assets, "_resolve_structure", side_effect=_ErrorLimited()) as rstruct:
            inv = get_inventory_for_characters([self.char], hull_type_id=T.HARBINGER)
        fan.assert_not_called()
        rstruct.assert_not_called()
        self.assertFalse(inv.error_limited)
        self.assertEqual(len(inv.ships), 1)

    def test_self_inventory_aborts_when_name_resolution_hits_error_limit(self):
        """H3 (now on the self-inventory path, which still resolves live, low N):
        an error limit raised by the secondary name/location resolution must abort
        the scan and surface error_limited, rather than being swallowed."""
        from allianceauth.authentication.models import CharacterOwnership
        from django.contrib.auth.models import User

        owner = User.objects.create_user("scout-owner", password="x")
        CharacterOwnership.objects.create(
            user=owner, character=self.char, owner_hash="owner-hash-12345"
        )
        with mock.patch.object(
                    esi_assets, "user_tokens_by_character",
                    return_value=({12345: object()}, []),
                ), \
                mock.patch.object(esi_assets, "_fetch_assets", return_value=ASSETS), \
                mock.patch.object(esi_assets, "_fetch_asset_names", side_effect=_ErrorLimited()):
            inv = esi_assets.get_ship_inventory(owner)
        self.assertTrue(inv.error_limited)
        self.assertEqual(inv.ships, [])

    def _abyssal(self, n):
        from ..constants import Section
        from ..services.fit_data import FitItem
        items = [
            FitItem(section=Section.MED, type_id=T.WEB_ABYSSAL, quantity=1, source_item_id=1000 + i)
            for i in range(n)
        ]
        rows = [{"type_id": T.WEB_ABYSSAL, "item_id": 1000 + i} for i in range(n)]
        return items, rows

    def _dogma_client(self, side_effect=None, result=None):
        op = mock.Mock()
        if side_effect is not None:
            op.result.side_effect = side_effect
        else:
            op.result.return_value = result or {"dogma_attributes": []}
        client = mock.Mock()
        client.Dogma.GetDogmaDynamicItemsTypeIdItemId.return_value = op
        provider = mock.Mock()
        provider.client = client
        return provider, client

    def test_verify_mutated_items_caps_lookups(self):
        """H4: the per-ship abyssal verification fan-out is bounded."""
        from ..services.esi_assets import _MAX_DYNAMIC_ITEM_LOOKUPS, _verify_mutated_items
        items, rows = self._abyssal(_MAX_DYNAMIC_ITEM_LOOKUPS + 5)
        provider, client = self._dogma_client()
        with mock.patch.object(esi_assets, "esi_client", return_value=provider):
            _verify_mutated_items(items, rows)
        self.assertEqual(
            client.Dogma.GetDogmaDynamicItemsTypeIdItemId.call_count,
            _MAX_DYNAMIC_ITEM_LOOKUPS,
        )

    def test_verify_mutated_items_reraises_on_error_limit(self):
        """H4/H3: error limit during dynamic-item verification aborts cleanly."""
        from ..services.esi_assets import _verify_mutated_items
        items, rows = self._abyssal(3)
        provider, _client = self._dogma_client(side_effect=_ErrorLimited())
        with mock.patch.object(esi_assets, "esi_client", return_value=provider):
            with self.assertRaises(_ErrorLimited):
                _verify_mutated_items(items, rows)


def _ship_tree(*ship_item_ids):
    """A minimal asset tree holding the given Harbinger hulls, each with one
    fitted Heat Sink II."""
    rows = []
    for i, ship_id in enumerate(ship_item_ids):
        rows.append(
            {"item_id": ship_id, "type_id": T.HARBINGER, "location_id": 60003760,
             "location_flag": "Hangar", "quantity": 1, "is_singleton": True}
        )
        rows.append(
            {"item_id": 9000 + i, "type_id": T.HEAT_SINK_II, "location_id": ship_id,
             "location_flag": "LoSlot0", "quantity": 1, "is_singleton": False}
        )
    return rows


class SelfAuditBatchingTests(TestCase):
    """My Ships self-audit POST: grading several selected ships costs ONE
    asset-tree / ship-name / implant fetch per character - never one per ship -
    and a character_id outside the requester's ownerships is dropped before
    any asset source (incl. the token-less corptools cache) is consulted."""

    @classmethod
    def setUpTestData(cls):
        from allianceauth.authentication.models import CharacterOwnership

        from .testdata.factories import create_user

        create_sde_testdata()
        cls.user = create_user("selfauditor")
        cls.char1 = cls.user.profile.main_character
        CharacterOwnership.objects.create(
            user=cls.user, character=cls.char1, owner_hash="hash-selfaudit-1"
        )
        cls.char2 = EveCharacter.objects.create(
            character_id=77777, character_name="Alt", corporation_id=2001,
            corporation_name="Corp", corporation_ticker="CRP", security_status=0,
        )
        CharacterOwnership.objects.create(
            user=cls.user, character=cls.char2, owner_hash="hash-selfaudit-2"
        )

    def _post_selections(self, selections):
        from django.urls import reverse

        cid1 = self.char1.character_id
        cid2 = self.char2.character_id
        trees = {cid1: _ship_tree(11, 12), cid2: _ship_tree(21)}
        token = object()
        self.client.force_login(self.user)
        with mock.patch.object(
                    esi_assets, "user_tokens_by_character",
                    return_value=({cid1: token, cid2: token}, []),
                ), \
                mock.patch.object(
                    esi_assets, "resolve_assets",
                    side_effect=lambda cid, tok=None: trees.get(cid),
                ) as resolve, \
                mock.patch.object(
                    esi_assets, "_fetch_asset_names", return_value={}
                ) as names, \
                mock.patch.object(
                    esi_assets, "get_active_implants", return_value={19540}
                ) as implants, \
                mock.patch.object(esi_assets, "_verify_mutated_items"), \
                mock.patch(
                    "fitcheck.views.member.validate_parsed_ship", return_value=[]
                ) as validate:
            response = self.client.post(
                reverse("fitcheck:ship_inventory"), {"ships": selections}
            )
        return response, resolve, names, implants, validate

    def test_one_fetch_per_character_not_per_ship(self):
        cid1 = self.char1.character_id
        cid2 = self.char2.character_id
        response, resolve, names, implants, validate = self._post_selections(
            [f"{cid1}:11", f"{cid1}:12", f"{cid2}:21"]
        )
        self.assertEqual(response.status_code, 200)
        # 3 ships across 2 characters -> exactly 2 of each per-character fetch.
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(names.call_count, 2)
        self.assertEqual(implants.call_count, 2)
        self.assertEqual(validate.call_count, 3)
        # Every graded fit carries the (once-fetched) implants.
        for call in validate.call_args_list:
            parsed = call.args[1]
            self.assertEqual(parsed.pilot_implant_type_ids, {19540})

    def test_unowned_character_is_dropped_without_asset_lookup(self):
        response, resolve, names, implants, validate = self._post_selections(
            ["99999999:11"]
        )
        resolve.assert_not_called()
        implants.assert_not_called()
        validate.assert_not_called()
