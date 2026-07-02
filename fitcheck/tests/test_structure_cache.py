"""Private-structure (Citadel) name cache + the out-of-band refresh task.

The member-inventory scan must resolve Citadel names from the local cache only -
resolving them live across every pilot's structure token is what tripped ESI's
error limit. These tests lock in: the bulk scan makes ZERO ESI name/structure
calls, self-inventory still resolves live, and the refresh task resolves with a
bounded fan-out, negative caching + backoff, and a clean stop on error-limit.
"""

from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from ..models import StructureNameCache
from ..services import esi_assets, structure_cache
from .testdata.sde_fixtures import T, create_sde_testdata

STRUCT = 10**12 + 5  # a private upwell structure id


# --- mock ESI client / exceptions -------------------------------------------


class _Resp403:
    status_code = 403


class _Forbidden(Exception):
    """403 (no docking access) - NOT error-limited; _resolve_structure skips it."""

    def __init__(self):
        self.response = _Resp403()


class _Resp420:
    status_code = 420


class _ErrorLimited(Exception):
    """420 (ESI error limit) - is_error_limited() True; aborts the run."""

    def __init__(self):
        self.response = _Resp420()


def _provider_returning(data):
    provider = mock.Mock()
    op = mock.Mock()
    op.return_value.result.return_value = data
    provider.client.Universe.GetUniverseStructuresStructureId = op
    return provider, op


def _provider_raising(exc):
    provider = mock.Mock()
    op = mock.Mock()
    op.return_value.result.side_effect = exc
    provider.client.Universe.GetUniverseStructuresStructureId = op
    return provider, op


def _ship_row(location_id=STRUCT, item_id=5000):
    return {
        "item_id": item_id,
        "type_id": T.HARBINGER,
        "location_id": location_id,
        "location_flag": "Hangar",
        "quantity": 1,
        "is_singleton": True,
        "name": "",
    }


# --- local-only helpers (no ESI) --------------------------------------------


class LocalCacheHelpersTests(TestCase):
    def test_ensure_pending_creates_and_preserves_resolved(self):
        structure_cache.ensure_pending({STRUCT, STRUCT + 1})
        self.assertEqual(StructureNameCache.objects.count(), 2)
        row = StructureNameCache.objects.get(structure_id=STRUCT)
        row.name = "Keepstar"
        row.resolved_at = timezone.now()
        row.save()
        # Re-running must not overwrite the resolved name (ignore_conflicts).
        structure_cache.ensure_pending({STRUCT, STRUCT + 1, STRUCT + 2})
        row.refresh_from_db()
        self.assertEqual(row.name, "Keepstar")
        self.assertEqual(StructureNameCache.objects.count(), 3)

    def test_ensure_pending_empty_is_noop(self):
        structure_cache.ensure_pending(set())
        self.assertEqual(StructureNameCache.objects.count(), 0)

    def test_names_for_structures_returns_only_cached_with_staleness(self):
        now = timezone.now()
        StructureNameCache.objects.create(
            structure_id=1, name="Fresh", solar_system_id=30000001, resolved_at=now
        )
        StructureNameCache.objects.create(
            structure_id=2, name="Old", resolved_at=now - timedelta(days=2)
        )
        StructureNameCache.objects.create(structure_id=3)  # pending
        out = structure_cache.names_for_structures({1, 2, 3, 999})
        self.assertNotIn(999, out)  # uncached id absent
        self.assertEqual(out[1]["name"], "Fresh")
        self.assertFalse(out[1]["stale"])
        self.assertTrue(out[2]["stale"])  # older than the 24h default TTL
        self.assertTrue(out[3]["stale"])  # pending counts as stale

    @override_settings(FITCHECK_STRUCTURE_CACHE_TTL=10)
    def test_ttl_override_is_honoured(self):
        row = StructureNameCache.objects.create(
            structure_id=1, name="X", resolved_at=timezone.now() - timedelta(minutes=1)
        )
        out = structure_cache.names_for_structures({row.structure_id})
        self.assertTrue(out[1]["stale"])  # 1 min old, TTL 10s


# --- _build_owned_ships branch behaviour ------------------------------------


class BuildOwnedShipsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_resolve_names_false_is_cache_only(self):
        token = mock.Mock()
        with mock.patch.object(esi_assets, "_fetch_asset_names") as fan, \
             mock.patch.object(esi_assets, "_resolve_structure") as rstruct, \
             mock.patch.object(
                 esi_assets, "_ship_group_names",
                 return_value={T.HARBINGER: "Battlecruiser"},
             ):
            out = esi_assets._build_owned_ships(
                777, "Cap", [_ship_row()], token, resolve_names=False
            )
        # No live ESI for names or private-structure locations.
        fan.assert_not_called()
        rstruct.assert_not_called()
        self.assertEqual(out[0].ship_name, "")  # template falls back to type name
        self.assertEqual(out[0].location_name, f"Structure {STRUCT}")
        # The unseen structure was queued for the refresh task.
        self.assertTrue(
            StructureNameCache.objects.filter(
                structure_id=STRUCT, resolved_at__isnull=True
            ).exists()
        )

    def test_resolve_names_false_surfaces_cached_name(self):
        StructureNameCache.objects.create(
            structure_id=STRUCT, name="Sotiyo", resolved_at=timezone.now()
        )
        token = mock.Mock()
        with mock.patch.object(esi_assets, "_fetch_asset_names") as fan, \
             mock.patch.object(esi_assets, "_resolve_structure") as rstruct, \
             mock.patch.object(
                 esi_assets, "_ship_group_names",
                 return_value={T.HARBINGER: "Battlecruiser"},
             ):
            out = esi_assets._build_owned_ships(
                777, "Cap", [_ship_row()], token, resolve_names=False
            )
        fan.assert_not_called()
        rstruct.assert_not_called()
        self.assertEqual(out[0].location_name, "Sotiyo")

    def test_resolve_names_true_fetches_names_live_but_locations_cached(self):
        """Self-inventory (#39): custom ship names still come from one live
        batched call, but locations ALWAYS come from the StructureNameCache -
        the live per-Citadel lookup is gone entirely."""
        StructureNameCache.objects.create(
            structure_id=STRUCT, name="Sotiyo", resolved_at=timezone.now()
        )
        token = mock.Mock()
        with mock.patch.object(
                 esi_assets, "_fetch_asset_names", return_value={5000: "Live Name"}
             ) as fan, \
             mock.patch.object(esi_assets, "_resolve_structure") as rstruct, \
             mock.patch.object(
                 esi_assets, "_ship_group_names",
                 return_value={T.HARBINGER: "Battlecruiser"},
             ):
            out = esi_assets._build_owned_ships(
                777, "Cap", [_ship_row()], token, resolve_names=True
            )
        fan.assert_called_once()
        rstruct.assert_not_called()
        self.assertEqual(out[0].ship_name, "Live Name")
        self.assertEqual(out[0].location_name, "Sotiyo")

    def test_system_region_cached_never_fetches(self):
        # An uncached system resolves to ("", "") rather than triggering an ESI load.
        self.assertEqual(esi_assets._system_region_cached(30000999), ("", ""))


# --- end-to-end bulk scan: ZERO ESI -----------------------------------------


class MemberScanNoEsiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def _make_char(self):
        from allianceauth.eveonline.models import EveCharacter

        return EveCharacter.objects.create(
            character_id=777, character_name="Cap",
            corporation_id=1, corporation_name="C", corporation_ticker="C",
            alliance_id=1, alliance_name="A", alliance_ticker="A",
            security_status=0,
        )

    def test_bulk_scan_makes_no_esi_name_or_structure_calls(self):
        char = self._make_char()
        token = mock.Mock()
        # Two ships in the SAME citadel -> exactly one pending cache row (dedupe).
        ships = [_ship_row(item_id=5000), _ship_row(item_id=5001)]
        with mock.patch.object(esi_assets, "tokens_by_character", return_value={777: token}), \
             mock.patch(
                 "fitcheck.services.corptools_source.ship_assets_for_character",
                 return_value=ships,
             ), \
             mock.patch.object(
                 esi_assets, "_ship_group_names",
                 return_value={T.HARBINGER: "Battlecruiser"},
             ), \
             mock.patch.object(esi_assets, "_fetch_asset_names") as fan, \
             mock.patch.object(esi_assets, "_resolve_structure") as rstruct:
            inventory = esi_assets.get_inventory_for_characters(
                [char], hull_type_id=T.HARBINGER
            )
        fan.assert_not_called()
        rstruct.assert_not_called()
        self.assertFalse(inventory.error_limited)
        self.assertEqual([s.type_id for s in inventory.ships], [T.HARBINGER, T.HARBINGER])
        self.assertEqual(
            StructureNameCache.objects.filter(structure_id=STRUCT).count(), 1
        )


# --- the refresh task / resolver --------------------------------------------


class ResolvePendingAndStaleTests(TestCase):
    def test_happy_path_persists_name_and_warms_system(self):
        row = StructureNameCache.objects.create(structure_id=STRUCT)
        provider, op = _provider_returning(
            {"name": "Keepstar", "solar_system_id": 30000001}
        )
        with mock.patch.object(esi_assets, "all_structure_tokens", return_value=[mock.Mock()]), \
             mock.patch.object(esi_assets, "esi_client", return_value=provider), \
             mock.patch.object(esi_assets, "_system_region") as sysreg:
            summary = structure_cache.resolve_pending_and_stale()
        row.refresh_from_db()
        self.assertEqual(row.name, "Keepstar")
        self.assertEqual(row.solar_system_id, 30000001)
        self.assertIsNotNone(row.resolved_at)
        self.assertTrue(row.accessible)
        self.assertEqual(row.fail_count, 0)
        sysreg.assert_called_once_with(30000001)
        self.assertEqual(summary["resolved"], 1)

    def test_bounded_fanout_and_negative_cache(self):
        row = StructureNameCache.objects.create(structure_id=STRUCT)
        tokens = [mock.Mock() for _ in range(10)]
        provider, op = _provider_raising(_Forbidden())  # every token 403s
        with mock.patch.object(esi_assets, "all_structure_tokens", return_value=tokens), \
             mock.patch.object(esi_assets, "esi_client", return_value=provider):
            summary = structure_cache.resolve_pending_and_stale()
        # At most _MAX_TOKEN_ATTEMPTS calls for one structure - NOT len(tokens).
        self.assertLessEqual(op.call_count, structure_cache._MAX_TOKEN_ATTEMPTS)
        row.refresh_from_db()
        self.assertFalse(row.accessible)
        self.assertEqual(row.fail_count, 1)
        self.assertIsNotNone(row.last_attempt_at)
        self.assertIsNone(row.name)
        self.assertEqual(summary["failed"], 1)

    def test_error_limit_aborts_cleanly(self):
        StructureNameCache.objects.create(structure_id=STRUCT)
        StructureNameCache.objects.create(structure_id=STRUCT + 1)
        provider, op = _provider_raising(_ErrorLimited())
        with mock.patch.object(esi_assets, "all_structure_tokens", return_value=[mock.Mock()]), \
             mock.patch.object(esi_assets, "esi_client", return_value=provider):
            summary = structure_cache.resolve_pending_and_stale()
        self.assertTrue(summary["aborted"])
        self.assertEqual(op.call_count, 1)  # stopped after the first error-limit
        self.assertFalse(
            StructureNameCache.objects.exclude(name__isnull=True).exists()
        )

    def test_warm_system_error_limit_aborts_after_persist(self):
        row = StructureNameCache.objects.create(structure_id=STRUCT)
        provider, op = _provider_returning(
            {"name": "Keepstar", "solar_system_id": 30000001}
        )
        with mock.patch.object(esi_assets, "all_structure_tokens", return_value=[mock.Mock()]), \
             mock.patch.object(esi_assets, "esi_client", return_value=provider), \
             mock.patch.object(esi_assets, "_system_region", side_effect=_ErrorLimited()):
            summary = structure_cache.resolve_pending_and_stale()
        row.refresh_from_db()
        self.assertEqual(row.name, "Keepstar")  # name persisted before the warm step
        self.assertTrue(summary["aborted"])

    @override_settings(FITCHECK_STRUCTURE_CACHE_TTL=10)
    def test_only_stale_resolved_rows_are_refreshed(self):
        now = timezone.now()
        fresh = StructureNameCache.objects.create(
            structure_id=STRUCT, name="Fresh", resolved_at=now
        )
        stale = StructureNameCache.objects.create(
            structure_id=STRUCT + 1, name="OldName", resolved_at=now - timedelta(hours=1)
        )
        provider, op = _provider_returning({"name": "NewName", "solar_system_id": None})
        with mock.patch.object(esi_assets, "all_structure_tokens", return_value=[mock.Mock()]), \
             mock.patch.object(esi_assets, "esi_client", return_value=provider):
            structure_cache.resolve_pending_and_stale()
        fresh.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(fresh.name, "Fresh")  # within TTL -> untouched
        self.assertEqual(stale.name, "NewName")
        self.assertEqual(op.call_count, 1)

    def test_no_tokens_is_noop(self):
        row = StructureNameCache.objects.create(structure_id=STRUCT)
        with mock.patch.object(esi_assets, "all_structure_tokens", return_value=[]):
            summary = structure_cache.resolve_pending_and_stale()
        row.refresh_from_db()
        self.assertTrue(summary["no_tokens"])
        self.assertIsNone(row.name)
        self.assertIsNone(row.last_attempt_at)


class BackoffTests(TestCase):
    def test_backoff_is_exponential_and_capped(self):
        base = structure_cache._BACKOFF_BASE_SECONDS
        self.assertEqual(structure_cache.backoff_seconds(0), base)
        self.assertEqual(structure_cache.backoff_seconds(1), 2 * base)
        self.assertEqual(
            structure_cache.backoff_seconds(100),
            structure_cache._BACKOFF_MAX_MULT * base,
        )

    def test_negative_cached_skipped_within_backoff_retried_after(self):
        now = timezone.now()
        recent = StructureNameCache.objects.create(
            structure_id=STRUCT, accessible=False, fail_count=1, last_attempt_at=now
        )
        due = StructureNameCache.objects.create(
            structure_id=STRUCT + 1, accessible=False, fail_count=1,
            last_attempt_at=now - timedelta(hours=5),  # past the fail_count=1 backoff (2h)
        )
        provider, op = _provider_returning({"name": "Recovered", "solar_system_id": None})
        with mock.patch.object(esi_assets, "all_structure_tokens", return_value=[mock.Mock()]), \
             mock.patch.object(esi_assets, "esi_client", return_value=provider):
            structure_cache.resolve_pending_and_stale()
        self.assertEqual(op.call_count, 1)  # only the due one
        due.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual(due.name, "Recovered")
        self.assertTrue(due.accessible)
        self.assertFalse(recent.accessible)  # left alone within backoff
