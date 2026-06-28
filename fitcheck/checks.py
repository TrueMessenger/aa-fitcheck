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
