from django.db import models
from django.utils.translation import gettext_lazy as _


class VerificationMode(models.TextChoices):
    """How a verification concern (implants, fuel, boosters, FEB) is enforced.

    A per-item ``SubstitutionPolicy.ANY`` always means "no requirement" and is
    skipped before the mode is consulted - the mode governs how *real*
    requirements are treated."""

    REJECT = "REJECT", _("Reject - Always Enforce")
    POLICY = "POLICY", _("Enforce by policy")
    WARN = "WARN", _("Pass - Warning")
    IGNORE = "IGNORE", _("Pass - Never Enforce")


class EnforcementSettings(models.Model):
    """Singleton of site-wide enforcement modes for the verification concerns the
    compliance engine can't always verify (implants/fuel/boosters from EFT, FEB
    from ESI). Defaults preserve the historical behaviour: implants enforced by
    policy; fuel and boosters warn-only; FEB not checked."""

    implant_mode = models.CharField(
        max_length=6, choices=VerificationMode.choices, default=VerificationMode.POLICY
    )
    fuel_mode = models.CharField(
        max_length=6, choices=VerificationMode.choices, default=VerificationMode.WARN
    )
    booster_mode = models.CharField(
        max_length=6, choices=VerificationMode.choices, default=VerificationMode.WARN
    )
    feb_mode = models.CharField(
        max_length=6, choices=VerificationMode.choices, default=VerificationMode.IGNORE
    )
    # Grace window for compliance *consequences* of staleness: for this many
    # days after the relevant fit/policy change, a stale-but-passing submission
    # still counts as current for the Python API and Secure Groups. The stale
    # badge and pilot notifications are always immediate regardless. 0 = no
    # grace (compliance expires the moment the change lands).
    stale_grace_days = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Enforcement settings"
        verbose_name_plural = "Enforcement settings"

    def __str__(self) -> str:
        return "Fit Check enforcement settings"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce a single row
        super().save(*args, **kwargs)

    @classmethod
    def current(cls) -> "EnforcementSettings":
        """The single settings row, created with defaults on first access."""
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class ScanParameters(models.Model):
    """Singleton of admin-tunable scan/result limits (Settings -> Scan & Result
    Limits). Each bounds how much work a single page or scan may do; the
    defaults are sized so a large-alliance install stays inside typical web
    worker timeouts and ESI's error budget."""

    # Live-ESI fallback budget for the member-inventory scan: members without a
    # corptools sync each cost a full asset-tree ESI fetch (~1-3s, serial,
    # inside the page load). corptools-synced members never count against it.
    member_scan_esi_budget = models.PositiveIntegerField(default=25)
    # Ships graded per "Audit selected" POST on the member-inventory page.
    audit_ships_per_post = models.PositiveIntegerField(default=50)
    # ESI dynamic-item lookups per ship when verifying abyssal modules; rolls
    # past the budget stay unverified (warn-only).
    abyssal_lookups_per_ship = models.PositiveIntegerField(default=25)
    # Page size for the paginated lists (review queue, pilot history,
    # Fittings & Standards, Reports drill-down).
    results_per_page = models.PositiveIntegerField(default=50)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Scan parameters"
        verbose_name_plural = "Scan parameters"

    def __str__(self) -> str:
        return "Fit Check scan parameters"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce a single row
        super().save(*args, **kwargs)

    @classmethod
    def current(cls) -> "ScanParameters":
        """The single parameters row, created with defaults on first access."""
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj
