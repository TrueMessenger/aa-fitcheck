import re

from django.contrib.auth.models import User
from django.db import models
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from allianceauth.eveonline.models import EveCharacter

from ..constants import Section
from ..managers import FitSubmissionQuerySet
from .doctrine import Doctrine, DoctrineFit

# Matches an EFT header `[Hull, Custom Name]` and captures the name half.
_EFT_HEADER_RE = re.compile(r"^\s*\[\s*[^,\]]+\s*,\s*(.+?)\s*\]\s*$")


class FitSubmission(models.Model):
    class Source(models.TextChoices):
        EFT = "EFT", _("EFT paste")
        ESI = "ESI", _("ESI import")

    class Status(models.TextChoices):
        PENDING = "P", _("Pending review")
        APPROVED = "A", _("Approved")
        REJECTED = "R", _("Rejected")

    class Verdict(models.TextChoices):
        COMPLIANT = "C", _("Compliant")
        COMPLIANT_SUBS = "S", _("Compliant with substitutions")
        NON_COMPLIANT = "N", _("Non-compliant")
        ERROR = "E", _("Could not be checked")

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    character = models.ForeignKey(
        EveCharacter, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    doctrine_fit = models.ForeignKey(
        DoctrineFit, on_delete=models.CASCADE, related_name="submissions"
    )
    # The doctrine whose per-(doctrine, fit) policy snapshot this submission was
    # graded against. NULL = graded against the fit's source-level defaults
    # (standalone fit, or no doctrine chosen). SET_NULL keeps submission history
    # intact when a doctrine is later deleted.
    doctrine = models.ForeignKey(
        Doctrine,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Doctrine whose policy snapshot this submission was graded against. "
        "Empty = graded against the fit's source-level defaults.",
    )
    fit_version = models.PositiveIntegerField(
        help_text="Version of the doctrine fit this submission was checked against."
    )
    source = models.CharField(max_length=3, choices=Source.choices)
    eft_text = models.TextField(
        blank=True, help_text="Raw paste, or the ESI fit rendered to EFT for reviewers."
    )
    esi_fitting_id = models.PositiveIntegerField(null=True, blank=True)
    # The ESI asset item_id of the ship this was built from, when sourced from a
    # pilot's inventory. Lets Re-check re-pull the same ship's latest fit.
    esi_ship_item_id = models.PositiveBigIntegerField(null=True, blank=True)
    ship_type = models.ForeignKey(
        "eveuniverse.EveType", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    verdict = models.CharField(max_length=1, choices=Verdict.choices)
    status = models.CharField(max_length=1, choices=Status.choices, default=Status.PENDING)
    reviewed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_comment = models.TextField(blank=True)
    # Informational: the EVE type_id of the ship sitting in the parent's
    # Frigate Escape Bay at submission time. NULL = empty / non-FEB hull /
    # ESI didn't surface it. Not consulted by the compliance engine.
    frigate_escape_bay_type_id = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = FitSubmissionQuerySet.as_manager()

    class Meta:
        default_permissions = ()
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["status", "verdict", "-created_at"]),
            models.Index(fields=["doctrine_fit", "status"]),
            models.Index(fields=["doctrine", "status"]),
        ]

    def __str__(self) -> str:
        return f"#{self.pk} {self.user} -> {self.doctrine_fit} [{self.get_verdict_display()}]"

    @property
    def is_stale(self) -> bool:
        """True when the doctrine fit changed after this submission was checked."""
        return self.fit_version != self.doctrine_fit.version

    @cached_property
    def ship_name(self) -> str:
        """In-game custom ship name parsed from the EFT header (`[Hull, Name]`).
        Empty when the submission carries no EFT text (e.g. ESI-source with the
        rendering switched off) or the header is malformed. Reviewers use this
        to tell apart two same-hull submissions from one pilot."""
        if not self.eft_text:
            return ""
        first_line = self.eft_text.lstrip().split("\n", 1)[0]
        match = _EFT_HEADER_RE.match(first_line)
        return match.group(1).strip() if match else ""


class SubmissionItem(models.Model):
    class MutationSource(models.TextChoices):
        EFT_PYFA = "PYFA", _("Pyfa EFT export")
        MANUAL = "MAN", _("Self-reported")
        ESI_VERIFIED = "ESI", _("ESI verified")

    submission = models.ForeignKey(FitSubmission, on_delete=models.CASCADE, related_name="items")
    section = models.CharField(max_length=8, choices=Section.choices)
    eve_type = models.ForeignKey("eveuniverse.EveType", on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField(default=1)
    charge_type = models.ForeignKey(
        "eveuniverse.EveType", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # {"<attr_id>": rolled_value} for abyssal modules, else null.
    mutated_attributes = models.JSONField(null=True, blank=True)
    mutation_source = models.CharField(
        max_length=4, choices=MutationSource.choices, blank=True, default=""
    )

    class Meta:
        default_permissions = ()

    def __str__(self) -> str:
        return f"{self.submission_id}: {self.quantity}x {self.eve_type} [{self.section}]"


class ComplianceFinding(models.Model):
    class Code(models.TextChoices):
        OK = "OK", _("Exact match")
        SUBSTITUTE = "SUB", _("Allowed substitute")
        CARGO_REFIT = "REF", _("Carried in cargo as refit")
        FITTED_REFIT = "FRF", _("Fitted to ship")
        NO_ENFORCEMENT = "ANY", _("No enforcement")
        NOT_ALLOWED = "BAD", _("Not an allowed substitute")
        MISSING = "MIS", _("Missing")
        EXTRA = "EXT", _("Not part of the fit")
        QTY_SHORT = "QTY", _("Quantity short")
        WRONG_HULL = "HUL", _("Wrong hull")
        UNRESOLVED = "UNR", _("Unrecognized item")
        IMPLANT_MISSING = "IMP", _("Required implant missing")
        UNVERIFIED = "UNV", _("Listed but not verifiable")

    submission = models.ForeignKey(FitSubmission, on_delete=models.CASCADE, related_name="findings")
    section = models.CharField(max_length=8, choices=Section.choices, blank=True, default="")
    code = models.CharField(max_length=3, choices=Code.choices)
    expected_type = models.ForeignKey(
        "eveuniverse.EveType", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    actual_type = models.ForeignKey(
        "eveuniverse.EveType", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    expected_qty = models.PositiveIntegerField(null=True, blank=True)
    actual_qty = models.PositiveIntegerField(null=True, blank=True)
    message = models.CharField(max_length=500)
    # [{"type_id": int, "name": str}, ...]
    allowed_alternatives = models.JSONField(default=list, blank=True)
    # [{"attribute": str, "required": float, "actual": float, "passed": bool}, ...]
    attribute_results = models.JSONField(default=list, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        default_permissions = ()
        ordering = ["sort_order", "pk"]

    def __str__(self) -> str:
        return f"{self.submission_id}: {self.code} {self.message[:60]}"


class SubmissionActionLog(models.Model):
    class Action(models.TextChoices):
        SUBMITTED = "SUB", _("Submitted")
        AUTO_CHECKED = "CHK", _("Auto-checked")
        RECHECKED = "RCK", _("Re-checked")
        APPROVED = "APP", _("Approved")
        REJECTED = "REJ", _("Rejected")
        COMMENTED = "COM", _("Commented")

    submission = models.ForeignKey(FitSubmission, on_delete=models.CASCADE, related_name="log")
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=3, choices=Action.choices)
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        default_permissions = ()
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.submission_id}: {self.get_action_display()} by {self.actor}"
