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
