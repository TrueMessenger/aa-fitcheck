from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Count, F, Q
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_GET, require_POST

from ..constants import (
    FEB_ELIGIBLE_GROUP_IDS,
    LEEWAY_SECTIONS,
    SECTION_ORDER,
    SECTION_TO_SLOT_KINDS,
    EveCategoryId,
    Section,
)
from ..forms import (
    ApplyPolicyForm,
    AssignFittingForm,
    CompliancePolicyForm,
    DoctrineCategoryEditForm,
    DoctrineCategoryForm,
    DoctrineForm,
    EnforcementSettingsForm,
    FitBomUpdateForm,
    FitImportForm,
    FitItemPolicyFormSet,
    FitSettingsForm,
    OverrideAddForm,
    PolicySlotRuleForm,
    ScanParametersForm,
    hull_allows_feb,
)
from ..models import (
    CompliancePolicy,
    Doctrine,
    DoctrineCategory,
    DoctrineFit,
    DoctrineFitItem,
    EnforcementSettings,
    FitItemOverride,
    FitSubmission,
    PolicySlotRule,
    ScanParameters,
    SdeType,
)
from ..models.doctrine import POLICY_SECTIONS, SubstitutionPolicy
from ..services.doctrine_import import DoctrineImportError, _get_or_create_eve_type, import_fit
from ..services.fit_lint import slot_layout_warnings
from ..services.substitutions import (
    abyssal_name_for_item,
    possible_meta_groups_bulk,
    rollable_attributes_for_item,
)
from .common import paginate


# Version-bump routing: each edit path bumps exactly the ladder it changed, so
# only the submissions actually graded from the edited config go stale (see
# FitSubmission.is_stale):
#   fit.bump_version()               - BOM / fit-wide settings; stales everything
#   fit.bump_source_policy_version() - the fit's own policy defaults; stales
#                                      source-defaults submissions only
#   assignment.bump_version()        - one (doctrine, fit) snapshot; stales that
#                                      doctrine's submissions only
# Re-check is intentionally NOT triggered by any bump - managers fire it
# manually from the Recheck Stale page to keep the Celery queue from
# saturating when policy is iterated rapidly.


# ---------------------------------------------------------------- fittings ---


# Sort keys accepted by `sort=` on the Fittings & Standards list, mapped to the
# ORM field/annotation each toggles. `name` is always appended as a secondary
# key so ties (e.g. equal doctrine counts) render in a stable order.
STANDARDS_SORT_FIELDS = {
    "name": "name",
    "hull": "ship_type__name",
    "doctrines": "doctrine_count",
    "version": "version",
    "created": "created_at",
    "updated": "last_updated",
}


@login_required
@permission_required("fitcheck.manage_doctrines")
def standards_list(request):
    """Fittings & Standards home: every fitting, doctrine-bound or standalone.

    Filters (`q`, `doctrine`, `group`, `category`), sorting (`sort`) and
    pagination all live in GET params so a filtered view is a shareable URL.
    """
    q = request.GET.get("q", "").strip()
    doctrine_param = request.GET.get("doctrine", "").strip()
    group_param = request.GET.get("group", "").strip()
    category_param = request.GET.get("category", "").strip()
    sort_param = request.GET.get("sort", "").strip()

    fits = (
        DoctrineFit.objects.select_related("ship_type", "compliance_policy")
        .prefetch_related("doctrines")
        .annotate(
            doctrine_count=Count("doctrines", distinct=True),
            last_updated=Coalesce("bom_updated_at", "updated_at"),
        )
    )

    if q:
        fits = fits.filter(name__icontains=q)
    if doctrine_param == "none":
        fits = fits.filter(doctrines__isnull=True)
    elif doctrine_param.isdigit():
        fits = fits.filter(doctrines__pk=int(doctrine_param)).distinct()
    if group_param.isdigit():
        fits = fits.filter(ship_type__eve_group_id=int(group_param))
    if category_param.isdigit():
        fits = fits.filter(categories__pk=int(category_param)).distinct()

    sort_key = sort_param.lstrip("-")
    order_field = STANDARDS_SORT_FIELDS.get(sort_key)
    if order_field:
        prefix = "-" if sort_param.startswith("-") else ""
        fits = fits.order_by(f"{prefix}{order_field}", "name")
    else:
        sort_param = ""
        fits = fits.order_by("name")

    page_obj, elided_range, querystring = paginate(request, fits)

    doctrines = list(
        Doctrine.objects.annotate(fit_count=Count("fits", distinct=True)).order_by(
            "-fit_count", "name"
        )
    )

    # DoctrineFit.Meta.ordering = ["name"] leaks into SELECT DISTINCT (Django
    # adds the default order_by fields to the SELECT list), so without
    # clearing it here each fit's name makes its own row "distinct" and hull
    # groups repeat once per fit sharing that group.
    ship_groups = sorted(
        (
            {"id": group_id, "name": group_name}
            for group_id, group_name in DoctrineFit.objects.order_by()
            .values_list("ship_type__eve_group_id", "ship_type__eve_group__name")
            .distinct()
            if group_id is not None
        ),
        key=lambda g: g["name"] or "",
    )
    categories = DoctrineCategory.objects.order_by("name")

    no_doctrine_params = request.GET.copy()
    no_doctrine_params.pop("doctrine", None)
    no_doctrine_params.pop("page", None)
    no_sort_params = request.GET.copy()
    no_sort_params.pop("sort", None)
    no_sort_params.pop("page", None)

    return render(
        request,
        "fitcheck/standards/list.html",
        {
            "page_obj": page_obj,
            "elided_range": elided_range,
            "querystring": querystring,
            "doctrines": doctrines,
            "pill_doctrines": doctrines[:8],
            "overflow_doctrines": doctrines[8:],
            "overflow_active": any(
                str(d.pk) == doctrine_param for d in doctrines[8:]
            ),
            "ship_groups": ship_groups,
            "categories": categories,
            "active_q": q,
            "active_doctrine": doctrine_param,
            "active_group": group_param,
            "active_category": category_param,
            "active_sort": sort_param,
            "has_filters": bool(q or doctrine_param or group_param or category_param),
            "qs_no_doctrine": no_doctrine_params.urlencode(),
            "qs_no_sort": no_sort_params.urlencode(),
            "page_title": _("Fittings & Standards"),
        },
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
def standard_import(request, doctrine_pk: int | None = None):
    """Import an EFT paste as a fitting standard - optionally straight into a doctrine."""
    doctrine = get_object_or_404(Doctrine, pk=doctrine_pk) if doctrine_pk else None
    if request.method == "POST":
        form = FitImportForm(request.POST)
        if form.is_valid():
            try:
                fit = import_fit(
                    form.cleaned_data["eft_text"],
                    request.user,
                    doctrine=doctrine,
                    name=form.cleaned_data["name"] or None,
                )
            except DoctrineImportError as exc:
                for error in exc.errors or [str(exc)]:
                    form.add_error("eft_text", error)
            else:
                fit.default_policy = form.cleaned_data["default_policy"]
                fit.strict_extras = form.cleaned_data["strict_extras"]
                fit.save(update_fields=["default_policy", "strict_extras"])
                fit.items.update(policy=fit.default_policy)
                messages.success(
                    request, _("Fitting '%(name)s' imported.") % {"name": fit.name}
                )
                for warning in slot_layout_warnings(fit):
                    messages.warning(request, warning)
                if doctrine:
                    return redirect("fitcheck:doctrine_detail", doctrine_pk=doctrine.pk)
                return redirect("fitcheck:fit_detail", fit_pk=fit.pk)
    else:
        form = FitImportForm()
    return render(
        request,
        "fitcheck/standards/import.html",
        {"form": form, "doctrine": doctrine, "page_title": _("Import Fitting")},
    )


def _feb_group_members(fit) -> dict:
    """group_id (str) -> [{id, name}] of every eligible frigate in each FEB ship
    class, for the settings page's class-quick-add JS. Empty for non-FEB hulls."""
    if not hull_allows_feb(fit.ship_type_id):
        return {}
    members: dict[str, list] = {}
    rows = (
        SdeType.objects.filter(
            category_id=EveCategoryId.SHIP,
            published=True,
            group_id__in=FEB_ELIGIBLE_GROUP_IDS,
        )
        .order_by("name")
        .values_list("group_id", "type_id", "name")
    )
    for group_id, type_id, name in rows:
        members.setdefault(str(group_id), []).append({"id": type_id, "name": name})
    return members


@login_required
@permission_required("fitcheck.manage_doctrines")
def fit_settings(request, fit_pk: int):
    fit = get_object_or_404(
        DoctrineFit.objects.select_related("ship_type", "compliance_policy"), pk=fit_pk
    )
    if request.method == "POST":
        form = FitSettingsForm(request.POST, instance=fit)
        if form.is_valid():
            form.save()
            fit.bump_version()  # fit-wide settings affect every grading path
            messages.success(
                request,
                _("Fitting saved. Pending submissions are now stale - use Recheck Stale to re-grade them."),
            )
            return redirect("fitcheck:fit_detail", fit_pk=fit.pk)
    else:
        form = FitSettingsForm(instance=fit)
    return render(
        request,
        "fitcheck/standards/fit_settings.html",
        {
            "form": form,
            "fit": fit,
            "apply_policy_form": ApplyPolicyForm(),
            "has_policies": CompliancePolicy.objects.exists(),
            "stale_pending_count": _stale_pending_count(fit),
            "feb_group_members": _feb_group_members(fit),
            "page_title": _("Fitting Settings"),
        },
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
def fit_update_bom(request, fit_pk: int):
    """Replace a fit's module list (BOM). Archives the old version and carries
    per-module policy forward onto modules that survive the edit."""
    fit = get_object_or_404(DoctrineFit.objects.select_related("ship_type"), pk=fit_pk)
    if request.method == "POST":
        form = FitBomUpdateForm(request.POST)
        if form.is_valid():
            from ..services.fit_edit import update_fit_bom

            try:
                result = update_fit_bom(fit, form.cleaned_data["eft_text"], request.user)
            except DoctrineImportError as exc:
                for error in exc.errors or [str(exc)]:
                    form.add_error("eft_text", error)
            else:
                messages.success(
                    request,
                    _(
                        "Fit updated to v%(version)s. Policy carried forward for "
                        "%(carried)s module(s); %(added)s new module(s) use the default "
                        "policy; %(dropped)s removed. The previous version was archived. "
                        "Pending submissions are now stale - use Recheck Stale to re-grade them."
                    )
                    % {
                        "version": fit.version,
                        "carried": len(result.carried),
                        "added": len(result.added),
                        "dropped": len(result.dropped),
                    },
                )
                if result.added:
                    messages.info(
                        request,
                        _("New modules needing policy review: %(names)s")
                        % {"names": ", ".join(result.added)},
                    )
                for warning in slot_layout_warnings(fit):
                    messages.warning(request, warning)
                return redirect("fitcheck:fit_detail", fit_pk=fit.pk)
    else:
        form = FitBomUpdateForm(initial={"eft_text": fit.eft_source})
    return render(
        request,
        "fitcheck/standards/fit_update.html",
        {
            "form": form,
            "fit": fit,
            "page_title": _("Update Fit"),
        },
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
def fit_archives(request, fit_pk: int):
    """View-only list of a fit's archived (superseded) versions."""
    fit = get_object_or_404(DoctrineFit.objects.select_related("ship_type"), pk=fit_pk)
    archives = fit.archives.select_related("archived_by").all()
    return render(
        request,
        "fitcheck/standards/fit_archives.html",
        {
            "fit": fit,
            "archives": archives,
            "page_title": _("Version History"),
        },
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def fit_delete(request, fit_pk: int):
    fit = get_object_or_404(DoctrineFit, pk=fit_pk)
    name = fit.name
    fit.delete()
    messages.success(request, _("Fitting '%(name)s' deleted.") % {"name": name})
    return redirect("fitcheck:standards_list")


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def fit_apply_policy(request, fit_pk: int):
    fit = get_object_or_404(DoctrineFit, pk=fit_pk)
    form = ApplyPolicyForm(request.POST)
    if form.is_valid():
        from ..services.policies import apply_policy_to_fit

        updated = apply_policy_to_fit(fit, form.cleaned_data["policy"])
        fit.bump_source_policy_version()
        messages.success(
            request,
            _(
                "Policy '%(policy)s' applied to %(count)s modules. Submissions graded "
                "against the fit's defaults are now stale - use Recheck Stale to re-grade them."
            )
            % {"policy": form.cleaned_data["policy"], "count": updated},
        )
    else:
        messages.error(request, _("Pick a policy first."))
    return redirect("fitcheck:manage_fit_items", fit_pk=fit.pk)


# --------------------------------------------------------- manual rechecks ---

_RECHECK_COOLDOWN_SECONDS = 60


def _queue_recheck(fit_pk: int) -> bool:
    """Queue a recheck for one fit, honoring a per-fit cooldown.
    Returns True if queued, False if rate-limited."""
    from django.core.cache import cache

    from ..tasks import recheck_pending_submissions

    key = f"fitcheck:fit_recheck:{fit_pk}"
    if not cache.add(key, True, timeout=_RECHECK_COOLDOWN_SECONDS):
        return False
    recheck_pending_submissions.delay(fit_pk)
    return True


def _stale_pending_count(fit: DoctrineFit) -> int:
    # Scope-aware staleness (three ladders) can't be a single SQL comparison,
    # so count over the annotated pending rows; the per-fit volume is small.
    return sum(
        1
        for s in fit.submissions.pending().with_staleness().select_related("doctrine_fit")
        if s.is_stale
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def fit_recheck_stale(request, fit_pk: int):
    """Manager-triggered recheck of one fit's stale pending submissions."""
    fit = get_object_or_404(DoctrineFit, pk=fit_pk)
    count = _stale_pending_count(fit)
    if not count:
        messages.info(request, _("No stale pending submissions to re-check."))
    elif _queue_recheck(fit.pk):
        messages.success(
            request,
            _("Queued re-check of %(count)s stale pending submission(s).") % {"count": count},
        )
    else:
        messages.warning(
            request,
            _("A re-check for this fitting was just queued - please wait %(s)ss before trying again.")
            % {"s": _RECHECK_COOLDOWN_SECONDS},
        )
    return redirect(request.META.get("HTTP_REFERER") or "fitcheck:fit_detail", fit_pk=fit.pk)


def _stale_fits_queryset():
    """Fittings with at least one stale pending submission, each carrying a
    `stale_pending` count. Staleness is scope-aware (three ladders), so the
    counts come from the annotated pending rows in Python rather than one SQL
    aggregate; the pending set is small on a manager page."""
    counts: dict[int, int] = {}
    pending = (
        FitSubmission.objects.pending().with_staleness().select_related("doctrine_fit")
    )
    for submission in pending:
        if submission.is_stale:
            counts[submission.doctrine_fit_id] = counts.get(submission.doctrine_fit_id, 0) + 1
    fits = list(
        DoctrineFit.objects.filter(pk__in=counts)
        .select_related("ship_type")
        .prefetch_related("doctrines")
        .order_by("name")
    )
    for fit in fits:
        fit.stale_pending = counts[fit.pk]
    return fits


@login_required
@permission_required("fitcheck.manage_doctrines")
def stale_recheck_page(request):
    """List every fitting whose current version has stale pending submissions,
    with three controls: per-row checkbox + Recheck Selected, Recheck All."""
    fits = list(_stale_fits_queryset())
    if request.method == "POST":
        action = request.POST.get("action", "selected")
        if action == "all":
            target_pks = [fit.pk for fit in fits]
        else:
            target_pks = [
                int(pk) for pk in request.POST.getlist("fits") if pk.isdigit()
            ]
            target_pks = [pk for pk in target_pks if pk in {f.pk for f in fits}]
        queued = 0
        cooldown_blocked = 0
        for pk in target_pks:
            if _queue_recheck(pk):
                queued += 1
            else:
                cooldown_blocked += 1
        if queued:
            messages.success(
                request,
                _("Queued re-check for %(n)s fitting(s).") % {"n": queued},
            )
        if cooldown_blocked:
            messages.warning(
                request,
                _("%(n)s fitting(s) were skipped (cooldown active - try again in a minute).")
                % {"n": cooldown_blocked},
            )
        if not target_pks:
            messages.info(request, _("No fittings selected."))
        return redirect("fitcheck:stale_recheck_page")
    return render(
        request,
        "fitcheck/standards/recheck.html",
        {
            "fits": fits,
            "page_title": _("Recheck Stale Submissions"),
        },
    )


def _policy_row_urls(item, *, assignment: bool) -> dict:
    """Endpoint URLs for one policy row, so the shared template doesn't hardcode
    the source-fit names. `item` is a DoctrineFitItem or an AssignmentItemPolicy."""
    prefix = "assignment_" if assignment else ""
    return {
        "override_add": reverse(f"fitcheck:{prefix}override_add", args=[item.pk]),
        "override_add_bulk": reverse(f"fitcheck:{prefix}override_add_bulk", args=[item.pk]),
        "attr_candidates": reverse(f"fitcheck:{prefix}attribute_candidates", args=[item.pk]),
        "attr_save": reverse(f"fitcheck:{prefix}attribute_policy_save", args=[item.pk]),
    }


def _row_overrides(item, *, assignment: bool) -> list:
    """Override chips for one row, each with its delete URL (source vs per-doctrine)."""
    name = "fitcheck:assignment_override_remove" if assignment else "fitcheck:override_remove"
    return [
        {"obj": o, "remove_url": reverse(name, args=[o.pk])} for o in item.overrides.all()
    ]


def _build_policy_row(
    form,
    item,
    last_section,
    *,
    assignment: bool,
    meta_groups: dict,
    possible_map: dict | None = None,
    differs: bool = False,
):
    """Shared row dict for the policy editor (source-fit and per-assignment).
    `differs` marks an assignment row whose policy has drifted from its source
    template (assignment mode only)."""
    rollable = rollable_attributes_for_item(item)
    # No variant substitutes -> no meta-group checkboxes (the possible set is empty
    # once the item itself is excluded). Surfaced as a hint when there is also no
    # abyssal variant to allow.
    meta_groups_trivial = possible_map is not None and not possible_map.get(
        item.module_type_id
    )
    return {
        "form": form,
        "item": item,
        "section_header": (
            item.get_section_display() if item.section != last_section else None
        ),
        "is_quantity_section": item.section in LEEWAY_SECTIONS,
        "own_meta_group": meta_groups.get(item.module_type_id),
        "meta_groups_trivial": meta_groups_trivial,
        "has_rollable": bool(rollable),
        # Selected attributes for the at-a-glance summary on the row (req: show
        # saved abyssal bounds without reopening the modal).
        "selected_attrs": [a for a in rollable if a["selected"]],
        "urls": _policy_row_urls(item, assignment=assignment),
        "overrides": _row_overrides(item, assignment=assignment),
        "differs": differs,
    }


@login_required
@permission_required("fitcheck.manage_doctrines")
def fit_items(request, fit_pk: int):
    """Per-module policy editor: substitution policy, meta filters, mutated toggle,
    quantity variance and overrides for every module in the fit."""
    fit = get_object_or_404(DoctrineFit.objects.select_related("ship_type"), pk=fit_pk)
    queryset = (
        DoctrineFitItem.objects.filter(fit=fit)
        .select_related("module_type", "charge_type")
        .prefetch_related("overrides__alt_type")
    )
    # Per-item: only the meta groups that actually exist in each module's variant
    # family are offered (and validated) as substitution exceptions.
    possible_map = possible_meta_groups_bulk(
        set(queryset.values_list("module_type_id", flat=True))
    )
    form_kwargs = {"possible_meta_groups_map": possible_map}
    if request.method == "POST":
        formset = FitItemPolicyFormSet(
            request.POST, queryset=queryset, form_kwargs=form_kwargs
        )
        if formset.is_valid():
            changed = any(form.has_changed() for form in formset.forms)
            formset.save()
            if changed:
                fit.bump_source_policy_version()
                messages.success(
                    request,
                    _(
                        "Policies saved. Submissions graded against the fit's defaults are now stale - use Recheck Stale to re-grade them."
                    ),
                )
            else:
                messages.info(request, _("No changes."))
            return redirect("fitcheck:manage_fit_items", fit_pk=fit.pk)
        messages.error(request, _("Please fix the errors below."))
    else:
        formset = FitItemPolicyFormSet(queryset=queryset, form_kwargs=form_kwargs)

    forms_sorted = sorted(
        formset.forms, key=lambda f: SECTION_ORDER.get(f.instance.section, 99)
    )
    meta_groups = dict(
        SdeType.objects.filter(
            type_id__in={f.instance.module_type_id for f in forms_sorted}
        ).values_list("type_id", "meta_group_id")
    )
    rows = []
    last_section = None
    for form in forms_sorted:
        item = form.instance
        rows.append(
            _build_policy_row(
                form,
                item,
                last_section,
                assignment=False,
                meta_groups=meta_groups,
                possible_map=possible_map,
            )
        )
        last_section = item.section
    # "Used in N doctrines" panel: each combination this fit is graded under
    # gets its own policy copy (the audit reads the copy, not this template),
    # flagged when it has drifted from the template.
    from ..services.assignments import differing_assignments

    drifted = differing_assignments(fit)
    used_in = [
        {
            "doctrine": a.doctrine,
            "assignment_pk": a.pk,
            "differs": a.pk in drifted,
        }
        for a in fit.assignments.select_related("doctrine").order_by("doctrine__name")
    ]
    return render(
        request,
        "fitcheck/standards/fit_items.html",
        {
            "fit": fit,
            "formset": formset,
            "rows": rows,
            "used_in": used_in,
            "override_form": OverrideAddForm(),
            "apply_policy_form": ApplyPolicyForm(),
            "has_policies": CompliancePolicy.objects.exists(),
            "stale_pending_count": _stale_pending_count(fit),
            "page_title": _("Module Policies"),
            "assignment": None,  # source-defaults editor
        },
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
def assignment_items(request, assignment_pk: int):
    """Per-(doctrine, fit) policy editor. Mirrors fit_items but operates on
    AssignmentItemPolicy rows for one specific FitAssignment - edits stay
    local to this combination and don't move the fit's source defaults."""
    from ..forms import AssignmentItemPolicyFormSet
    from ..models import AssignmentItemPolicy, FitAssignment
    from ..services.assignments import assignment_item_differs

    assignment = get_object_or_404(
        FitAssignment.objects.select_related("doctrine", "fit", "fit__ship_type"),
        pk=assignment_pk,
    )
    queryset = (
        AssignmentItemPolicy.objects.filter(assignment=assignment)
        .select_related("module_type", "charge_type", "source_item")
        .prefetch_related("overrides__alt_type", "source_item__overrides")
    )
    possible_map = possible_meta_groups_bulk(
        set(queryset.values_list("module_type_id", flat=True))
    )
    form_kwargs = {"possible_meta_groups_map": possible_map}
    if request.method == "POST":
        formset = AssignmentItemPolicyFormSet(
            request.POST, queryset=queryset, form_kwargs=form_kwargs
        )
        if formset.is_valid():
            changed = any(form.has_changed() for form in formset.forms)
            formset.save()
            if changed:
                assignment.bump_version()
                messages.success(
                    request,
                    _(
                        "Policies for %(doctrine)s saved. That doctrine's pending submissions are now stale - use Recheck Stale to re-grade them."
                    )
                    % {"doctrine": assignment.doctrine.name},
                )
            else:
                messages.info(request, _("No changes."))
            return redirect(
                "fitcheck:manage_assignment_items", assignment_pk=assignment.pk
            )
        messages.error(request, _("Please fix the errors below."))
    else:
        formset = AssignmentItemPolicyFormSet(queryset=queryset, form_kwargs=form_kwargs)

    forms_sorted = sorted(
        formset.forms, key=lambda f: SECTION_ORDER.get(f.instance.section, 99)
    )
    meta_groups = dict(
        SdeType.objects.filter(
            type_id__in={f.instance.module_type_id for f in forms_sorted}
        ).values_list("type_id", "meta_group_id")
    )
    rows = []
    last_section = None
    for form in forms_sorted:
        item = form.instance
        rows.append(
            _build_policy_row(
                form,
                item,
                last_section,
                assignment=True,
                meta_groups=meta_groups,
                possible_map=possible_map,
                differs=assignment_item_differs(item),
            )
        )
        last_section = item.section
    # Snapshot drifts from the source template when any row differs OR the BOM
    # gained/lost a module since this snapshot was cloned (compare key sets).
    source_keys = {(i.section, i.module_type_id) for i in assignment.fit.items.all()}
    snapshot_keys = {(f.instance.section, f.instance.module_type_id) for f in formset.forms}
    snapshot_differs = snapshot_keys != source_keys or any(r["differs"] for r in rows)
    return render(
        request,
        "fitcheck/standards/fit_items.html",
        {
            "fit": assignment.fit,
            "assignment": assignment,
            "formset": formset,
            "rows": rows,
            "snapshot_differs": snapshot_differs,
            "override_form": OverrideAddForm(),
            "apply_policy_form": ApplyPolicyForm(),
            "has_policies": CompliancePolicy.objects.exists(),
            "stale_pending_count": _stale_pending_count(assignment.fit),
            "page_title": _("Policies: %(fit)s in %(doctrine)s")
            % {"fit": assignment.fit.name, "doctrine": assignment.doctrine.name},
        },
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def assignment_resync(request, assignment_pk: int):
    """Re-clone one assignment's policy snapshot from the fit's current source
    template, discarding this combination's customizations. Bumps the fit
    version so dependent pending submissions show stale and can be rechecked."""
    from ..models import FitAssignment
    from ..services.assignments import resync_assignment_from_source

    assignment = get_object_or_404(
        FitAssignment.objects.select_related("doctrine", "fit"), pk=assignment_pk
    )
    resync_assignment_from_source(assignment)
    assignment.bump_version()
    messages.success(
        request,
        _(
            "Re-synced %(fit)s in %(doctrine)s from the fit template. That doctrine's pending submissions are now stale - use Recheck Stale to re-grade them."
        )
        % {"fit": assignment.fit.name, "doctrine": assignment.doctrine.name},
    )
    return redirect("fitcheck:manage_assignment_items", assignment_pk=assignment.pk)


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_GET
def module_search(request):
    """JSON autocomplete: SdeType rows whose slot_kind matches the given
    Section, filtered by name. Powers the override picker on fit_items.html.

    When the in-slot query returns nothing we also surface an `off_slot`
    summary so the picker can tell the admin "exists in HIGH/MED, not this
    slot" instead of just a flat 'no matches' that reads as a search bug."""
    section = request.GET.get("section", "")
    query = request.GET.get("q", "").strip()
    slot_kinds = SECTION_TO_SLOT_KINDS.get(section, ())
    results = []
    off_slot: list[dict] = []
    if slot_kinds and len(query) >= 2:
        results = list(
            SdeType.objects.filter(
                slot_kind__in=slot_kinds, published=True, name__icontains=query
            )
            .order_by("name")
            .values("type_id", "name", "meta_group_id")[:20]
        )
        if not results:
            # Probe wider so the UI can say "this name exists, just not for
            # this slot." Aggregate by slot_kind so the hint stays short.
            from collections import Counter
            hits = (
                SdeType.objects.filter(published=True, name__icontains=query)
                .exclude(slot_kind__in=slot_kinds)
                .values_list("slot_kind", flat=True)[:50]
            )
            off_slot = [
                {"slot_kind": kind, "count": count}
                for kind, count in Counter(hits).most_common()
            ]
    return JsonResponse({"results": results, "off_slot": off_slot})


# --- shared bodies for the source-fit + per-assignment override/attr endpoints ---
# Each twin pair below differs only in the override model, its FK field back to
# the policy item, which version ladder to bump (`bump` is a zero-arg callable:
# the fit's source-policy ladder or the assignment's own ladder), and the
# redirect target; the body is shared.


def _apply_bulk_overrides(request, item, bump, back, override_model, fk_field):
    """Create many overrides at once (one mode per call) and bump the edited
    policy ladder once. `override_model` is FitItemOverride or
    AssignmentItemOverride; `fk_field` is its FK back to the policy item
    (``item`` / ``assignment_item``)."""
    mode = request.POST.get("mode", override_model.Mode.INCLUDE)
    if mode not in (override_model.Mode.INCLUDE, override_model.Mode.EXCLUDE):
        messages.error(request, _("Invalid override mode."))
        return back
    type_ids: list[int] = []
    for raw in request.POST.getlist("type_ids"):
        try:
            type_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not type_ids:
        messages.error(request, _("Pick at least one module from the search results."))
        return back
    slot_kinds = SECTION_TO_SLOT_KINDS.get(item.section, ())
    sde_rows = {
        row["type_id"]: row["name"]
        for row in SdeType.objects.filter(
            type_id__in=type_ids, slot_kind__in=slot_kinds, published=True
        ).values("type_id", "name")
    }
    added, skipped = 0, []
    for type_id in type_ids:
        if type_id not in sde_rows:
            skipped.append(str(type_id))
            continue
        if mode == override_model.Mode.EXCLUDE and type_id == item.module_type_id:
            skipped.append(sde_rows[type_id])
            continue
        override_model.objects.update_or_create(
            **{fk_field: item},
            alt_type=_get_or_create_eve_type(type_id),
            defaults={"mode": mode},
        )
        added += 1
    if added:
        bump()
        messages.success(request, _("Saved %(n)s override(s).") % {"n": added})
    if skipped:
        messages.warning(
            request,
            _("Skipped %(n)s entr(y/ies) that didn't fit this slot or are the doctrine module itself: %(names)s.")
            % {"n": len(skipped), "names": ", ".join(skipped)},
        )
    return back


def _apply_single_override(request, item, bump, back, override_model, fk_field):
    """Add one override from a typed module name (the OverrideAddForm flow)."""
    form = OverrideAddForm(request.POST)
    if form.is_valid():
        name = form.cleaned_data["type_name"].strip()
        sde_type = (
            SdeType.objects.filter(name__iexact=name, published=True)
            .order_by("type_id")
            .first()
        )
        if sde_type is None:
            messages.error(request, _("'%(name)s' is not a known type.") % {"name": name})
        elif (
            form.cleaned_data["mode"] == override_model.Mode.EXCLUDE
            and sde_type.type_id == item.module_type_id
        ):
            messages.error(request, _("The doctrine module itself cannot be excluded."))
        else:
            override_model.objects.update_or_create(
                **{fk_field: item},
                alt_type=_get_or_create_eve_type(sde_type.type_id),
                defaults={"mode": form.cleaned_data["mode"]},
            )
            bump()
            messages.success(request, _("Override saved: %(name)s.") % {"name": sde_type.name})
    else:
        messages.error(request, _("Enter a module name."))
    return back


def _apply_attribute_policy(request, item, bump, back):
    """Save the per-attribute meet-or-beat selection for one policy item. The
    posted `attr_ids` become the item's explicit `checked_attributes`; attributes
    NOT listed are ignored at grading time (auto-pass). An empty selection clears
    the list, restoring the engine's smart defaults. Only attributes a mutaplasmid
    can actually roll for this module are accepted, so a stale/forged id can't
    smuggle an unrelated attribute into the comparison."""
    candidates = {c["attr_id"]: c for c in rollable_attributes_for_item(item)}
    chosen: list[int] = []
    bounds: dict[str, dict] = {}
    for raw in request.POST.getlist("attr_ids"):
        try:
            attr_id = int(raw)
        except (TypeError, ValueError):
            continue
        if attr_id not in candidates or attr_id in chosen:
            continue
        chosen.append(attr_id)
        # Optional abyssal acceptance window; clamp to the rollable range.
        cand = candidates[attr_id]
        lo_raw = request.POST.get(f"min_{attr_id}")
        hi_raw = request.POST.get(f"max_{attr_id}")
        try:
            lo, hi = float(lo_raw), float(hi_raw)
        except (TypeError, ValueError):
            continue
        a_min, a_max = cand.get("abyssal_min"), cand.get("abyssal_max")
        if a_min is not None and a_max is not None:
            lo = max(a_min, min(lo, a_max))
            hi = max(a_min, min(hi, a_max))
        if lo > hi:
            lo, hi = hi, lo
        # Only store a bound when it actually narrows the full abyssal range.
        if a_min is None or a_max is None or lo > a_min or hi < a_max:
            bounds[str(attr_id)] = {"min": lo, "max": hi}
    item.checked_attributes = chosen
    item.attribute_bounds = bounds
    item.save(update_fields=["checked_attributes", "attribute_bounds"])
    bump()
    if chosen:
        messages.success(
            request,
            _("Saved %(n)s required attribute(s) for %(mod)s.")
            % {"n": len(chosen), "mod": item.module_type.name},
        )
    else:
        messages.info(
            request,
            _("Cleared required attributes for %(mod)s - using smart defaults.")
            % {"mod": item.module_type.name},
        )
    return back


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def override_add_bulk(request, item_pk: int):
    """Create multiple FitItemOverrides at once (one mode per call). The
    picker UI stages chips and submits the whole list with one Allow/Forbid
    click, so we batch the writes and bump the fit version exactly once."""
    item = get_object_or_404(
        DoctrineFitItem.objects.select_related("fit", "module_type"), pk=item_pk
    )
    back = redirect("fitcheck:manage_fit_items", fit_pk=item.fit_id)
    return _apply_bulk_overrides(
        request, item, item.fit.bump_source_policy_version, back, FitItemOverride, "item"
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def attribute_policy_save(request, item_pk: int):
    """Save the per-attribute meet-or-beat selection for one fit item."""
    item = get_object_or_404(
        DoctrineFitItem.objects.select_related("fit"), pk=item_pk
    )
    back = redirect("fitcheck:manage_fit_items", fit_pk=item.fit_id)
    return _apply_attribute_policy(request, item, item.fit.bump_source_policy_version, back)


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_GET
def attribute_candidates(request, item_pk: int):
    """JSON feed for the required-attributes modal: every attribute a mutaplasmid
    can roll for this module (incl. fitting attrs), with the standard module's
    baseline, direction, and whether it's currently required."""
    item = get_object_or_404(DoctrineFitItem.objects.select_related("module_type"), pk=item_pk)
    abyssal_type_id, abyssal_name = abyssal_name_for_item(item)
    return JsonResponse(
        {
            "module": item.module_type.name,
            "base_type_id": item.module_type_id,
            "abyssal_type_id": abyssal_type_id,
            "abyssal_name": abyssal_name or _("Abyssal %(name)s") % {"name": item.module_type.name},
            "attributes": rollable_attributes_for_item(item),
        }
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def override_add(request, item_pk: int):
    item = get_object_or_404(
        DoctrineFitItem.objects.select_related("fit", "module_type"), pk=item_pk
    )
    back = redirect("fitcheck:manage_fit_items", fit_pk=item.fit_id)
    return _apply_single_override(
        request, item, item.fit.bump_source_policy_version, back, FitItemOverride, "item"
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def override_remove(request, override_pk: int):
    override = get_object_or_404(
        FitItemOverride.objects.select_related("item__fit"), pk=override_pk
    )
    fit = override.item.fit
    override.delete()
    fit.bump_source_policy_version()
    messages.success(request, _("Override removed."))
    return redirect("fitcheck:manage_fit_items", fit_pk=fit.pk)


# ----------------------------------------- per-assignment override + attrs ---
# Twins of the source-fit endpoints above, operating on AssignmentItemPolicy /
# AssignmentItemOverride so a per-(doctrine, fit) snapshot is editable on its
# own. Each redirects back to the assignment editor and bumps that
# assignment's ladder - only the doctrine's own submissions go stale.


def _assignment_item(item_pk: int):
    from ..models import AssignmentItemPolicy

    return get_object_or_404(
        AssignmentItemPolicy.objects.select_related(
            "assignment__fit", "assignment__doctrine", "module_type"
        ),
        pk=item_pk,
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def assignment_override_add_bulk(request, item_pk: int):
    from ..models import AssignmentItemOverride

    item = _assignment_item(item_pk)
    back = redirect("fitcheck:manage_assignment_items", assignment_pk=item.assignment_id)
    return _apply_bulk_overrides(
        request, item, item.assignment.bump_version, back, AssignmentItemOverride, "assignment_item"
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def assignment_override_add(request, item_pk: int):
    from ..models import AssignmentItemOverride

    item = _assignment_item(item_pk)
    back = redirect("fitcheck:manage_assignment_items", assignment_pk=item.assignment_id)
    return _apply_single_override(
        request, item, item.assignment.bump_version, back, AssignmentItemOverride, "assignment_item"
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def assignment_override_remove(request, override_pk: int):
    from ..models import AssignmentItemOverride

    override = get_object_or_404(
        AssignmentItemOverride.objects.select_related("assignment_item__assignment__fit"),
        pk=override_pk,
    )
    assignment = override.assignment_item.assignment
    override.delete()
    assignment.bump_version()
    messages.success(request, _("Override removed."))
    return redirect("fitcheck:manage_assignment_items", assignment_pk=assignment.pk)


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def assignment_attribute_policy_save(request, item_pk: int):
    item = _assignment_item(item_pk)
    back = redirect("fitcheck:manage_assignment_items", assignment_pk=item.assignment_id)
    return _apply_attribute_policy(request, item, item.assignment.bump_version, back)


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_GET
def assignment_attribute_candidates(request, item_pk: int):
    item = _assignment_item(item_pk)
    abyssal_type_id, abyssal_name = abyssal_name_for_item(item)
    return JsonResponse(
        {
            "module": item.module_type.name,
            "base_type_id": item.module_type_id,
            "abyssal_type_id": abyssal_type_id,
            "abyssal_name": abyssal_name or _("Abyssal %(name)s") % {"name": item.module_type.name},
            "attributes": rollable_attributes_for_item(item),
        }
    )


# --------------------------------------------------------------- doctrines ---


@login_required
@permission_required("fitcheck.manage_doctrines")
def doctrine_create(request):
    """Create a doctrine - guided (wizard steps) or direct (single form),
    chosen client-side; both post the same form."""
    if request.method == "POST":
        form = DoctrineForm(request.POST)
        if form.is_valid():
            doctrine = form.save(commit=False)
            doctrine.created_by = request.user
            doctrine.save()
            form.save_m2m()
            messages.success(request, _("Doctrine '%(name)s' created.") % {"name": doctrine.name})
            return redirect("fitcheck:doctrine_detail", doctrine_pk=doctrine.pk)
    else:
        form = DoctrineForm()
    return render(
        request,
        "fitcheck/doctrine_create.html",
        {
            "form": form,
            "category_form": DoctrineCategoryForm(),
            "mode": request.GET.get("mode", "guided"),
            "page_title": _("New Doctrine"),
        },
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def doctrine_edit(request, doctrine_pk: int):
    doctrine = get_object_or_404(Doctrine, pk=doctrine_pk)
    form = DoctrineForm(request.POST, instance=doctrine)
    if form.is_valid():
        form.save()
        messages.success(request, _("Doctrine saved."))
    else:
        for fieldname, errors in form.errors.items():
            for error in errors:
                messages.error(
                    request,
                    _("%(field)s: %(error)s") % {"field": fieldname, "error": error},
                )
    return redirect("fitcheck:doctrine_detail", doctrine_pk=doctrine.pk)


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def doctrine_delete(request, doctrine_pk: int):
    doctrine = get_object_or_404(Doctrine, pk=doctrine_pk)
    name = doctrine.name
    doctrine.delete()  # fittings survive - they are standalone standards
    messages.success(
        request,
        _("Doctrine '%(name)s' deleted. Its fittings remain as standalone standards.")
        % {"name": name},
    )
    return redirect("fitcheck:index")


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def doctrine_assign_fit(request, doctrine_pk: int):
    from ..services.assignments import attach_fit_to_doctrine

    doctrine = get_object_or_404(Doctrine, pk=doctrine_pk)
    form = AssignFittingForm(request.POST, doctrine=doctrine)
    if form.is_valid():
        fit = form.cleaned_data["fit"]
        attach_fit_to_doctrine(fit, doctrine, user=request.user)
        messages.success(
            request,
            _("'%(fit)s' assigned to %(doctrine)s.") % {"fit": fit.name, "doctrine": doctrine.name},
        )
    else:
        messages.error(request, _("Pick a fitting to assign."))
    return redirect("fitcheck:doctrine_detail", doctrine_pk=doctrine.pk)


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def doctrine_assign_fits_bulk(request, doctrine_pk: int):
    """Attach many fittings to one doctrine in a single submission.

    The multi-select picker on the doctrine detail page POSTs a list of
    `fit_ids` selected from a filterable grid. Already-attached fits are
    silently ignored (idempotent). Each new attachment clones the source
    policies into a per-(doctrine, fit) FitAssignment snapshot."""
    from ..services.assignments import attach_fit_to_doctrine

    doctrine = get_object_or_404(Doctrine, pk=doctrine_pk)
    raw_ids = request.POST.getlist("fit_ids")
    fit_ids: list[int] = []
    for raw in raw_ids:
        try:
            fit_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not fit_ids:
        messages.error(request, _("Pick at least one fitting from the list."))
        return redirect("fitcheck:doctrine_detail", doctrine_pk=doctrine.pk)

    fits = list(DoctrineFit.objects.filter(pk__in=fit_ids))
    added = 0
    for fit in fits:
        if not fit.doctrines.filter(pk=doctrine.pk).exists():
            attach_fit_to_doctrine(fit, doctrine, user=request.user)
            added += 1
    messages.success(
        request,
        _("%(added)d fitting(s) assigned to %(doctrine)s.")
        % {"added": added, "doctrine": doctrine.name},
    )
    return redirect("fitcheck:doctrine_detail", doctrine_pk=doctrine.pk)


def _resolve_target_charset(user):
    """Return the EveCharacter queryset this user is allowed to inventory.

    Alliance-wide if they hold view_member_inventory; corp-only if they hold
    view_own_corp_inventory; empty queryset (view 403s) otherwise. The user's
    main character supplies the alliance/corp scope - falling back gracefully
    when no main is set."""
    from allianceauth.eveonline.models import EveCharacter

    main = getattr(getattr(user, "profile", None), "main_character", None)
    if user.has_perm("fitcheck.view_member_inventory"):
        if main and main.alliance_id:
            return EveCharacter.objects.filter(alliance_id=main.alliance_id)
        if main:
            return EveCharacter.objects.filter(corporation_id=main.corporation_id)
        return EveCharacter.objects.none()
    if user.has_perm("fitcheck.view_own_corp_inventory"):
        if main:
            return EveCharacter.objects.filter(corporation_id=main.corporation_id)
        return EveCharacter.objects.none()
    return EveCharacter.objects.none()


def _audit_selected_ships(request, fit, ships, char_by_id, tokens, selected):
    """Phase 2: grade the reviewer-selected ships on demand and persist one
    FitSubmission each. Returns {ship_item_id: {"verdict", "submission_pk"}}.

    Security: only ships present in the legitimately-listed `ships` (already
    scope- and hull-filtered by Phase 1) are graded, so the POSTed
    (character, item) pairs are never trusted blindly - a crafted POST cannot
    reach a character outside the requester's scope or an item that isn't this
    doctrine's hull."""
    from collections import defaultdict

    from ..services.check_runner import submit_fit
    from ..services.esi_assets import build_parsed_fit, is_error_limited, resolve_contents

    legitimate = {(s.character_id, s.item_id): s for s in ships}
    wanted: dict[int, list[int]] = defaultdict(list)
    for raw in selected:
        try:
            cid, iid = (int(p) for p in str(raw).split(":"))
        except (ValueError, TypeError):
            continue
        if (cid, iid) in legitimate and iid not in wanted[cid]:
            wanted[cid].append(iid)

    # Upper bound on ships graded in one POST, so the on-demand ESI / engine
    # fan-out stays predictable even if every box on the page is ticked.
    # Tunable: Settings -> Scan & Result Limits.
    max_audit_ships = ScanParameters.current().audit_ships_per_post
    graded: dict[int, dict] = {}
    rate_limited = False
    capped_ships = 0
    capped_modules = 0
    for cid, item_ids in wanted.items():
        if len(graded) >= max_audit_ships or rate_limited:
            break
        token = tokens.get(cid)
        # The owning User backs the persisted submission; fall back to the
        # requester when a corptools-served ship has no fitcheck token.
        owner = getattr(token, "user", None) or request.user
        try:
            # One fetch per character (ESI) or a narrow slice (corptools), shared
            # across every selected ship on that character.
            contents = resolve_contents(cid, item_ids, token)
        except Exception as exc:  # pragma: no cover - network dependent
            if is_error_limited(exc):
                rate_limited = True
                break
            raise
        if contents is None:
            continue
        for iid in item_ids:
            if len(graded) >= max_audit_ships:
                break
            ship = legitimate[(cid, iid)]
            try:
                parsed = build_parsed_fit(
                    owner, cid, iid, assets=contents, token=token,
                    fit_name=ship.ship_name or None,
                )
            except Exception as exc:  # pragma: no cover - network dependent
                if is_error_limited(exc):
                    rate_limited = True
                    break
                raise
            if parsed is None:
                continue
            # Defence in depth: the listing already hull-filters, so this only
            # rejects a crafted/raced item_id that isn't this doctrine's hull.
            if fit.ship_type_id and parsed.ship_type_id != fit.ship_type_id:
                continue
            if parsed.abyssal_capped:
                capped_ships += 1
                capped_modules += parsed.abyssal_capped
            submission = submit_fit(
                owner, fit, parsed,
                source=FitSubmission.Source.ESI,
                character=char_by_id.get(cid),
                doctrine=None,
            )
            graded[iid] = {"verdict": submission.verdict, "submission_pk": submission.pk}

    if rate_limited:
        messages.warning(
            request,
            _("EVE's ESI rate limit was reached mid-audit - results are partial."),
        )
    if capped_ships:
        messages.warning(
            request,
            _(
                "Abyssal module verification was capped on %(ships)d ship(s) - "
                "%(modules)d module(s) stayed unverified. Raise 'Abyssal lookups "
                "per ship' under Settings -> Scan & Result Limits and re-run the "
                "audit."
            )
            % {"ships": capped_ships, "modules": capped_modules},
        )
    if graded:
        messages.success(
            request, _("Audited %(n)d ship(s).") % {"n": len(graded)}
        )
    return graded


@login_required
def member_inventory_for_fit(request, fit_pk: int):
    """Proactive fit check: list alliance/corp members' ships matching this
    doctrine's hull; grade the ones a reviewer selects for audit.

    Two phases (decoupled so an alliance-wide scan never materialises every
    pilot's whole asset tree, nor grades hulls nobody asked about):
      - GET lists the ships in scope - a narrow read, no grading, no submissions.
      - POST grades only the ticked ships (`ships` = "character_id:item_id"
        values) and persists one FitSubmission each.

    Permission gating is dual: view_member_inventory unlocks the alliance-wide
    view; view_own_corp_inventory narrows to the requester's own corporation.
    Filters: `q` (character name contains), `corp` (corporation_id), `granted`
    (toggle to only show pilots with a valid asset-scope token)."""
    from allianceauth.eveonline.models import EveCorporationInfo

    from ..services.esi_assets import (
        get_inventory_for_characters,
        tokens_by_character,
    )

    base_qs = _resolve_target_charset(request.user)
    if not base_qs.exists():
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied()

    fit = get_object_or_404(DoctrineFit, pk=fit_pk)
    if not fit.ship_type_id:
        messages.error(request, _("This fitting has no hull set - cannot scan."))
        return redirect("fitcheck:fit_detail", fit_pk=fit.pk)

    # Filters travel in the querystring on BOTH GET and POST (the audit form
    # posts to "?<querystring>"), so the listed scope stays identical between
    # showing the ships and grading the selected ones.
    q = request.GET.get("q", "").strip()
    corp_filter = request.GET.get("corp", "").strip()
    granted_only = request.GET.get("granted", "") == "1"

    characters = base_qs
    if q:
        characters = characters.filter(character_name__icontains=q)
    if corp_filter and corp_filter.isdigit():
        characters = characters.filter(corporation_id=int(corp_filter))

    # The full roster is scanned - corptools-synced members cost a bulk DB read
    # regardless of alliance size; only the live-ESI fallback is bounded, by the
    # ScanParameters budget inside get_inventory_for_characters.
    characters = list(characters.order_by("character_name"))

    tokens = tokens_by_character(c.character_id for c in characters)
    if granted_only:
        characters = [c for c in characters if c.character_id in tokens]
    char_by_id = {c.character_id: c for c in characters}

    # Phase 1 - list ships of this hull in scope. No grading: this reads only the
    # narrow ship rows (corptools) or the ship slice of a live fetch (ESI).
    inventory = get_inventory_for_characters(
        characters, hull_type_id=fit.ship_type_id, tokens=tokens
    )
    if inventory.error_limited:
        messages.warning(
            request,
            _("EVE's ESI rate limit was reached - the member scan stopped early. "
              "Results are partial; try again in a minute."),
        )

    # Phase 2 - grade only the ships the reviewer ticked and submitted for audit.
    graded: dict[int, dict] = {}
    if request.method == "POST":
        graded = _audit_selected_ships(
            request, fit, inventory.ships, char_by_id, tokens,
            request.POST.getlist("ships"),
        )

    ship_rows = []
    for ship in inventory.ships:
        result = graded.get(ship.item_id)
        ship_rows.append({
            "ship": ship,
            "verdict": result["verdict"] if result else None,
            "submission_pk": result["submission_pk"] if result else None,
            "audited": result is not None,
        })

    # Corp dropdown options: only meaningful for alliance-scoped users.
    show_corp_filter = request.user.has_perm("fitcheck.view_member_inventory")
    corps = []
    if show_corp_filter:
        corp_ids = sorted({c.corporation_id for c in characters if c.corporation_id})
        corps = list(
            EveCorporationInfo.objects.filter(corporation_id__in=corp_ids)
            .order_by("corporation_name")
            .values("corporation_id", "corporation_name")
        )

    return render(
        request,
        "fitcheck/manage/member_inventory.html",
        {
            "fit": fit,
            "ship_rows": ship_rows,
            "without_token": inventory.characters_without_token,
            "skipped_esi": inventory.esi_fallback_skipped,
            "errors": inventory.errors,
            "filters": {
                "q": q,
                "corp": corp_filter,
                "granted": granted_only,
            },
            "querystring": request.GET.urlencode(),
            "corps": corps,
            "show_corp_filter": show_corp_filter,
            "scope_label": (
                _("alliance-wide")
                if request.user.has_perm("fitcheck.view_member_inventory")
                else _("your corporation only")
            ),
            "page_title": _("Member Inventory: %(fit)s") % {"fit": fit.name},
        },
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def fit_set_doctrines(request, fit_pk: int):
    """Replace the doctrine assignments of one fit with the posted set.

    The Edit Doctrines collapse on fit_detail.html sends `doctrine_ids`
    representing what the user wants AFTER the edit. We diff against the
    current set so the operation is one POST instead of many add/remove
    calls, and so the message reflects net change."""
    fit = get_object_or_404(DoctrineFit, pk=fit_pk)
    raw_ids = request.POST.getlist("doctrine_ids")
    requested: set[int] = set()
    for raw in raw_ids:
        try:
            requested.add(int(raw))
        except (TypeError, ValueError):
            continue
    from ..services.assignments import attach_fit_to_doctrine, detach_fit_from_doctrine

    valid = set(
        Doctrine.objects.filter(pk__in=requested).values_list("pk", flat=True)
    )
    current = set(fit.doctrines.values_list("pk", flat=True))
    to_add = valid - current
    to_remove = current - valid
    for doctrine in Doctrine.objects.filter(pk__in=to_add):
        attach_fit_to_doctrine(fit, doctrine, user=request.user)
    for doctrine in Doctrine.objects.filter(pk__in=to_remove):
        detach_fit_from_doctrine(fit, doctrine)
    if to_add or to_remove:
        messages.success(
            request,
            _("Doctrines updated: %(added)d added, %(removed)d removed.")
            % {"added": len(to_add), "removed": len(to_remove)},
        )
    else:
        messages.info(request, _("No doctrine changes."))
    return redirect("fitcheck:fit_detail", fit_pk=fit.pk)


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def doctrine_remove_fit(request, doctrine_pk: int, fit_pk: int):
    from ..services.assignments import detach_fit_from_doctrine

    doctrine = get_object_or_404(Doctrine, pk=doctrine_pk)
    fit = get_object_or_404(DoctrineFit, pk=fit_pk)
    detach_fit_from_doctrine(fit, doctrine)
    messages.success(
        request,
        _("'%(fit)s' removed from %(doctrine)s. The fitting itself still exists.")
        % {"fit": fit.name, "doctrine": doctrine.name},
    )
    return redirect("fitcheck:doctrine_detail", doctrine_pk=doctrine.pk)


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def category_add(request):
    form = DoctrineCategoryForm(request.POST)
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    next_url = request.POST.get("next", "")
    if not next_url.startswith("/"):
        next_url = "fitcheck:index"
    if form.is_valid():
        category = form.save()
        if is_ajax:
            return JsonResponse(
                {
                    "pk": category.pk,
                    "name": category.name,
                    "color": category.color,
                    "text_color": category.text_color,
                }
            )
        messages.success(request, _("Category '%(name)s' created.") % {"name": category.name})
    else:
        if is_ajax:
            return JsonResponse({"error": "invalid"}, status=400)
        messages.error(request, _("Category could not be created (name taken?)."))
    return redirect(next_url)


@login_required
@permission_required("fitcheck.manage_doctrines")
def category_list(request):
    """Standalone management of categories (the visibility objects)."""
    categories = DoctrineCategory.objects.prefetch_related(
        "selected_groups", "required_groups", "fits", "doctrines"
    ).order_by("name")
    return render(
        request,
        "fitcheck/categories/list.html",
        {"categories": categories, "page_title": _("Categories")},
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
def category_edit(request, category_pk: int | None = None):
    """Create or edit a category: colour, the two group-visibility lists, and
    the fits + doctrines it gates."""
    category = get_object_or_404(DoctrineCategory, pk=category_pk) if category_pk else None
    if request.method == "POST":
        form = DoctrineCategoryEditForm(request.POST, instance=category or DoctrineCategory())
        if form.is_valid():
            category = form.save()
            category.doctrines.set(form.cleaned_data["doctrines"])
            messages.success(request, _("Category '%(name)s' saved.") % {"name": category.name})
            return redirect("fitcheck:category_list")
        messages.error(request, _("Please fix the errors below."))
    else:
        form = DoctrineCategoryEditForm(instance=category)
    return render(
        request,
        "fitcheck/categories/edit.html",
        {
            "form": form,
            "category": category,
            "page_title": category.name if category else _("New Category"),
        },
    )


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def category_delete(request, category_pk: int):
    category = get_object_or_404(DoctrineCategory, pk=category_pk)
    name = category.name
    category.delete()
    messages.success(request, _("Category '%(name)s' deleted.") % {"name": name})
    return redirect("fitcheck:category_list")


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_GET
def fitting_search(request):
    """JSON search for the doctrine multi-select picker.

    Filters: `q` (name icontains), `group` (ship's eve_group name exact),
    `hull` (ship_type_id), `exclude_doctrine` (drop fits already assigned).
    Returns up to 30 fittings with the metadata the picker card needs."""
    query = request.GET.get("q", "").strip()
    group_name = request.GET.get("group", "").strip()
    hull = request.GET.get("hull", "").strip()
    exclude_doctrine = request.GET.get("exclude_doctrine", "").strip()

    qs = (
        DoctrineFit.objects.select_related("ship_type", "ship_type__eve_group")
        .filter(is_active=True)
        # Annotated so the result loop below doesn't run one COUNT query per fit.
        .annotate(num_doctrines=Count("doctrines", distinct=True))
    )
    if query:
        qs = qs.filter(name__icontains=query)
    if group_name:
        qs = qs.filter(ship_type__eve_group__name=group_name)
    if hull and hull.isdigit():
        qs = qs.filter(ship_type_id=int(hull))
    if exclude_doctrine and exclude_doctrine.isdigit():
        qs = qs.exclude(doctrines__pk=int(exclude_doctrine))

    results = [
        {
            "fit_id": fit.pk,
            "name": fit.name,
            "ship_type_id": fit.ship_type_id,
            "ship_name": fit.ship_type.name,
            "group_name": (
                fit.ship_type.eve_group.name if fit.ship_type.eve_group_id else ""
            ),
            "doctrine_count": fit.num_doctrines,
        }
        for fit in qs.order_by("ship_type__name", "name")[:30]
    ]
    return JsonResponse({"results": results})


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_GET
def ship_group_list(request):
    """JSON: ship groups (Battleship, Force Auxiliary, ...) that have at
    least one active fitting. Powers the picker's group filter."""
    from eveuniverse.models import EveGroup

    group_ids = set(
        DoctrineFit.objects.filter(is_active=True)
        .values_list("ship_type__eve_group_id", flat=True)
        .distinct()
    )
    group_ids.discard(None)
    groups = (
        EveGroup.objects.filter(id__in=group_ids)
        .order_by("name")
        .values_list("name", flat=True)
    )
    return JsonResponse({"results": list(groups)})


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_GET
def ship_search(request):
    """JSON autocomplete for the doctrine image picker."""
    query = request.GET.get("q", "").strip()
    results = []
    if len(query) >= 2:
        results = [
            {"type_id": row[0], "name": row[1]}
            for row in SdeType.objects.filter(
                category_id=EveCategoryId.SHIP, published=True, name__icontains=query
            )
            .order_by("name")
            .values_list("type_id", "name")[:12]
        ]
    return JsonResponse({"results": results})


# --------------------------------------------------- fittings plugin import ---


@login_required
@permission_required("fitcheck.manage_doctrines")
@require_POST
def doctrine_resync_from_plugin(request, doctrine_pk: int):
    """Pull updates for one doctrine from the colcrunch `fittings` plugin.
    Preserves our policy data; only refreshes fitting BOMs + membership."""
    from ..services.fittings_import import resync_doctrine_from_plugin

    doctrine = get_object_or_404(Doctrine, pk=doctrine_pk)
    if doctrine.source_plugin_pk is None:
        messages.error(
            request,
            _("This doctrine wasn't imported from the fittings plugin - nothing to pull."),
        )
        return redirect("fitcheck:doctrine_detail", doctrine_pk=doctrine.pk)

    report = resync_doctrine_from_plugin(doctrine, request.user)
    if report.error:
        messages.error(request, _("Resync failed: %(err)s") % {"err": report.error})
    elif not report.changed_anything and not report.categories_synced:
        messages.info(request, _("No changes - %(name)s is up to date.") % {"name": doctrine.name})
    else:
        messages.success(
            request,
            _(
                "Synced %(name)s: %(added)d added, %(updated)d updated, "
                "%(dropped)d dropped (%(unchanged)d unchanged)."
            )
            % {
                "name": doctrine.name,
                "added": len(report.fits_added),
                "updated": len(report.fits_updated),
                "dropped": len(report.fits_dropped),
                "unchanged": len(report.unchanged),
            },
        )
        if report.categories_synced:
            messages.info(
                request,
                _("Re-synced %(c)s categories from the fittings plugin - their group "
                  "visibility now mirrors the plugin (this overwrites local edits to "
                  "those categories).")
                % {"c": len(set(report.categories_synced))},
            )
        _emit_resync_slot_warnings(request, doctrine, report)
    return redirect("fitcheck:doctrine_detail", doctrine_pk=doctrine.pk)


def _emit_resync_slot_warnings(request, doctrine: Doctrine, report) -> None:
    """Run the slot-layout lint against every fit the resync touched (added or
    refreshed). The report only carries fit names, so resolve them back to
    DoctrineFit rows scoped to this doctrine; each warning is prefixed with
    the fit name since the lint text itself only names the hull."""
    touched_names = set(report.fits_added) | set(report.fits_updated)
    if not touched_names:
        return
    for fit in doctrine.fits.filter(name__in=touched_names).select_related("ship_type"):
        for warning in slot_layout_warnings(fit):
            messages.warning(
                request, _("%(fit)s: %(warning)s") % {"fit": fit.name, "warning": warning}
            )


@login_required
@permission_required("fitcheck.manage_doctrines")
def fittings_plugin_import(request):
    from ..services.fittings_import import (
        fittings_installed,
        import_plugin_baseline_fits,
        import_plugin_doctrines,
        list_plugin_doctrines,
    )

    if not fittings_installed():
        messages.info(
            request,
            _("The 'fittings' plugin is not installed - create doctrines directly instead."),
        )
        return redirect("fitcheck:index")

    if request.method == "POST":
        doctrine_ids = [int(pk) for pk in request.POST.getlist("doctrines") if pk.isdigit()]
        report = import_plugin_doctrines(request.user, doctrine_ids)
        if request.POST.get("include_baseline"):
            baseline = import_plugin_baseline_fits(request.user)
            report.fits_created += baseline.fits_created
            report.skipped += baseline.skipped
            report.categories_synced += baseline.categories_synced
        if report.imported_anything:
            messages.success(
                request,
                _("Imported %(d)s doctrines and %(f)s fittings (%(s)s skipped).")
                % {
                    "d": len(report.doctrines_created),
                    "f": len(report.fits_created) + len(report.fits_linked),
                    "s": len(report.skipped),
                },
            )
            if report.categories_synced:
                messages.info(
                    request,
                    _("Synced %(c)s categories from the fittings plugin - their "
                      "group visibility now mirrors the plugin (this overwrites any "
                      "local edits to those categories).")
                    % {"c": len(set(report.categories_synced))},
                )
        else:
            messages.info(request, _("Nothing new to import."))
        return redirect("fitcheck:index")

    return render(
        request,
        "fitcheck/fittings_import.html",
        {
            "plugin_doctrines": list_plugin_doctrines(),
            "page_title": _("Import from Fittings Plugin"),
        },
    )


# ------------------------------------------------------------ policy editor ---


@login_required
@permission_required("fitcheck.manage_policies")
def policy_list(request):
    policies = CompliancePolicy.objects.prefetch_related("rules", "fits").order_by("name")
    return render(
        request,
        "fitcheck/policies/list.html",
        {"policies": policies, "page_title": _("Compliance Policies")},
    )


@login_required
def settings_home(request):
    """Settings hub: the fittings-import ingress methods plus site-wide
    enforcement / global settings. Each section is gated by its own permission
    (`manage_doctrines` for import, `manage_policies` for enforcement); the tab
    shows for anyone who can reach at least one section."""
    from django.core.exceptions import PermissionDenied

    from ..services.fittings_import import fittings_installed

    can_import = request.user.has_perm("fitcheck.manage_doctrines")
    can_enforce = request.user.has_perm("fitcheck.manage_policies")
    if not (can_import or can_enforce):
        raise PermissionDenied()
    return render(
        request,
        "fitcheck/settings/home.html",
        {
            "page_title": _("Settings"),
            "can_import": can_import,
            "can_enforce": can_enforce,
            # Diagnostics is plugin-admin only (same tier as enforcement).
            "can_diagnose": request.user.has_perm("fitcheck.manage_policies"),
            "fittings_available": fittings_installed(),
        },
    )


@login_required
@permission_required("fitcheck.manage_policies")
def diagnostics(request):
    """Admin Diagnostics & Health page: app-health stats, the inventory doctor,
    and the reporting-data controls (snapshot run-now / purge). Reads are
    DB/cache only - never calls ESI (the CLI command keeps the deliberate
    --esi mode)."""
    from ..services import diagnostics as diag

    context = {
        "page_title": _("Diagnostics & Health"),
        "health": diag.health_summary(),
    }
    ident = request.GET.get("character", "").strip()
    if ident:
        context["doctor_ident"] = ident
        character = diag.resolve_character(ident)
        if character is None:
            context["doctor_not_found"] = True
        else:
            context["doctor_character"] = character
            context["doctor"] = diag.inventory_report(
                character.character_id, with_esi=False
            )
    return render(request, "fitcheck/settings/diagnostics.html", context)


@login_required
@permission_required("fitcheck.manage_policies")
@require_POST
def snapshot_run_now(request):
    """Ad-hoc compliance-snapshot run: enqueue the beat task once. A same-day
    re-run updates today's rows in place, so this is always safe to press."""
    from ..tasks import take_compliance_snapshots

    take_compliance_snapshots.delay()
    messages.info(
        request,
        _(
            "Compliance snapshot queued. Today's rows will appear (or refresh) "
            "once the worker has processed it - reload this page in a minute."
        ),
    )
    return redirect("fitcheck:diagnostics")


@login_required
@permission_required("fitcheck.manage_policies")
@require_POST
def snapshot_purge(request):
    """Purge collected snapshot rows without database access: keep the most
    recent N days (blank = delete everything). Snapshot data is derived and
    safe to delete; purging only shortens the available trend history."""
    from ..services.snapshots import purge_snapshots

    raw = (request.POST.get("keep_days") or "").strip()
    keep_days = None
    if raw:
        try:
            keep_days = int(raw)
        except ValueError:
            keep_days = -1
        if keep_days < 0:
            messages.error(request, _("Days to keep must be a whole number (or blank for all)."))
            return redirect("fitcheck:diagnostics")
    deleted = purge_snapshots(older_than_days=keep_days)
    if keep_days is None:
        messages.success(
            request,
            _("Purged all compliance-snapshot rows (%(n)d deleted).") % {"n": deleted},
        )
    else:
        messages.success(
            request,
            _("Purged %(n)d snapshot row(s) older than %(d)d days.")
            % {"n": deleted, "d": keep_days},
        )
    return redirect("fitcheck:diagnostics")


@login_required
@permission_required("fitcheck.manage_policies")
def enforcement_settings(request):
    """Site-wide enforcement modes for implants / FEB / fuel / boosters."""
    settings_obj = EnforcementSettings.current()
    if request.method == "POST":
        form = EnforcementSettingsForm(request.POST, instance=settings_obj)
        if form.is_valid():
            form.save()
            messages.success(request, _("Enforcement settings saved."))
            return redirect("fitcheck:enforcement_settings")
    else:
        form = EnforcementSettingsForm(instance=settings_obj)
    return render(
        request,
        "fitcheck/policies/enforcement.html",
        {"form": form, "page_title": _("Enforcement Settings")},
    )


@login_required
@permission_required("fitcheck.manage_policies")
def scan_parameters(request):
    """Admin-tunable scan/result limits (member-scan ESI budget, audit batch
    size, abyssal lookup budget, list page size)."""
    params = ScanParameters.current()
    if request.method == "POST":
        form = ScanParametersForm(request.POST, instance=params)
        if form.is_valid():
            form.save()
            messages.success(request, _("Scan parameters saved."))
            return redirect("fitcheck:scan_parameters")
    else:
        form = ScanParametersForm(instance=params)
    return render(
        request,
        "fitcheck/settings/parameters.html",
        {"form": form, "page_title": _("Scan & Result Limits")},
    )


def _policy_section_label(section: str) -> str:
    if section == Section.CARGO:
        return str(_("Cargo / fuel bay"))
    return str(Section(section).label)


@login_required
@permission_required("fitcheck.manage_policies")
def policy_edit(request, policy_pk: int | None = None):
    policy = get_object_or_404(CompliancePolicy, pk=policy_pk) if policy_pk else None
    # Pre-built (seeded) policies are editable only by superusers; managers may
    # view their config on the Policies page and apply them, but not modify them.
    if policy and policy.is_builtin and not request.user.is_superuser:
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied()
    existing_rules = (
        {rule.section: rule for rule in policy.rules.all()} if policy else {}
    )

    if request.method == "POST":
        form = CompliancePolicyForm(request.POST, instance=policy)
        rule_forms = [
            (section, PolicySlotRuleForm(request.POST, prefix=section))
            for section in POLICY_SECTIONS
        ]
        if form.is_valid() and all(rf.is_valid() for _s, rf in rule_forms):
            obj = form.save(commit=False)
            if policy is None:
                obj.created_by = request.user
            obj.save()
            obj.rules.all().delete()
            for section, rule_form in rule_forms:
                data = rule_form.cleaned_data
                if not data.get("enforcement"):
                    continue
                PolicySlotRule.objects.create(
                    policy=obj,
                    section=section,
                    enforcement=data["enforcement"],
                    allow_mutated=data.get("allow_mutated", True),
                    min_quantity_pct=data.get("min_quantity_pct") or 100,
                )
            messages.success(request, _("Policy saved."))
            return redirect("fitcheck:policy_list")
        messages.error(request, _("Please fix the errors below."))
    else:
        form = CompliancePolicyForm(instance=policy)
        rule_forms = []
        for section in POLICY_SECTIONS:
            rule = existing_rules.get(section)
            initial = (
                {
                    "enforcement": rule.enforcement,
                    "allow_mutated": rule.allow_mutated,
                    "min_quantity_pct": rule.min_quantity_pct,
                }
                if rule
                else {}
            )
            rule_forms.append((section, PolicySlotRuleForm(prefix=section, initial=initial)))

    rows = [
        {
            "section": section,
            "label": _policy_section_label(section),
            "form": rule_form,
            "is_quantity": section in LEEWAY_SECTIONS,
        }
        for section, rule_form in rule_forms
    ]
    return render(
        request,
        "fitcheck/policies/edit.html",
        {
            "form": form,
            "policy": policy,
            "rows": rows,
            "page_title": policy.name if policy else _("New policy"),
        },
    )


@login_required
@permission_required("fitcheck.manage_policies")
@require_POST
def policy_delete(request, policy_pk: int):
    policy = get_object_or_404(CompliancePolicy, pk=policy_pk)
    # Built-in (seeded) policies can never be deleted - disable them instead.
    if policy.is_builtin:
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied()
    name = policy.name
    policy.delete()
    messages.success(request, _("Policy '%(name)s' deleted.") % {"name": name})
    return redirect("fitcheck:policy_list")


@login_required
@permission_required("fitcheck.manage_policies")
@require_POST
def policy_toggle_disabled(request, policy_pk: int):
    """Soft-disable or re-enable a policy. Available for every policy (incl.
    built-ins) - it's the non-destructive way to retire a policy that can't be
    deleted. Disabled policies are no longer offered when applying to a fit."""
    from django.utils import timezone

    policy = get_object_or_404(CompliancePolicy, pk=policy_pk)
    if policy.is_disabled:
        policy.disabled_at = None
        policy.save(update_fields=["disabled_at"])
        messages.success(request, _("Policy '%(name)s' enabled.") % {"name": policy.name})
    else:
        policy.disabled_at = timezone.now()
        policy.save(update_fields=["disabled_at"])
        messages.success(request, _("Policy '%(name)s' disabled.") % {"name": policy.name})
    return redirect("fitcheck:policy_list")
