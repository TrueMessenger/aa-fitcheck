from django.conf import settings as django_settings
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


class NotificationSettings(models.Model):
    """Singleton of admin-tunable per-type on/off switches for the Alliance Auth
    notifications Fit Check emits from ``fitcheck.tasks`` (Settings ->
    Notifications). Turning a type off does not delete or skip anything except
    the ``allianceauth.notifications.notify()`` call for it - no Notification
    row is created, so any relay app (e.g. aa-discordnotify) that reads
    Notification rows is silenced too.

    On first-ever access (no row yet) the four fields are seeded from the
    legacy ``FITCHECK_NOTIFY_REVIEWERS`` / ``FITCHECK_REVIEWER_DIGEST`` /
    ``FITCHECK_NOTIFY_PILOTS_STALE`` Django settings, so an existing install's
    configured behaviour survives the upgrade unchanged. After that first
    access this row is authoritative - editing those legacy settings in
    ``local.py`` has no further effect; use the Notification Settings page.
    """

    notify_reviewers_new_submission = models.BooleanField(
        default=True,
        help_text=_(
            "Ping everyone with review authority as soon as a member submits a "
            "fit for review. Has no effect while Reviewer digest (below) is on."
        ),
    )
    reviewer_digest = models.BooleanField(
        default=False,
        help_text=_(
            "Send a periodic summary of the pending queue instead of a ping "
            "per submission. Suppresses the per-submission ping above; "
            "requires fitcheck.tasks.send_review_digest to be scheduled via "
            "CELERYBEAT_SCHEDULE to actually run."
        ),
    )
    notify_member_decision = models.BooleanField(
        default=True,
        help_text=_(
            "Tell a pilot their submission was approved or rejected - covers "
            "both a human reviewer's decision and a doctrine's automatic "
            "'approved by rule' decision."
        ),
    )
    notify_pilots_stale = models.BooleanField(
        default=True,
        help_text=_(
            "Tell a pilot when a fitting-standard change re-grades their "
            "pending submission (with an old-to-new module diff when the BOM "
            "changed), and warn holders of an already-approved submission "
            "that the fit has moved on since their approval."
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Notification settings"
        verbose_name_plural = "Notification settings"

    def __str__(self) -> str:
        return "Fit Check notification settings"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce a single row
        super().save(*args, **kwargs)

    @classmethod
    def current(cls) -> "NotificationSettings":
        """The single settings row. On first-ever access, seeds the four
        fields from the legacy FITCHECK_NOTIFY_*/FITCHECK_REVIEWER_DIGEST
        Django settings (imported here, not at module scope, so tests can
        patch ``fitcheck.app_settings`` and have it take effect on this call)."""
        from ..app_settings import (
            FITCHECK_NOTIFY_PILOTS_STALE,
            FITCHECK_NOTIFY_REVIEWERS,
            FITCHECK_REVIEWER_DIGEST,
        )

        obj, _created = cls.objects.get_or_create(
            pk=1,
            defaults={
                "notify_reviewers_new_submission": FITCHECK_NOTIFY_REVIEWERS,
                "reviewer_digest": FITCHECK_REVIEWER_DIGEST,
                "notify_pilots_stale": FITCHECK_NOTIFY_PILOTS_STALE,
            },
        )
        return obj


class UserNotificationPreference(models.Model):
    """Per-user opt-out of every Fit Check notification. A muted user simply
    never has a Notification row created for them, so muting is fitcheck-wide
    (it also silences a Discord relay app, since those relay existing
    Notification rows rather than intercepting the event)."""

    user = models.OneToOneField(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="fitcheck_notification_preference",
    )
    mute_all = models.BooleanField(
        default=False,
        help_text=_("Suppress every Fit Check notification for this account."),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Notification preference"
        verbose_name_plural = "Notification preferences"

    def __str__(self) -> str:
        return f"{self.user}: {'muted' if self.mute_all else 'unmuted'}"

    @classmethod
    def is_muted(cls, user) -> bool:
        """True when ``user`` has muted Fit Check notifications. Never raises
        when no preference row exists yet (the common case) or when ``user``
        is None/anonymous."""
        if not getattr(user, "pk", None):
            return False
        return cls.objects.filter(user=user, mute_all=True).exists()
