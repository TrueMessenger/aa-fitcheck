"""Local name cache for private upwell structures (Citadels, id >= 1e12).

The proactive member-inventory scan must never resolve Citadel names over ESI:
``GetUniverseStructuresStructureId`` returns 403 when the token-holder can't dock,
and one inaccessible Citadel tried against every structure-scoped token in scope
(up to one per scanned pilot) empties ESI's error budget in seconds. So the scan
reads names ONLY from the ``StructureNameCache`` table via the local-only helpers
here, and the periodic ``refresh_structure_names`` task resolves/refreshes them
out-of-band with a bounded, paced, negatively-cached fan-out.

Nothing here imports ESI at module load - every ESI/eveuniverse import is lazy,
matching ``corptools_source`` / ``esi_assets``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as _tz

logger = logging.getLogger(__name__)

# At most this many structure-scoped tokens are tried per structure per run. The
# whole point of the cache: an unreachable Citadel costs a couple of 403s per run,
# not one per token across the alliance.
_MAX_TOKEN_ATTEMPTS = 3
# Upper bound on rows resolved in a single task run, so the background ESI fan-out
# stays predictable on a fresh install that just discovered thousands of structures.
_DEFAULT_REFRESH_LIMIT = 200
# Negative-cache backoff: an inaccessible structure is retried after
# min(2**fail_count, _BACKOFF_MAX_MULT) * _BACKOFF_BASE_SECONDS.
_BACKOFF_BASE_SECONDS = 3600
_BACKOFF_MAX_MULT = 24

# Private upwell structures use ids at/above this threshold; below it are NPC
# stations / solar systems that resolve from eveuniverse without a token.
PRIVATE_STRUCTURE_MIN_ID = 10**12

_MIN_DT = datetime.min.replace(tzinfo=_tz.utc)


def _ttl() -> int:
    """Seconds before a resolved name is considered stale. Read live from Django
    settings (not the import-time app_settings constant) so override_settings and
    a local.py override both take effect."""
    from django.conf import settings

    return int(getattr(settings, "FITCHECK_STRUCTURE_CACHE_TTL", 86400))


def backoff_seconds(fail_count: int) -> int:
    """How long a negative-cached (inaccessible) structure waits before another
    attempt. Pure function: exponential in fail_count, capped."""
    return min(2 ** fail_count, _BACKOFF_MAX_MULT) * _BACKOFF_BASE_SECONDS


# --- Local-only helpers (used by the scan; never touch ESI) -----------------


def names_for_structures(structure_ids) -> dict[int, dict]:
    """{structure_id: {"name", "solar_system_id", "stale"}} for cached rows among
    `structure_ids`. Missing ids are simply absent. One query, no ESI."""
    from ..models import StructureNameCache

    ids = {int(i) for i in structure_ids}
    if not ids:
        return {}
    stale_before = _now() - timedelta(seconds=_ttl())
    out: dict[int, dict] = {}
    for row in StructureNameCache.objects.filter(structure_id__in=ids):
        out[row.structure_id] = {
            "name": row.name,
            "solar_system_id": row.solar_system_id,
            "stale": row.resolved_at is None or row.resolved_at < stale_before,
        }
    return out


def ensure_pending(structure_ids) -> None:
    """Upsert 'pending' rows for ids not yet cached so the refresh task knows to
    resolve them. One INSERT (ignore_conflicts), never overwrites a resolved row -
    safe to call from a GET request."""
    from ..models import StructureNameCache

    ids = {int(i) for i in structure_ids}
    if not ids:
        return
    StructureNameCache.objects.bulk_create(
        [StructureNameCache(structure_id=i) for i in ids],
        ignore_conflicts=True,
    )


# --- Background resolver (used by refresh_structure_names) -------------------


def resolve_pending_and_stale(limit: int | None = None) -> dict:
    """Resolve pending + stale + retryable negative-cached structures via ESI.

    Bounded fan-out (<= _MAX_TOKEN_ATTEMPTS tokens per structure), negative caching
    with exponential backoff, and a clean stop the moment ESI signals its error
    limit. This is where the (acceptable, paced, background) ESI cost lives -
    never in the member-inventory request path.
    """
    from . import esi_assets

    now = _now()
    work = _select_work(now, limit if limit is not None else _DEFAULT_REFRESH_LIMIT)
    summary = {"attempted": 0, "resolved": 0, "failed": 0, "aborted": False}
    if not work:
        return summary

    tokens = esi_assets.all_structure_tokens()
    if not tokens:
        summary["no_tokens"] = True
        return summary
    tokens = tokens[:_MAX_TOKEN_ATTEMPTS]

    for row in work:
        summary["attempted"] += 1
        try:
            name, system_id = esi_assets._resolve_structure(row.structure_id, tokens)
        except Exception as exc:  # pragma: no cover - network dependent
            if esi_assets.is_error_limited(exc):
                summary["aborted"] = True
                break
            raise

        if name is not None:
            row.name = name
            row.solar_system_id = system_id
            row.resolved_at = now
            row.last_attempt_at = now
            row.accessible = True
            row.fail_count = 0
            row.save(update_fields=[
                "name", "solar_system_id", "resolved_at",
                "last_attempt_at", "accessible", "fail_count",
            ])
            summary["resolved"] += 1
            # Warm the eveuniverse system/region cache so the next scan can label
            # it locally. Non-fatal unless ESI is error-limited (then stop cleanly;
            # the name itself is already persisted above).
            if system_id:
                try:
                    esi_assets._system_region(system_id)
                except Exception as exc:  # pragma: no cover - network dependent
                    if esi_assets.is_error_limited(exc):
                        summary["aborted"] = True
                        break
        else:
            row.last_attempt_at = now
            row.accessible = False
            row.fail_count += 1
            row.save(update_fields=["last_attempt_at", "accessible", "fail_count"])
            summary["failed"] += 1

    return summary


def _select_work(now: datetime, limit: int) -> list:
    """Rows due for resolution: pending (never resolved), stale (older than TTL),
    or negative-cached past their backoff. Pending first, then oldest attempt
    first; capped at `limit`."""
    from django.db.models import Q

    from ..models import StructureNameCache

    stale_before = now - timedelta(seconds=_ttl())
    candidates = StructureNameCache.objects.filter(
        Q(resolved_at__isnull=True, accessible=True)  # pending
        | Q(resolved_at__lt=stale_before)             # stale resolved
        | Q(accessible=False)                          # negative-cached (backoff in Python)
    )

    def _due(row) -> bool:
        if not row.accessible:
            if row.last_attempt_at is None:
                return True
            return row.last_attempt_at < now - timedelta(
                seconds=backoff_seconds(row.fail_count)
            )
        return True

    due = [row for row in candidates if _due(row)]
    due.sort(key=lambda r: (r.resolved_at is not None, r.last_attempt_at or _MIN_DT))
    return due[:limit]


def _now() -> datetime:
    from django.utils import timezone

    return timezone.now()
