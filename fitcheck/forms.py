from django import forms
from django.db.models import Q
from django.forms import modelformset_factory
from django.utils.translation import gettext_lazy as _

from .constants import (
    EveCategoryId,
    FEB_CAPABLE_HULL_GROUP_IDS,
    FEB_ELIGIBLE_EXCEPTION_NAMES,
    FEB_ELIGIBLE_GROUP_IDS,
    FEB_ELIGIBLE_GROUP_LABELS,
    META_GROUP_DISPLAY_ORDER,
    META_GROUP_LABELS,
)
from django.contrib.auth.models import Group

from .models import (
    CompliancePolicy,
    Doctrine,
    DoctrineCategory,
    DoctrineFit,
    DoctrineFitItem,
    EnforcementSettings,
    SdeType,
)
from .models.doctrine import EnforcementMode, SubstitutionPolicy

# Full static fallback list (every labelled non-abyssal meta group), derived from
# the single source of truth in constants. The policy editor narrows this per item
# to the groups actually possible for that item via
# FitItemPolicyForm(possible_meta_groups_map=...).
META_GROUP_CHOICES = tuple(
    (gid, META_GROUP_LABELS[gid])
    for gid in META_GROUP_DISPLAY_ORDER
    if gid in META_GROUP_LABELS
)


class DoctrineForm(forms.ModelForm):
    image_type_id = forms.IntegerField(
        required=False,
        widget=forms.HiddenInput(),
        label=_("Doctrine Image"),
    )

    class Meta:
        model = Doctrine
        fields = ["name", "description", "image_type_id", "categories", "is_active"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "categories": forms.CheckboxSelectMultiple,
        }

    def clean_image_type_id(self):
        type_id = self.cleaned_data.get("image_type_id")
        if type_id and not SdeType.objects.filter(
            type_id=type_id, category_id=EveCategoryId.SHIP
        ).exists():
            raise forms.ValidationError(_("Pick a ship from the suggestions."))
        return type_id


class DoctrineCategoryForm(forms.ModelForm):
    """Quick inline create: just name + colour (used by the AJAX add endpoint)."""

    class Meta:
        model = DoctrineCategory
        fields = ["name", "color"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control form-control-sm", "placeholder": _("New category")}
            ),
            "color": forms.TextInput(
                attrs={
                    "class": "form-control form-control-sm coloris",
                    "data-coloris": "",
                    "value": "#0d6efd",
                }
            ),
        }


class DoctrineCategoryEditForm(forms.ModelForm):
    """Full category management: colour + the two group-visibility lists + the
    fits and doctrines this category gates. `doctrines` is the reverse side of
    Doctrine.categories, so it's a manual field saved in the view."""

    doctrines = forms.ModelMultipleChoiceField(
        queryset=Doctrine.objects.order_by("name"),
        required=False,
        widget=forms.SelectMultiple(attrs={"size": 8, "class": "form-select"}),
        help_text=_("Doctrines in this category."),
    )

    class Meta:
        model = DoctrineCategory
        fields = ["name", "color", "selected_groups", "required_groups", "fits"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "color": forms.TextInput(
                attrs={"class": "form-control coloris", "data-coloris": "", "value": "#0d6efd"}
            ),
            "selected_groups": forms.SelectMultiple(attrs={"size": 8, "class": "form-select"}),
            "required_groups": forms.SelectMultiple(attrs={"size": 8, "class": "form-select"}),
            "fits": forms.SelectMultiple(attrs={"size": 8, "class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["selected_groups"].queryset = Group.objects.order_by("name")
        self.fields["required_groups"].queryset = Group.objects.order_by("name")
        self.fields["fits"].queryset = DoctrineFit.objects.order_by("name")
        if self.instance.pk:
            self.fields["doctrines"].initial = self.instance.doctrines.all()


class AssignFittingForm(forms.Form):
    """Attach an existing fitting standard to a doctrine."""

    fit = forms.ModelChoiceField(
        queryset=DoctrineFit.objects.none(),
        label=_("Fitting Standard"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, doctrine: Doctrine | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = DoctrineFit.objects.select_related("ship_type").order_by("name")
        if doctrine is not None and doctrine.pk:
            queryset = queryset.exclude(doctrines=doctrine)
        self.fields["fit"].queryset = queryset


class FitImportForm(forms.Form):
    eft_text = forms.CharField(
        label=_("EFT Fitting"),
        widget=forms.Textarea(
            attrs={
                "rows": 16,
                "placeholder": "[Hull, Fit Name]\n...",
                "class": "form-control font-monospace",
            }
        ),
        help_text=_("Paste from the in-game fitting window (Copy) or Pyfa (EFT export)."),
    )
    name = forms.CharField(
        label=_("Fitting Name"),
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
        help_text=_("Leave empty to use the name from the EFT header."),
    )
    default_policy = forms.ChoiceField(
        label=_("Default Substitution Policy"),
        choices=SubstitutionPolicy.choices,
        initial=SubstitutionPolicy.VARIANTS,
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text=_("Applied to every module; fine-tune per module afterwards."),
    )
    strict_extras = forms.BooleanField(
        label=_("Strict Extras"),
        required=False,
        help_text=_("Fail fits that carry modules outside the doctrine."),
    )


class FitBomUpdateForm(forms.Form):
    """Edit a published fit's module list (BOM). Per-module policy is carried
    forward by (section, type) onto modules that survive the edit."""

    eft_text = forms.CharField(
        label=_("EFT fitting"),
        widget=forms.Textarea(
            attrs={
                "rows": 18,
                "class": "form-control font-monospace",
                "placeholder": "[Hull, Fit Name]\n...",
            }
        ),
        help_text=_(
            "Edit the module list and save. Existing per-module policy and "
            "exceptions are kept for modules that stay in the fit; new modules "
            "start from the fit's default policy."
        ),
    )


def feb_eligible_frigate_choices():
    """(type_id_str, name) options for the FEB frigate picker: hulls that fit in a
    Frigate Escape Bay - the frigate-tier ship groups plus a few allowed exception
    ships named explicitly. Sorted by name. Strings so they match a
    MultipleChoiceField's stringified values."""
    rows = (
        SdeType.objects.filter(category_id=EveCategoryId.SHIP, published=True)
        .filter(Q(group_id__in=FEB_ELIGIBLE_GROUP_IDS) | Q(name__in=FEB_ELIGIBLE_EXCEPTION_NAMES))
        .order_by("name")
        .values_list("type_id", "name")
    )
    return [(str(type_id), name) for type_id, name in rows]


def feb_eligible_group_choices():
    """(group_id_str, class_name) options for the FEB ship-class quick-add picker.
    Only the eligible classes that actually have at least one published ship in the
    local SDE mirror are offered, so the dropdown never lists an empty class. Sorted
    by class name. Strings to match a MultipleChoiceField's stringified values."""
    present = set(
        SdeType.objects.filter(
            category_id=EveCategoryId.SHIP,
            published=True,
            group_id__in=FEB_ELIGIBLE_GROUP_IDS,
        )
        .values_list("group_id", flat=True)
        .distinct()
    )
    rows = [(gid, FEB_ELIGIBLE_GROUP_LABELS.get(gid, str(gid))) for gid in present]
    rows.sort(key=lambda r: r[1])
    return [(str(gid), label) for gid, label in rows]


def feb_frigate_type_ids_for_groups(group_ids) -> set[int]:
    """Member frigate type_ids for the given eligible ship-class group ids, drawn
    from the same FEB-eligible ship set as feb_eligible_frigate_choices(). Used to
    expand a chosen class into its individual hulls before save."""
    wanted = {int(g) for g in group_ids} & set(FEB_ELIGIBLE_GROUP_IDS)
    if not wanted:
        return set()
    return set(
        SdeType.objects.filter(
            category_id=EveCategoryId.SHIP, published=True, group_id__in=wanted
        ).values_list("type_id", flat=True)
    )


def hull_allows_feb(ship_type_id: int | None) -> bool:
    """True if the hull's ship group carries a Frigate Escape Bay (battleship-class).
    The local SDE mirror has no ship dogma attributes, so we key on the ship group
    rather than the frigateEscapeBayCapacity attribute."""
    if not ship_type_id:
        return False
    group_id = (
        SdeType.objects.filter(type_id=ship_type_id)
        .values_list("group_id", flat=True)
        .first()
    )
    return group_id in FEB_CAPABLE_HULL_GROUP_IDS


class FitSettingsForm(forms.ModelForm):
    # Declared explicitly (not auto-built from the JSONField) so it renders as a
    # name-based multi-select restricted to FEB-eligible frigates; choices are
    # populated in __init__ and double as server-side eligibility validation.
    feb_frigate_type_ids = forms.MultipleChoiceField(
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "data-feb-picker": "1"}),
        label=_("Frigate Escape Bay - Allowed"),
        help_text=_(
            "Frigates this doctrine accepts in the hull's Frigate Escape Bay. The "
            "pilot's bay passes if it holds any one of these. Leave empty for no FEB "
            "requirement. Enforced per the site FEB mode."
        ),
    )
    # Quick-add by whole ship class. NOT a model field - on save() its selection is
    # expanded into the individual frigate type_ids and unioned into
    # feb_frigate_type_ids (see clean()), so storage stays a flat type-id list and
    # the engine is unchanged. It does not round-trip (re-opening shows the expanded
    # frigates selected, this box empty).
    feb_frigate_group_ids = forms.MultipleChoiceField(
        required=False,
        widget=forms.SelectMultiple(
            attrs={"class": "form-select", "data-feb-group-picker": "1"}
        ),
        label=_("Add a whole ship class"),
        help_text=_(
            "Add every frigate of a class to the Allowed list at once (e.g. all "
            "Assault Frigates). Expanded into individual frigates when you save."
        ),
    )

    class Meta:
        model = DoctrineFit
        # NOTE: `doctrines` is deliberately NOT editable here. Doctrine links own a
        # per-(doctrine, fit) policy snapshot and must be written only through
        # services/assignments.attach_fit_to_doctrine / detach_fit_from_doctrine
        # (standing decision 7). A ModelForm save() would write the bare M2M and
        # orphan/skip those snapshots. Edit doctrine links via fit_set_doctrines.
        fields = [
            "name", "description", "is_active", "strict_extras",
            "default_policy", "feb_frigate_type_ids",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
            "default_policy": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        fit = self.instance if (self.instance and self.instance.pk) else None
        # Hulls with no Frigate Escape Bay (supercaps, capitals, destroyers,
        # frigates, etc.) never need the picker - drop it entirely so it neither
        # renders nor accepts a value. The existing DB value (empty for such
        # hulls) is left untouched because the field is absent from save().
        if fit and not hull_allows_feb(fit.ship_type_id):
            self.fields.pop("feb_frigate_type_ids", None)
            self.fields.pop("feb_frigate_group_ids", None)
            return
        self.fields["feb_frigate_type_ids"].choices = feb_eligible_frigate_choices()
        self.fields["feb_frigate_group_ids"].choices = feb_eligible_group_choices()
        if fit:
            self.initial["feb_frigate_type_ids"] = [
                str(t) for t in (fit.feb_frigate_type_ids or [])
            ]

    def clean_feb_frigate_type_ids(self):
        # MultipleChoiceField already validated each value is an eligible frigate;
        # store as ints to match the JSON list the engine reads.
        return [int(x) for x in self.cleaned_data.get("feb_frigate_type_ids") or []]

    def clean(self):
        cleaned = super().clean()
        # Expand any chosen ship classes into their member frigate type_ids and union
        # them with the individually-picked frigates, so a flat de-duplicated type-id
        # list is persisted. Only runs when both fields survived __init__ (FEB hull).
        if "feb_frigate_type_ids" in self.fields and "feb_frigate_group_ids" in self.fields:
            ids = set(cleaned.get("feb_frigate_type_ids") or [])
            ids |= feb_frigate_type_ids_for_groups(cleaned.get("feb_frigate_group_ids") or [])
            cleaned["feb_frigate_type_ids"] = sorted(ids)
        return cleaned


class FitItemPolicyForm(forms.ModelForm):
    allowed_meta_groups = forms.TypedMultipleChoiceField(
        coerce=int,
        choices=META_GROUP_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label=_("Allowed Meta Groups"),
    )

    def __init__(self, *args, possible_meta_groups_map=None, **kwargs):
        """``possible_meta_groups_map`` (type_id -> set of meta group ids actually
        present in that item's variant family, from
        ``substitutions.possible_meta_groups_bulk``) narrows the allow-list to the
        groups that can really exist for this item - so a rig only offers Tech I/II,
        ammo never offers Officer, etc. Restricting the field's choices also makes
        the form reject an impossible group submitted via a crafted POST. When the
        map is omitted (e.g. admin/tests) the full static list is kept (no lockout)."""
        super().__init__(*args, **kwargs)
        if possible_meta_groups_map is not None:
            possible = set(
                possible_meta_groups_map.get(
                    getattr(self.instance, "module_type_id", None), set()
                )
            )
            ordered = [g for g in META_GROUP_DISPLAY_ORDER if g in possible]
            extra = sorted(possible.difference(META_GROUP_DISPLAY_ORDER))
            self.fields["allowed_meta_groups"].choices = [
                (g, META_GROUP_LABELS.get(g, _("Meta group %(id)d") % {"id": g}))
                for g in ordered + extra
            ]
    min_quantity_pct = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=100,
        label=_("Min Quantity %"),
        widget=forms.NumberInput(
            attrs={"class": "form-control form-control-sm", "style": "width: 5.5em"}
        ),
    )

    class Meta:
        model = DoctrineFitItem
        fields = [
            "policy",
            "allowed_meta_groups",
            "allow_mutated",
            "min_quantity_pct",
            "notes",
        ]
        widgets = {
            "policy": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "allow_mutated": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "notes": forms.TextInput(
                attrs={
                    "class": "form-control form-control-sm",
                    "placeholder": _("Note shown to members"),
                }
            ),
        }

    def clean_min_quantity_pct(self):
        value = self.cleaned_data.get("min_quantity_pct")
        return value if value is not None else (self.instance.min_quantity_pct or 100)


FitItemPolicyFormSet = modelformset_factory(DoctrineFitItem, form=FitItemPolicyForm, extra=0)


from .models import AssignmentItemPolicy as _AssignmentItemPolicyModel  # noqa: E402


class AssignmentItemPolicyForm(FitItemPolicyForm):
    """Same form shape as FitItemPolicyForm but bound to AssignmentItemPolicy.
    Used by the per-(doctrine, fit) policy editor."""

    class Meta(FitItemPolicyForm.Meta):
        model = _AssignmentItemPolicyModel


AssignmentItemPolicyFormSet = modelformset_factory(
    _AssignmentItemPolicyModel, form=AssignmentItemPolicyForm, extra=0
)


class OverrideAddForm(forms.Form):
    type_name = forms.CharField(label=_("Module Name"))
    mode = forms.ChoiceField(
        choices=(("I", _("Always allow")), ("E", _("Never allow"))), initial="I"
    )


class CompliancePolicyForm(forms.ModelForm):
    class Meta:
        model = CompliancePolicy
        fields = ["name", "description"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
        }


class PolicySlotRuleForm(forms.Form):
    """One slot group inside the policy editor. Empty enforcement = the policy
    leaves that slot group untouched."""

    enforcement = forms.ChoiceField(
        required=False,
        choices=[("", _("Not overridden"))] + list(EnforcementMode.choices),
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    min_meta_level = forms.IntegerField(
        required=False,
        min_value=0,
        max_value=14,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control form-control-sm",
                "placeholder": _("module's own"),
                "style": "width: 7em",
            }
        ),
    )
    allow_mutated = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    min_quantity_pct = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=100,
        initial=100,
        widget=forms.NumberInput(
            attrs={"class": "form-control form-control-sm", "style": "width: 5.5em"}
        ),
    )


class ApplyPolicyForm(forms.Form):
    # Disabled policies are retired and must not be offered for applying.
    policy = forms.ModelChoiceField(
        queryset=CompliancePolicy.objects.filter(disabled_at__isnull=True),
        label=_("Compliance Policy"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class ReviewDecisionForm(forms.Form):
    DECISIONS = (("approve", _("Approve")), ("reject", _("Reject")))
    decision = forms.ChoiceField(choices=DECISIONS, widget=forms.HiddenInput)
    comment = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}), label=_("Comment")
    )


class EftSubmitForm(forms.Form):
    eft_text = forms.CharField(
        label=_("Fit to Test (EFT Format)"),
        widget=forms.Textarea(
            attrs={
                "rows": 16,
                "placeholder": "[Hull, Fit Name]\n...",
                "class": "form-control font-monospace",
            }
        ),
        help_text=_(
            "In game: Fitting window > hamburger menu > Copy. "
            "Mutated modules: export from Pyfa with mutations included."
        ),
    )


class EnforcementSettingsForm(forms.ModelForm):
    """Site-wide 4-mode enforcement selectors for the verification concerns."""

    class Meta:
        model = EnforcementSettings
        fields = ["implant_mode", "feb_mode", "fuel_mode", "booster_mode"]
        widgets = {
            name: forms.Select(attrs={"class": "form-select"})
            for name in ("implant_mode", "feb_mode", "fuel_mode", "booster_mode")
        }
