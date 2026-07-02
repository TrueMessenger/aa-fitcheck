"""Deploy-time Django system checks for fitcheck."""

from __future__ import annotations

import sys

from django.core.checks import Warning, register


@register()
def sde_mirror_loaded_check(app_configs, **kwargs):
    """Warn when fitcheck's SDE mirror is empty - the cause of "Validate my ships"
    silently returning nothing and of fits failing to grade. Stays quiet during
    initial setup (table not migrated yet) and test runs."""
    if "test" in sys.argv:
        return []
    try:
        from .constants import EveCategoryId
        from .models import SdeType

        loaded = SdeType.objects.filter(category_id=EveCategoryId.SHIP).exists()
    except Exception:
        # Table not created yet (pre-migrate / collectstatic / first boot).
        return []
    if loaded:
        return []
    return [
        Warning(
            "fitcheck's EVE static-data (SDE) mirror is empty - ship validation "
            "and compliance grading will return nothing until it is loaded.",
            hint=(
                "Run `python manage.py fitcheck_load_sde`, and schedule "
                "`fitcheck.tasks.update_sde_data` via CELERYBEAT_SCHEDULE so it "
                "stays current."
            ),
            id="fitcheck.W001",
        )
    ]


@register()
def structure_name_task_check(app_configs, **kwargs):
    """Warn when private-structure (Citadel) name resolution looks unscheduled:
    the member-inventory scan has discovered Citadels (pending cache rows exist)
    but none has ever been resolved or even attempted - a strong sign that
    `fitcheck.tasks.refresh_structure_names` isn't in CELERYBEAT_SCHEDULE, so the
    scan will keep showing bare structure ids. Quiet during tests and pre-migrate;
    self-clears once the task runs once."""
    if "test" in sys.argv:
        return []
    try:
        from .models import StructureNameCache

        has_pending = StructureNameCache.objects.filter(resolved_at__isnull=True).exists()
        ever_attempted = StructureNameCache.objects.filter(
            last_attempt_at__isnull=False
        ).exists()
    except Exception:
        # Table not created yet (pre-migrate / collectstatic / first boot).
        return []
    if has_pending and not ever_attempted:
        return [
            Warning(
                "fitcheck has cached private structures (Citadels) awaiting name "
                "resolution, but the refresh task has never run - member-inventory "
                "ship locations will show bare ids.",
                hint=(
                    "Schedule `fitcheck.tasks.refresh_structure_names` via "
                    "CELERYBEAT_SCHEDULE (e.g. daily). FITCHECK_STRUCTURE_CACHE_TTL "
                    "bounds how stale a resolved name can get (default 24h)."
                ),
                id="fitcheck.W002",
            )
        ]
    return []


@register()
def snapshot_task_check(app_configs, **kwargs):
    """Warn when compliance-snapshot collection looks unscheduled or stalled:
    the plugin is in real use (active doctrines with submissions) but no
    snapshot row has been written recently. Trend history cannot be backfilled,
    so every unscheduled day is reporting data lost. Quiet during tests and
    pre-migrate; self-clears once the task runs."""
    import datetime as dt

    from django.utils import timezone

    if "test" in sys.argv:
        return []
    try:
        from .models import ComplianceSnapshot, Doctrine, FitSubmission

        in_use = (
            Doctrine.objects.filter(is_active=True).exists()
            and FitSubmission.objects.exists()
        )
        newest = (
            ComplianceSnapshot.objects.order_by("-date")
            .values_list("date", flat=True)
            .first()
        )
    except Exception:
        # Table not created yet (pre-migrate / collectstatic / first boot).
        return []
    if not in_use:
        return []
    if newest is not None and newest >= timezone.now().date() - dt.timedelta(days=3):
        return []
    return [
        Warning(
            "fitcheck's compliance-snapshot task has "
            + ("never run" if newest is None else f"not run since {newest}")
            + " - compliance reports will have no trend history for this period "
            "(it cannot be backfilled).",
            hint=(
                "Schedule `fitcheck.tasks.take_compliance_snapshots` via "
                "CELERYBEAT_SCHEDULE (daily), or trigger it from Settings > "
                "Diagnostics & health. FITCHECK_SNAPSHOT_RETENTION_DAYS bounds "
                "how much history is kept (default 365; 0 = keep forever)."
            ),
            id="fitcheck.W003",
        )
    ]
