from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from ..constants import LEEWAY_SECTIONS, Section
from ..managers import DoctrineFitQuerySet, DoctrineQuerySet


def default_allowed_meta_groups() -> list[int]:
    """New items allow all standard meta groups by default (checked = allowed:
    Tech I/II, Storyline, Faction, Officer, Deadspace). A manager unchecks groups
    to restrict; an empty list means no family substitutes are allowed."""
    return [1, 2, 3, 4, 5, 6]


def _required_quantity(section, quantity: int, min_quantity_pct: int) -> int:
    """Quantity needed to pass, after consumable leeway: consumable sections
    below 100% apply a ``ceil(qty * pct / 100)`` floor; everything else needs the
    full listed quantity. Shared by DoctrineFitItem and AssignmentItemPolicy."""
    if section in LEEWAY_SECTIONS and min_quantity_pct < 100:
        return -(-quantity * min_quantity_pct // 100)  # ceil division
    return quantity


class SubstitutionPolicy(models.TextChoices):
    # Labels use the Strict/Standard/Flexible vocabulary of the named
    # CompliancePolicy presets so the per-module rule reads consistently with the
    # bulk policies; the parenthetical keeps the precise behaviour explicit.
    EXACT = "EX", _("Strict (exact type only)")
    VARIANTS = "VA", _("Standard (variant family)")
    MEET_OR_BEAT = "MB", _("Flexible (meets or beats attributes)")
    ANY = "AN", _("No enforcement (anything accepted)")


class DoctrineCategory(models.Model):
    """A coloured label that ALSO drives visibility. A category groups doctrines
    and fittings and gates who can see them by Alliance Auth group:

    - `selected_groups` (OR): a pilot who has ANY of these groups is admitted.
    - `required_groups` (AND): a pilot who has ALL of these groups is admitted.
    A category with neither set is public. The two are combined with OR, so a
    pilot is admitted if they satisfy the Selected set OR the Required set.

    The background is an arbitrary hex colour; `text_color` picks black or white
    for readable contrast."""

    name = models.CharField(max_length=30, unique=True)
    color = models.CharField(
        max_length=7,
        default="#0d6efd",
        help_text="Background colour as a #rrggbb hex value.",
    )
    selected_groups = models.ManyToManyField(
        Group,
        blank=True,
        related_name="+",
        help_text="Pilots in ANY of these Auth groups may see this category's fits/doctrines.",
    )
    required_groups = models.ManyToManyField(
        Group,
        blank=True,
        related_name="+",
        help_text="Pilots must have ALL of these Auth groups to see this category's fits/doctrines.",
    )
    fits = models.ManyToManyField(
        "DoctrineFit",
        blank=True,
        related_name="categories",
        help_text="Fittings in this category (gated by the groups above).",
    )
    # When this category was synced from the colcrunch `fittings` plugin we
    # record its source row id so a later re-sync matches it across renames.
    # A NULL value marks a hand-made local category, which sync never touches.
    source_plugin_pk = models.PositiveIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Primary key of the source Category in the `fittings` plugin, if synced.",
    )

    class Meta:
        default_permissions = ()
        ordering = ["name"]
        verbose_name_plural = "doctrine categories"

    def __str__(self) -> str:
        return self.name

    @property
    def text_color(self) -> str:
        """Black or white foreground, whichever reads better on `color`.

        Uses the YIQ perceived-brightness heuristic (threshold 128) so light
        categories get dark text and saturated/dark ones get light text -
        matching the convention Bootstrap uses for its semantic colours."""
        value = (self.color or "").lstrip("#")
        if len(value) != 6:
            return "#ffffff"
        try:
            r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return "#ffffff"
        brightness = (r * 299 + g * 587 + b * 114) / 1000
        return "#000000" if brightness >= 128 else "#ffffff"

    def admits(self, user_group_ids: set) -> bool:
        """Whether a pilot with these Auth group ids may see this category's
        fits/doctrines. No restriction = public; otherwise Selected (OR) or
        Required (AND) grants access."""
        sel = set(self.selected_groups.values_list("id", flat=True))
        req = set(self.required_groups.values_list("id", flat=True))
        if not sel and not req:
            return True
        if sel & user_group_ids:
            return True
        if req and req <= user_group_ids:
            return True
        return False


class Doctrine(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    # A ship type used as the doctrine's poster image (rendered from EVE image server).
    image_type_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="EVE ship type whose render illustrates this doctrine.",
    )
    categories = models.ManyToManyField(
        DoctrineCategory,
        blank=True,
        related_name="doctrines",
        help_text="Categories this doctrine belongs to; their groups gate visibility.",
    )
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # When this doctrine was imported from the colcrunch `fittings` plugin,
    # we record its source row id so a later re-sync can find it again.
    source_plugin_pk = models.PositiveIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Primary key of the source row in the `fittings` plugin, if imported.",
    )

    objects = DoctrineQuerySet.as_manager()

    class Meta:
        default_permissions = ()

    def __str__(self) -> str:
        return self.name

    def image_url(self, size: int = 128) -> str | None:
        if self.image_type_id:
            return f"https://images.evetech.net/types/{self.image_type_id}/render?size={size}"
        return None


class EnforcementMode(models.TextChoices):
    """How a compliance policy treats one slot group."""

    EXACT = "EX", _("Exact fit")
    META = "ME", _("Meta level enforcement / exception")
    GTE = "GE", _("Equal to or greater")
    ANY = "AN", _("Any (no enforcement)")


# Map slot-group enforcement to the per-module substitution policy it implies.
ENFORCEMENT_TO_POLICY = {
    EnforcementMode.EXACT: SubstitutionPolicy.EXACT,
    EnforcementMode.META: SubstitutionPolicy.VARIANTS,
    EnforcementMode.GTE: SubstitutionPolicy.MEET_OR_BEAT,
    EnforcementMode.ANY: SubstitutionPolicy.ANY,
}

# Slot groups a policy can override (fuel/consumables live in CARGO).
POLICY_SECTIONS = (
    Section.HIGH,
    Section.MED,
    Section.LOW,
    Section.RIG,
    Section.SUBSYSTEM,
    Section.DRONE_BAY,
    Section.FIGHTER_BAY,
    Section.CARGO,
    Section.FUEL_BAY,
    Section.BOOSTER,
)


class CompliancePolicy(models.Model):
    """Named set of per-slot-group enforcement overrides, applied to fittings in bulk."""

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    # Pre-built policies that ship with the plugin (seeded by migration 0022).
    # Editable only by superusers; managers may view + apply them. Built-ins can
    # never be deleted (only disabled).
    is_builtin = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Soft-disable: when set, the policy is no longer offered for applying to
    # fittings (a non-destructive alternative to deletion - the only way to
    # retire a built-in). Fits that already had it applied keep their per-module
    # settings; disabling does not retroactively rewrite them.
    disabled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this policy was disabled. Disabled policies can't be applied to fittings.",
    )

    class Meta:
        default_permissions = ()
        verbose_name_plural = "compliance policies"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def is_disabled(self) -> bool:
        return self.disabled_at is not None


class PolicySlotRule(models.Model):
    """One slot-group override inside a CompliancePolicy."""

    policy = models.ForeignKey(CompliancePolicy, on_delete=models.CASCADE, related_name="rules")
    section = models.CharField(max_length=8, choices=Section.choices)
    enforcement = models.CharField(
        max_length=2, choices=EnforcementMode.choices, default=EnforcementMode.META
    )
    min_meta_level = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Meta enforcement: substitutes need at least this meta level. "
        "Empty = each module's own level.",
    )
    allow_mutated = models.BooleanField(
        default=True,
        help_text="Equal-or-greater: allow abyssal/mutated modules whose rolls qualify.",
    )
    min_quantity_pct = models.PositiveSmallIntegerField(
        default=100,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="Consumable sections: pass at this percent of the listed quantity.",
    )

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["policy", "section"], name="fitcheck_unique_policy_rule")
        ]

    def __str__(self) -> str:
        return f"{self.policy}: {self.get_section_display()} = {self.get_enforcement_display()}"


class DoctrineFit(models.Model):
    """A fitting standard. Lives independently; doctrines reference it (M2M),
    and one fitting may belong to any number of doctrines (or none)."""

    doctrines = models.ManyToManyField(
        Doctrine,
        blank=True,
        related_name="fits",
        help_text="Doctrines this fitting belongs to. A fitting may stand alone.",
    )
    name = models.CharField(max_length=100)
    ship_type = models.ForeignKey("eveuniverse.EveType", on_delete=models.PROTECT, related_name="+")
    # Frigate(s) the doctrine accepts in the hull's Frigate Escape Bay, stored as
    # a list of bare EVE type_ids (like Doctrine.image_type_id, but many). The
    # pilot's bay passes if it holds ANY one of these. Empty list = no FEB
    # requirement. Eligible types are the frigate-tier ship groups plus a few
    # named exceptions (see forms.feb_eligible_frigate_choices). Checked against
    # the pilot's bay per the site FEB enforcement mode.
    feb_frigate_type_ids = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True)
    eft_source = models.TextField(help_text="Raw EFT text as imported, preserved verbatim.")
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    strict_extras = models.BooleanField(
        default=False,
        help_text="When set, modules outside the doctrine fail the check instead of warning.",
    )
    default_policy = models.CharField(
        max_length=2, choices=SubstitutionPolicy.choices, default=SubstitutionPolicy.VARIANTS
    )
    compliance_policy = models.ForeignKey(
        CompliancePolicy,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fits",
        help_text="The slot-group policy last applied to this fitting, if any.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Distinct from updated_at (which bumps on any save, including policy edits):
    # set only when the BOM/EFT itself changes - import, BOM update, resync.
    bom_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the fit's module list (BOM) last changed.",
    )
    last_imported_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    source_plugin_pk = models.PositiveIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Primary key of the source row in the `fittings` plugin, if imported.",
    )

    objects = DoctrineFitQuerySet.as_manager()

    class Meta:
        default_permissions = ()
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def bump_version(self) -> None:
        self.version = models.F("version") + 1
        self.save(update_fields=["version"])
        self.refresh_from_db(fields=["version"])


class ArchivedFitVersion(models.Model):
    """A point-in-time snapshot of a fitting captured just before its BOM was
    replaced by a new version. Retained for retrieval/audit (view-only - no
    restore action). The `policy_snapshot` keeps the per-item policy and
    overrides keyed by ``(section, type_id)`` so a manager can see exactly how
    the superseded version was configured."""

    fit = models.ForeignKey(DoctrineFit, on_delete=models.CASCADE, related_name="archives")
    version = models.PositiveIntegerField()
    eft_source = models.TextField()
    ship_type_id = models.PositiveIntegerField()
    # {"items": [{"section", "type_id", "qty", "policy", ...}], plus per-item overrides}
    policy_snapshot = models.JSONField(default=dict, blank=True)
    archived_at = models.DateTimeField(auto_now_add=True)
    archived_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        default_permissions = ()
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["fit", "version"], name="fitcheck_unique_fit_archive"
            )
        ]

    def __str__(self) -> str:
        return f"{self.fit} v{self.version} (archived)"


class DoctrineFitItem(models.Model):
    fit = models.ForeignKey(DoctrineFit, on_delete=models.CASCADE, related_name="items")
    section = models.CharField(max_length=8, choices=Section.choices)
    module_type = models.ForeignKey(
        "eveuniverse.EveType", on_delete=models.PROTECT, related_name="+"
    )
    quantity = models.PositiveIntegerField(default=1)
    charge_type = models.ForeignKey(
        "eveuniverse.EveType", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    policy = models.CharField(max_length=2, choices=SubstitutionPolicy.choices)
    min_meta_level = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Substitutes need at least this meta level. Empty = this module's own level.",
    )
    allowed_meta_groups = models.JSONField(
        default=default_allowed_meta_groups,
        blank=True,
        help_text="Meta group IDs allowed as substitutes (checked = allowed). Empty = no family substitutes.",
    )
    checked_attributes = models.JSONField(
        default=list,
        blank=True,
        help_text="Attribute IDs compared under meet-or-beat. Empty = sensible defaults.",
    )
    attribute_bounds = models.JSONField(
        default=dict,
        blank=True,
        help_text="Optional per-attribute abyssal acceptance window: "
        "{attr_id: {min, max}}. The worst-side bound is the pass threshold; "
        "absent = meet-or-beat the baseline.",
    )
    allow_mutated = models.BooleanField(
        default=True,
        help_text="Allow abyssal/mutated modules under meet-or-beat when their rolls qualify.",
    )
    min_quantity_pct = models.PositiveSmallIntegerField(
        default=100,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="Consumable leeway for bay/cargo items: pass at this percent of the listed quantity.",
    )
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["fit", "section", "module_type"], name="fitcheck_unique_fit_item"
            )
        ]
        indexes = [models.Index(fields=["fit", "section"])]

    def __str__(self) -> str:
        return f"{self.fit}: {self.quantity}x {self.module_type} [{self.section}]"

    @property
    def required_quantity(self) -> int:
        """Quantity needed to pass, after consumable leeway."""
        return _required_quantity(self.section, self.quantity, self.min_quantity_pct)


class FitAssignment(models.Model):
    """The link between a Doctrine and a DoctrineFit, owning per-(doctrine, fit)
    policy snapshots.

    Adding a fit to a doctrine clones the fit's source policies into a fresh
    `AssignmentItemPolicy` per item; from then on the assignment's policies
    evolve independently of the fit defaults. The same fit can sit in N
    doctrines with N independent policy sets - which is the whole point of
    this rework.

    The bare `DoctrineFit.doctrines` M2M still exists for backwards
    compatibility (read paths still iterate it), but new attachments go
    through this model so the policy snapshot exists from day one."""

    doctrine = models.ForeignKey(
        Doctrine, on_delete=models.CASCADE, related_name="assignments"
    )
    fit = models.ForeignKey(
        DoctrineFit, on_delete=models.CASCADE, related_name="assignments"
    )
    notes = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["doctrine", "fit"], name="fitcheck_unique_assignment"
            )
        ]

    def __str__(self) -> str:
        return f"{self.fit} in {self.doctrine}"


class AssignmentItemPolicy(models.Model):
    """Per-(doctrine, fit, item) policy snapshot.

    Mirrors `DoctrineFitItem`'s policy fields; sections, types and quantities are
    denormalised here too. The `source_item` FK is CASCADE, so a BOM re-import
    (`DoctrineFitItem` delete + `_materialise_items`) wipes these rows - the
    re-import paths therefore capture each snapshot first and replay it onto the
    new source items via `services.assignments.capture_assignment_policies` +
    `rebuild_assignment_snapshots` (keyed by section + module_type_id)."""

    assignment = models.ForeignKey(
        FitAssignment, on_delete=models.CASCADE, related_name="item_policies"
    )
    source_item = models.ForeignKey(
        DoctrineFitItem, on_delete=models.CASCADE, related_name="assignment_policies"
    )
    section = models.CharField(max_length=8, choices=Section.choices)
    module_type = models.ForeignKey(
        "eveuniverse.EveType", on_delete=models.PROTECT, related_name="+"
    )
    quantity = models.PositiveIntegerField(default=1)
    charge_type = models.ForeignKey(
        "eveuniverse.EveType",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    policy = models.CharField(max_length=2, choices=SubstitutionPolicy.choices)
    min_meta_level = models.PositiveSmallIntegerField(null=True, blank=True)
    allowed_meta_groups = models.JSONField(default=default_allowed_meta_groups, blank=True)
    checked_attributes = models.JSONField(default=list, blank=True)
    attribute_bounds = models.JSONField(default=dict, blank=True)
    allow_mutated = models.BooleanField(default=True)
    min_quantity_pct = models.PositiveSmallIntegerField(
        default=100,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
    )
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["assignment", "source_item"],
                name="fitcheck_unique_assignment_item",
            )
        ]
        indexes = [models.Index(fields=["assignment", "section"])]

    def __str__(self) -> str:
        return f"{self.assignment}: {self.module_type} [{self.section}]"

    @property
    def required_quantity(self) -> int:
        return _required_quantity(self.section, self.quantity, self.min_quantity_pct)


class FitItemOverride(models.Model):
    class Mode(models.TextChoices):
        INCLUDE = "I", _("Always allow")
        EXCLUDE = "E", _("Never allow")

    item = models.ForeignKey(DoctrineFitItem, on_delete=models.CASCADE, related_name="overrides")
    alt_type = models.ForeignKey("eveuniverse.EveType", on_delete=models.CASCADE, related_name="+")
    mode = models.CharField(max_length=1, choices=Mode.choices)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["item", "alt_type"], name="fitcheck_unique_override")
        ]

    def __str__(self) -> str:
        return f"{self.get_mode_display()}: {self.alt_type} for {self.item}"

    def clean(self):
        if self.mode == self.Mode.EXCLUDE and self.alt_type_id == self.item.module_type_id:
            raise ValidationError("The doctrine module itself cannot be excluded.")


class AssignmentItemOverride(models.Model):
    """Per-assignment override - same shape as FitItemOverride but attached to
    an AssignmentItemPolicy. Cloned from the source item's overrides when the
    assignment is created; edits stay independent of the fit's defaults."""

    class Mode(models.TextChoices):
        INCLUDE = "I", _("Always allow")
        EXCLUDE = "E", _("Never allow")

    assignment_item = models.ForeignKey(
        AssignmentItemPolicy,
        on_delete=models.CASCADE,
        related_name="overrides",
    )
    alt_type = models.ForeignKey(
        "eveuniverse.EveType", on_delete=models.CASCADE, related_name="+"
    )
    mode = models.CharField(max_length=1, choices=Mode.choices)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["assignment_item", "alt_type"],
                name="fitcheck_unique_assignment_override",
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_mode_display()}: {self.alt_type} for {self.assignment_item}"
