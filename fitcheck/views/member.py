import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)

from allianceauth.eveonline.models import EveCharacter

from ..forms import AssignFittingForm, DoctrineCategoryForm, DoctrineForm
from ..managers import visible_categories_among, visible_categories_for
from ..models import Doctrine, DoctrineFit, DoctrineFitItem, FitSubmission
from ..services.check_runner import (
    build_deficit_multibuy,
    gradeable_doctrines_for,
    validate_parsed_ship,
)
from ..services.eft_parser import parse_eft, render_eft
from .common import paginate as _paginate


def _can_review(user) -> bool:
    return user.has_perm("fitcheck.review_submissions") or user.has_perm(
        "fitcheck.secure_group_management"
    )


def _visible_fit_or_404(request, fit_pk: int) -> DoctrineFit:
    fit = get_object_or_404(
        DoctrineFit.objects.select_related("ship_type").prefetch_related("doctrines__categories"),
        pk=fit_pk,
    )
    if request.user.has_perm("fitcheck.manage_doctrines") or _can_review(request.user):
        return fit
    # Category-driven visibility: a fit with no effective categories is public;
    # otherwise an admitting category (its own or via a doctrine) is required.
    if DoctrineFit.objects.visible_to(request.user).filter(pk=fit.pk).exists():
        return fit
    raise PermissionDenied


@login_required
@permission_required("fitcheck.basic_access")
def index(request):
    doctrines = (
        Doctrine.objects.visible_to(request.user)
        .active()
        .prefetch_related(
            "categories",
            Prefetch(
                "fits",
                queryset=DoctrineFit.objects.filter(is_active=True).select_related("ship_type"),
            ),
        )
        .order_by("name")
    )
    category_pk = request.GET.get("category", "")
    if category_pk.isdigit():
        doctrines = doctrines.filter(categories__pk=category_pk)

    latest_by_fit = {}
    for submission in FitSubmission.objects.for_user(request.user).order_by(
        "doctrine_fit_id", "-created_at"
    ):
        latest_by_fit.setdefault(submission.doctrine_fit_id, submission)
    doctrines = list(doctrines)
    for doctrine in doctrines:
        for fit in doctrine.fits.all():
            fit.latest_submission = latest_by_fit.get(fit.pk)
        # A doctrine can be visible via one category while carrying another
        # the user isn't admitted to (OR across categories) - don't let the
        # card's badge row name a category the chip bar itself would hide.
        doctrine.visible_categories = visible_categories_among(
            request.user, doctrine.categories.all()
        )

    fittings_available = False
    if request.user.has_perm("fitcheck.manage_doctrines"):
        from ..services.fittings_import import fittings_installed

        fittings_available = fittings_installed()

    return render(
        request,
        "fitcheck/index.html",
        {
            "doctrines": doctrines,
            "all_categories": visible_categories_for(request.user),
            "active_category": category_pk,
            "fittings_available": fittings_available,
            "page_title": _("Doctrines"),
        },
    )


@login_required
@permission_required("fitcheck.basic_access")
def doctrine_detail(request, doctrine_pk: int):
    doctrine = get_object_or_404(
        Doctrine.objects.visible_to(request.user)
        .select_related("compliance_policy")
        .prefetch_related("categories", "fits__ship_type", "fits__doctrines"),
        pk=doctrine_pk,
    )
    can_manage = request.user.has_perm("fitcheck.manage_doctrines")
    context = {
        "doctrine": doctrine,
        "can_manage": can_manage,
        # Same OR-across-categories exposure as the index cards: a doctrine
        # visible via one category can carry another the user can't see.
        "visible_categories": visible_categories_among(
            request.user, doctrine.categories.all()
        ),
        "page_title": doctrine.name,
    }
    if can_manage:
        from ..forms import ApplyPolicyForm
        from ..models import CompliancePolicy

        context["assign_form"] = AssignFittingForm(doctrine=doctrine)
        context["doctrine_form"] = DoctrineForm(instance=doctrine)
        context["category_form"] = DoctrineCategoryForm()
        context["apply_policy_form"] = ApplyPolicyForm()
        context["has_policies"] = CompliancePolicy.objects.exists()
    return render(request, "fitcheck/doctrine_detail.html", context)


@login_required
@permission_required("fitcheck.basic_access")
def fit_detail(request, fit_pk: int):
    fit = _visible_fit_or_404(request, fit_pk)
    items = list(
        DoctrineFitItem.objects.filter(fit=fit)
        .select_related("module_type", "charge_type")
        .order_by("section", "module_type__name")
    )
    from ..constants import SECTION_ORDER
    from ..services.substitutions import resolve_allowed_bulk

    allowed_sets = resolve_allowed_bulk(items)
    items.sort(key=lambda i: SECTION_ORDER.get(i.section, 99))
    for item in items:
        allowed = allowed_sets.get(item.pk)
        item.alternatives = allowed.alternatives(limit=25) if allowed else []
        item.mutated_allowed = bool(allowed and allowed.mutated_candidates)
        item.no_enforcement = bool(allowed and allowed.allow_any)
    sections = []
    for item in items:
        if not sections or sections[-1]["code"] != item.section:
            sections.append(
                {"code": item.section, "section": item.get_section_display(), "items": []}
            )
        sections[-1]["items"].append(item)
    from ..services.eft_parser import aggregate_for_buy

    buy_lines = "\n".join(
        f"{name} x{qty}" for name, qty in aggregate_for_buy(items, fit.ship_type_id)
    )
    all_doctrines: list[Doctrine] = []
    assigned_ids: set[int] = set()
    if request.user.has_perm("fitcheck.manage_doctrines"):
        all_doctrines = list(
            Doctrine.objects.order_by("name").only("pk", "name", "is_active")
        )
        assigned_ids = set(fit.doctrines.values_list("pk", flat=True))

    from ..models import FitAssignment

    assignment_pk_by_doctrine = dict(
        FitAssignment.objects.filter(fit=fit).values_list("doctrine_id", "pk")
    )
    # Flag combinations whose per-doctrine policy copy has drifted from this
    # fit's template (manager-only - it's the surface they edit).
    drifted: set[int] = set()
    if request.user.has_perm("fitcheck.manage_doctrines"):
        from ..services.assignments import differing_assignments

        drifted = differing_assignments(fit)
    doctrine_list = list(fit.doctrines.all().prefetch_related("categories"))
    doctrine_chips = [
        {
            "doctrine": d,
            "assignment_pk": assignment_pk_by_doctrine.get(d.pk),
            "differs": assignment_pk_by_doctrine.get(d.pk) in drifted,
        }
        for d in doctrine_list
    ]
    # Categories shown once per fit, regardless of how many doctrines carry
    # them: the fit's own directly-assigned categories plus every category of
    # every doctrine it belongs to, de-duped by pk and ordered by name.
    categories_by_pk = {}
    for category in fit.categories.all():
        categories_by_pk[category.pk] = category
    for doctrine in doctrine_list:
        for category in doctrine.categories.all():
            categories_by_pk[category.pk] = category
    # A fit can be admitted via one category while carrying another (its own,
    # or via a doctrine) the user isn't admitted to (OR across categories) -
    # don't let the badge row name a category the user can't otherwise see.
    fit_categories = visible_categories_among(
        request.user, sorted(categories_by_pk.values(), key=lambda c: c.name)
    )
    return render(
        request,
        "fitcheck/fit_detail.html",
        {
            "fit": fit,
            "sections": sections,
            "buy_lines": buy_lines,
            "all_doctrines": all_doctrines,
            "assigned_doctrine_ids": assigned_ids,
            "doctrine_chips": doctrine_chips,
            "fit_categories": fit_categories,
            # Everyone with visibility may open the test bench; the page itself
            # limits non-staff to the check-only sandbox.
            "can_test": True,
            "page_title": fit.name,
        },
    )


def _inject_entered_values(specs, post_data):
    """Carry entered values back into the form when re-rendering after errors."""
    for spec in specs:
        for attr in spec["attributes"]:
            attr["value"] = post_data.get(
                f"mstat-{spec['type_id']}-{attr['attr_id']}", ""
            ).strip()


def _apply_manual_stats(parsed, specs, post_data):
    """Attach manually entered rolled values to abyssal items. Returns a list of
    input errors (empty = success)."""
    errors = []
    rolls_by_type: dict[int, dict[int, float]] = {}
    for spec in specs:
        rolls: dict[int, float] = {}
        for attr in spec["attributes"]:
            raw = post_data.get(f"mstat-{spec['type_id']}-{attr['attr_id']}", "").strip()
            if not raw:
                errors.append(
                    _("Missing value for %(attr)s on %(name)s.")
                    % {"attr": attr["label"], "name": spec["name"]}
                )
                continue
            try:
                rolls[attr["attr_id"]] = float(raw)
            except ValueError:
                errors.append(
                    _("'%(value)s' is not a number (%(attr)s on %(name)s).")
                    % {"value": raw, "attr": attr["label"], "name": spec["name"]}
                )
        rolls_by_type[spec["type_id"]] = rolls
    if errors:
        return errors
    for item in parsed.items:
        if item.mutated_attributes is None and item.type_id in rolls_by_type:
            item.mutated_attributes = rolls_by_type[item.type_id]
            item.mutation_source = "MAN"
    return []


@login_required
@permission_required("fitcheck.basic_access")
def submit_eft(request, fit_pk: int):
    """EFT-paste test bench. This is a pure sandbox: any member who can see
    the fit can grade a pasted fit against the engine, but nothing is ever
    persisted - no FitSubmission row, no review-queue entry, no reviewer
    notification. Pasted text can't be tied to what a pilot actually has
    fitted, so it can never stand in for a real submission. Submissions that
    count toward compliance come only from ESI/corptools inventory
    validation (Pilot Fittings tab)."""
    fit = _visible_fit_or_404(request, fit_pk)
    gradeable = gradeable_doctrines_for(fit, request.user)
    if request.method == "POST":
        eft_text = request.POST.get("eft_text", "").strip()
        # Selected doctrines to grade against (each carries its own policy
        # snapshot). Restricted to this fit's gradeable set; empty = grade
        # once against the fit's source-level defaults.
        chosen = [d for d in gradeable if str(d.pk) in request.POST.getlist("doctrines")]
        if not eft_text:
            messages.error(request, _("Please paste a fit in EFT format."))
        else:
            from ..services.substitutions import collect_mutated_stat_specs

            parsed = parse_eft(eft_text)
            specs = (
                collect_mutated_stat_specs(parsed.items, fit)
                if not parsed.has_blocking_errors
                else []
            )
            if specs:
                mutated_context = {
                    "fit": fit,
                    "specs": specs,
                    "eft_text": eft_text,
                    "selected_doctrines": chosen,
                    "page_title": _("Mutated Module Stats"),
                }
                if request.POST.get("stats_step") == "1":
                    errors = _apply_manual_stats(parsed, specs, request.POST)
                    if errors:
                        for error in errors:
                            messages.error(request, error)
                        _inject_entered_values(specs, request.POST)
                        return render(
                            request, "fitcheck/submit_mutated.html", mutated_context
                        )
                else:
                    # The paste names abyssal modules but carries no rolled stats
                    # (in-game copy) - ask for them before grading.
                    return render(
                        request, "fitcheck/submit_mutated.html", mutated_context
                    )
            # Pure-engine pass: nothing is persisted, nothing reaches the
            # review queue. The unsaved ComplianceFinding rows carry the
            # same display surface the findings partial already renders.
            from ..services.check_runner import build_finding_rows
            from ..services.compliance import check_fit, check_fit_for_doctrine

            doctrines = chosen or [None]
            results = []
            for doctrine in doctrines:
                result = (
                    check_fit_for_doctrine(parsed, fit, doctrine)
                    if doctrine is not None
                    else check_fit(parsed, fit)
                )
                results.append(
                    {
                        "doctrine": doctrine,
                        "verdict": result.verdict,
                        "verdict_display": FitSubmission.Verdict(result.verdict).label,
                        "findings": build_finding_rows(result),
                    }
                )
            return render(
                request,
                "fitcheck/sandbox_results.html",
                {
                    "fit": fit,
                    "parsed": parsed,
                    "results": results,
                    "page_title": _("Sandbox Check"),
                },
            )
    return render(
        request,
        "fitcheck/submit_eft.html",
        {
            "fit": fit,
            "gradeable_doctrines": gradeable,
            "page_title": _("Test a Fit"),
        },
    )


# Sentinel GET value for "graded against source defaults" (doctrine IS NULL) -
# distinct from "" (no filter applied), which can't itself be a pk.
_NO_DOCTRINE_FILTER = "none"


@login_required
@permission_required("fitcheck.basic_access")
def pilot_fittings(request):
    """The member's own submissions - never anyone else's. Hidden (soft-removed)
    rows never appear here, but stay visible to reviewers, reports, and Secure
    Groups compliance, which query FitSubmission directly rather than through
    this view."""
    from ..services.sde_loader import ensure_sde_loading

    own_submissions = FitSubmission.objects.for_user(request.user).filter(
        hidden_at__isnull=True
    )

    submissions = own_submissions.select_related(
        "doctrine_fit", "doctrine", "ship_type", "character"
    )
    status = request.GET.get("status", "")
    if status:
        submissions = submissions.filter(status=status)
    verdict = request.GET.get("verdict", "")
    if verdict:
        submissions = submissions.filter(verdict=verdict)
    doctrine = request.GET.get("doctrine", "")
    if doctrine == _NO_DOCTRINE_FILTER:
        submissions = submissions.filter(doctrine__isnull=True)
    elif doctrine.isdigit():
        submissions = submissions.filter(doctrine_id=doctrine)
    character = request.GET.get("character", "")
    if character.isdigit():
        submissions = submissions.filter(character_id=character)
    ship = request.GET.get("ship", "").strip()
    if ship:
        submissions = submissions.filter(ship_type__name__icontains=ship)

    # Filter option lists are scoped to the user's own (visible) submissions -
    # never every doctrine/character in the install - so a pilot never sees the
    # name of a doctrine they otherwise can't reach.
    doctrines = Doctrine.objects.filter(
        pk__in=own_submissions.exclude(doctrine__isnull=True).values_list(
            "doctrine_id", flat=True
        )
    ).order_by("name")
    has_source_default_submissions = own_submissions.filter(
        doctrine__isnull=True
    ).exists()
    characters = EveCharacter.objects.filter(
        pk__in=own_submissions.exclude(character__isnull=True).values_list(
            "character_id", flat=True
        )
    ).order_by("character_name")

    page_obj, elided_range, querystring = _paginate(
        request, submissions.order_by("-created_at")
    )
    return render(
        request,
        "fitcheck/pilot_fittings.html",
        {
            "submissions": page_obj,
            "page_obj": page_obj,
            "elided_range": elided_range,
            "querystring": querystring,
            "status_filter": status,
            "verdict_filter": verdict,
            "doctrine_filter": doctrine,
            "character_filter": character,
            "ship_filter": ship,
            "doctrines": doctrines,
            "has_source_default_submissions": has_source_default_submissions,
            "no_doctrine_filter_value": _NO_DOCTRINE_FILTER,
            "characters": characters,
            "statuses": FitSubmission.Status,
            "verdicts": FitSubmission.Verdict,
            "main_character": getattr(request.user.profile, "main_character", None),
            "sde_loaded": ensure_sde_loading(),
            "page_title": _("Pilot Fittings"),
        },
    )


def _filtered_inventory(request):
    """Fetch the account's ship inventory and apply the GET filters.

    `type_id` pre-filters to one hull (set by the 'Validate my ships' button on
    a fitting detail page); the remaining filters are user-driven and live
    within that pre-filtered subset.
    """
    from ..services.esi_assets import get_ship_inventory

    inventory = get_ship_inventory(request.user)
    ships = inventory.ships

    raw_type_id = request.GET.get("type_id", "")
    try:
        pinned_type_id = int(raw_type_id) if raw_type_id else None
    except ValueError:
        pinned_type_id = None
    pinned_type_name = ""
    if pinned_type_id is not None:
        for ship in ships:
            if ship.type_id == pinned_type_id:
                pinned_type_name = ship.type_name
                break
        if not pinned_type_name:
            # The pilot owns none of this hull, so the name isn't in `ships`.
            # Resolve it (SDE -> eveuniverse -> ESI) so the alert shows the hull
            # name, not a bare type_id.
            from ..services.eft_parser import resolve_render_names

            pinned_type_name = resolve_render_names([pinned_type_id]).get(
                pinned_type_id, ""
            )
        ships = [s for s in ships if s.type_id == pinned_type_id]

    character = request.GET.get("character", "")
    location = request.GET.get("location", "")
    group = request.GET.get("group", "")
    query = request.GET.get("q", "").strip().lower()

    filters = {
        "characters": sorted({s.character_name for s in ships}),
        "locations": sorted({s.location_name for s in ships}),
        "groups": sorted({s.group_name for s in ships if s.group_name}),
        "character": character,
        "location": location,
        "group": group,
        "q": query,
        "type_id": pinned_type_id,
        "type_name": pinned_type_name,
    }
    if character:
        ships = [s for s in ships if s.character_name == character]
    if location:
        ships = [s for s in ships if s.location_name == location]
    if group:
        ships = [s for s in ships if s.group_name == group]
    if query:
        ships = [
            s
            for s in ships
            if query in s.type_name.lower() or query in (s.ship_name or "").lower()
        ]
    return inventory, ships, filters


@login_required
@permission_required("fitcheck.basic_access")
def grant_all_esi(request):
    """One SSO consent for every ESI scope a pilot's audit features use - assets +
    structures (My Ships inventory & location names), implants (verify plugged-in
    implants), and fittings-write (Save-to-EVE) - so a pilot grants once instead
    of once per feature. Scopes already shared from another Auth app, or served by
    corptools, are reused (see esi_assets.existing_token / get_ship_inventory), so
    this only prompts for what's genuinely missing.

    next=inventory returns to My Ships after the grant; otherwise Pilot Fittings."""
    from esi.decorators import token_required

    from ..services.esi_assets import PILOT_GRANT_SCOPES

    @token_required(scopes=PILOT_GRANT_SCOPES)
    def _receive(request, token):
        messages.success(
            request,
            _("ESI access granted for %(name)s.") % {"name": token.character_name},
        )
        if request.GET.get("next") == "inventory":
            return redirect("fitcheck:ship_inventory")
        return redirect("fitcheck:pilot_fittings")

    return _receive(request)


@login_required
@permission_required("fitcheck.basic_access")
def add_fittings_write_token(request):
    """Kick off the SSO flow that grants the fittings write scope.

    The next_fit query param carries the fit pk so we can POST it immediately
    after the grant lands, sparing the pilot a second click."""
    from esi.decorators import token_required

    from ..services.esi_assets import FITTINGS_WRITE_SCOPES

    @token_required(scopes=FITTINGS_WRITE_SCOPES)
    def _receive(request, token):
        next_fit = request.GET.get("next_fit")
        messages.success(
            request,
            _("Save-to-EVE token added for %(name)s.")
            % {"name": token.character_name},
        )
        if next_fit and next_fit.isdigit():
            return redirect("fitcheck:fit_detail", fit_pk=int(next_fit))
        return redirect("fitcheck:index")

    return _receive(request)


@login_required
@permission_required("fitcheck.basic_access")
@require_POST
def save_fit_to_eve_view(request, fit_pk: int):
    """POST a doctrine fit into the pilot's in-game saved fittings.

    Uses the user's main character. If the write token is missing we send
    them through the SSO grant flow with `next_fit` so they come back here
    and can try again with one click."""
    from ..services.esi_fittings import NoFittingsTokenError, save_fit_to_eve

    fit = _visible_fit_or_404(request, fit_pk)
    main = getattr(request.user.profile, "main_character", None)
    if main is None:
        messages.error(request, _("Set a main character before saving to EVE."))
        return redirect("fitcheck:fit_detail", fit_pk=fit.pk)

    try:
        fitting_id = save_fit_to_eve(request.user, main.character_id, fit)
    except NoFittingsTokenError:
        url = reverse("fitcheck:add_fittings_write_token")
        return redirect(f"{url}?next_fit={fit.pk}")
    except Exception:
        # Don't echo the raw exception to the page - ESI error bodies can
        # carry operational detail. The full traceback is in the server log.
        logger.exception("Save-to-EVE failed for fit %s", fit.pk)
        messages.error(
            request,
            _("EVE rejected the fit. Try again in a minute; if it keeps "
              "failing, ask an admin to check the server log."),
        )
        return redirect("fitcheck:fit_detail", fit_pk=fit.pk)

    messages.success(
        request,
        _("Saved %(name)s to %(char)s's in-game fittings (id %(id)s).")
        % {"name": fit.name, "char": main.character_name, "id": fitting_id},
    )
    return redirect("fitcheck:fit_detail", fit_pk=fit.pk)


@login_required
@permission_required("fitcheck.basic_access")
def ship_inventory(request):
    """Pick real ships from ESI assets and validate them against the standards."""
    if request.method == "POST":
        from ..services.esi_assets import (
            build_parsed_fit,
            get_active_implants,
            resolve_assets,
            ship_names_for,
            user_tokens_by_character,
        )

        characters = {
            o.character.character_id: o.character
            for o in request.user.character_ownerships.select_related("character")
        }
        # Group the selected ships by character so each character's asset
        # tree / ship names / implants are fetched ONCE, not once per ship.
        # Only the requester's own characters are gradeable - a character_id
        # outside their ownerships is dropped (never resolved against the
        # corptools cache, which needs no token).
        wanted: dict[int, list[int]] = {}
        for selection in request.POST.getlist("ships")[:25]:
            try:
                character_id, ship_item_id = (int(p) for p in selection.split(":"))
            except ValueError:
                continue
            if character_id not in characters:
                continue
            wanted.setdefault(character_id, []).append(ship_item_id)

        results = []
        capped_ships = 0
        capped_modules = 0
        tokens, _missing = user_tokens_by_character(request.user)
        for character_id, ship_item_ids in wanted.items():
            token = tokens.get(character_id)
            assets = resolve_assets(character_id, token)
            names = (
                ship_names_for(token, character_id, ship_item_ids) if assets else {}
            )
            implants = get_active_implants(character_id) if assets else None
            for ship_item_id in ship_item_ids:
                parsed = (
                    build_parsed_fit(
                        request.user,
                        character_id,
                        ship_item_id,
                        assets=assets,
                        token=token,
                        asset_names=names,
                        implant_type_ids=implants,
                    )
                    if assets is not None
                    else None
                )
                if parsed is None:
                    messages.error(
                        request,
                        _("Ship %(id)s could not be read from ESI.")
                        % {"id": ship_item_id},
                    )
                    continue
                if parsed.abyssal_capped:
                    capped_ships += 1
                    capped_modules += parsed.abyssal_capped
                submissions = validate_parsed_ship(
                    request.user,
                    parsed,
                    character=characters.get(character_id),
                    eft_text=render_eft(parsed),
                )
                results.append(
                    {
                        "parsed": parsed,
                        "ship_type_id": parsed.ship_type_id,
                        "submissions": submissions,
                    }
                )
        if capped_ships:
            messages.warning(
                request,
                _(
                    "Abyssal module verification was capped on %(ships)d ship(s) - "
                    "%(modules)d module(s) stayed unverified. Ask an auditor to raise "
                    "'Abyssal lookups per ship' under Settings -> Scan & Result Limits, "
                    "then re-check."
                )
                % {"ships": capped_ships, "modules": capped_modules},
            )
        if results:
            from ..tasks import notify_reviewers_new_submission

            for result in results:
                for submission in result["submissions"]:
                    notify_reviewers_new_submission.delay(submission.pk)
            return render(
                request,
                "fitcheck/inventory_results.html",
                {"results": results, "page_title": _("Validation Results")},
            )
        messages.warning(request, _("No ships were selected."))
        return redirect("fitcheck:ship_inventory")

    from ..services.esi_assets import characters_missing_pilot_scopes
    from ..services.sde_loader import ensure_sde_loading

    inventory, ships, filters = _filtered_inventory(request)
    return render(
        request,
        "fitcheck/inventory.html",
        {
            "inventory": inventory,
            "ships": ships,
            "filters": filters,
            "sde_loaded": ensure_sde_loading(),
            "characters_missing_esi": characters_missing_pilot_scopes(request.user),
            "page_title": _("My Ships"),
        },
    )


# NOTE: the member-side "audit my saved fittings" flow was removed - a pilot's
# saved fittings are a plan, not proof of what they own, so grading them gives
# false assurance; the inventory-based self-audit (ship_inventory) is the real
# member path. The ESI saved-fittings read plumbing
# (esi_fittings.fetch_saved_fittings / parsed_fit_from_saved) is kept for the
# planned admin-side alliance-fittings import.


@login_required
@permission_required("fitcheck.basic_access")
def submission_detail(request, submission_pk: int):
    submission = get_object_or_404(
        FitSubmission.objects.select_related(
            "doctrine_fit", "doctrine", "ship_type", "user", "reviewed_by"
        ),
        pk=submission_pk,
    )
    if submission.user != request.user and not _can_review(request.user):
        raise PermissionDenied
    findings = submission.findings.select_related("expected_type", "actual_type")
    mutated_items = submission.items.exclude(mutation_source="").select_related("eve_type")
    # The full captured loadout, grouped by section, so the reviewer can see
    # everything ESI returned - including drone/fuel/cargo bay contents that the
    # compliance comparison doesn't surface (extras in "at least N" sections).
    submitted_items = submission.items.select_related("eve_type").order_by(
        "section", "eve_type__name"
    )
    can_review = _can_review(request.user) and submission.status == FitSubmission.Status.PENDING
    is_owner = submission.user == request.user
    # Frigate Escape Bay is informational only and only meaningful for the
    # hull classes that actually have one. Detect via the ship_type's eve_group
    # name so we don't have to maintain a SDE group_id allowlist.
    feb_capable_groups = frozenset(
        {"Battleship", "Black Ops", "Marauder"}
    )
    group_name = (
        submission.ship_type.eve_group.name
        if submission.ship_type and submission.ship_type.eve_group
        else ""
    )
    show_feb_panel = group_name in feb_capable_groups
    feb_type = None
    if show_feb_panel and submission.frigate_escape_bay_type_id:
        from eveuniverse.models import EveType

        feb_type = EveType.objects.filter(
            id=submission.frigate_escape_bay_type_id
        ).select_related("eve_group").first()

    # What changed since this submission was graded - only meaningful (and
    # only computable) when the fit moved on and an archived BOM covers it.
    stale_diff = None
    if submission.is_stale:
        from ..services.fit_diff import diff_for_submission

        stale_diff = diff_for_submission(submission)

    # Manager jump-link to the exact per-(doctrine, fit) policy this submission
    # was graded against - the surface to edit when a verdict looks wrong.
    policy_assignment_pk = None
    if submission.doctrine_id and request.user.has_perm("fitcheck.manage_doctrines"):
        from ..models import FitAssignment

        policy_assignment_pk = (
            FitAssignment.objects.filter(
                doctrine_id=submission.doctrine_id, fit_id=submission.doctrine_fit_id
            )
            .values_list("pk", flat=True)
            .first()
        )

    return render(
        request,
        "fitcheck/submission_detail.html",
        {
            "submission": submission,
            "findings": findings,
            "stale_diff": stale_diff,
            "mutated_items": mutated_items,
            "submitted_items": submitted_items,
            "policy_assignment_pk": policy_assignment_pk,
            "can_review": can_review,
            "is_owner": is_owner,
            "can_delete": is_owner and submission.status == FitSubmission.Status.PENDING,
            "can_recheck": is_owner and submission.source != FitSubmission.Source.EFT,
            "show_feb_panel": show_feb_panel,
            "feb_type": feb_type,
            "log_entries": submission.log.select_related("actor"),
            "missing_multibuy": (
                build_deficit_multibuy(submission) if (can_review or is_owner) else ""
            ),
            "page_title": _("Submission #%(pk)s") % {"pk": submission.pk},
        },
    )


_RECHECK_COOLDOWN_SECONDS = 30


@login_required
@permission_required("fitcheck.basic_access")
@require_POST
def submissions_delete_bulk(request):
    """Pilot multi-remove from the Pilot Fittings table. Own pending submissions
    are hard-deleted (unchanged); own rejected submissions are soft-hidden from
    this history view only - reviewer decisions stay on the record for
    reviewers/reports. Approved submissions are never touched from here."""
    raw_pks = request.POST.getlist("submission_pks")
    pks: list[int] = []
    for raw in raw_pks:
        try:
            pks.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not pks:
        messages.info(request, _("Pick at least one submission to remove."))
        return redirect("fitcheck:pilot_fittings")
    targets = FitSubmission.objects.filter(
        pk__in=pks,
        user=request.user,
        status__in=(FitSubmission.Status.PENDING, FitSubmission.Status.REJECTED),
    )
    pending_pks = list(
        targets.filter(status=FitSubmission.Status.PENDING).values_list("pk", flat=True)
    )
    rejected_pks = list(
        targets.filter(status=FitSubmission.Status.REJECTED).values_list("pk", flat=True)
    )
    deleted_count = 0
    if pending_pks:
        _deleted, detail = FitSubmission.objects.filter(pk__in=pending_pks).delete()
        # `detail` counts every cascaded row; we only want the submission count.
        deleted_count = detail.get("fitcheck.FitSubmission", 0)
    hidden_count = 0
    if rejected_pks:
        hidden_count = FitSubmission.objects.filter(pk__in=rejected_pks).update(
            hidden_at=timezone.now()
        )
    total = deleted_count + hidden_count
    if total:
        messages.success(
            request,
            ngettext(
                "Removed %(n)s submission from your history.",
                "Removed %(n)s submissions from your history.",
                total,
            )
            % {"n": total},
        )
    else:
        messages.warning(
            request,
            _(
                "No matching pending or rejected submissions were removed "
                "(already approved, or not yours)."
            ),
        )
    return redirect("fitcheck:pilot_fittings")


@login_required
@permission_required("fitcheck.basic_access")
@require_POST
def submission_delete(request, submission_pk: int):
    """Pilot removes their own submission from history. A PENDING submission is
    hard-deleted (unreviewed, nothing to preserve). A REJECTED submission is
    soft-hidden instead - it disappears from the pilot's own Pilot Fittings
    list but reviewers/reports still see it, since the reviewer's decision
    stays on the audit trail. Approved submissions can't be removed at all."""
    submission = get_object_or_404(FitSubmission, pk=submission_pk)
    if submission.user != request.user:
        raise PermissionDenied
    if submission.status == FitSubmission.Status.PENDING:
        submission.delete()
        messages.success(request, _("Submission removed."))
        return redirect("fitcheck:pilot_fittings")
    if submission.status == FitSubmission.Status.REJECTED:
        submission.hidden_at = timezone.now()
        submission.save(update_fields=["hidden_at"])
        messages.success(request, _("Submission removed from your history."))
        return redirect("fitcheck:pilot_fittings")
    messages.error(
        request,
        _("This submission has already been reviewed and can't be removed."),
    )
    return redirect("fitcheck:submission_detail", submission_pk=submission.pk)


@login_required
@permission_required("fitcheck.basic_access")
@require_POST
def submission_recheck(request, submission_pk: int):
    """Re-check this fit and keep only the newest result. Only ESI-sourced
    submissions can be re-checked - this re-pulls the ship's latest fit from
    EVE (falling back to re-grading the stored copy if the live pull fails).
    Legacy EFT-paste submissions predate the sandbox-only paste flow and
    can't be re-verified against anything, so they stay visible as read-only
    history instead. Either way the previous submission is replaced."""
    from django.core.cache import cache

    from ..services.check_runner import parsed_fit_from_submission, submit_fit

    submission = get_object_or_404(
        FitSubmission.objects.select_related("doctrine_fit", "character", "doctrine"),
        pk=submission_pk,
    )
    if submission.user != request.user:
        raise PermissionDenied
    if submission.source == FitSubmission.Source.EFT:
        messages.error(
            request,
            _(
                "This submission came from a pasted fit and is a legacy record - "
                "pasted fits can no longer be re-checked. Re-checking now "
                "re-validates against your EVE inventory."
            ),
        )
        return redirect("fitcheck:submission_detail", submission_pk=submission.pk)

    key = f"fitcheck:revalidate:{request.user.pk}:{submission.doctrine_fit_id}"
    if not cache.add(key, True, timeout=_RECHECK_COOLDOWN_SECONDS):
        messages.warning(
            request,
            _("Please wait %(s)ss before re-checking the same fitting again.")
            % {"s": _RECHECK_COOLDOWN_SECONDS},
        )
        return redirect("fitcheck:submission_detail", submission_pk=submission.pk)

    parsed = None
    eft_text = submission.eft_text
    character_id = submission.character.character_id if submission.character else None
    if submission.esi_ship_item_id and character_id:
        from ..services.esi_assets import build_parsed_fit

        try:
            parsed = build_parsed_fit(
                request.user, character_id, submission.esi_ship_item_id, fetch_implants=True
            )
        except Exception:
            logger.exception("Re-check ESI re-pull failed for submission %s", submission.pk)
            parsed = None
        if parsed is not None:
            eft_text = render_eft(parsed)
        else:
            messages.warning(
                request,
                _("Couldn't pull the latest fit from EVE - re-graded the stored copy instead."),
            )
    if parsed is None:
        parsed = parsed_fit_from_submission(submission)

    new = submit_fit(
        request.user,
        submission.doctrine_fit,
        parsed,
        source=submission.source,
        eft_text=eft_text,
        character=submission.character,
        doctrine=submission.doctrine,
    )
    submission.delete()  # replace: only the newest submission is kept

    from ..tasks import notify_reviewers_new_submission

    notify_reviewers_new_submission.delay(new.pk)
    messages.success(request, _("Re-check complete - the previous submission was replaced."))
    return redirect("fitcheck:submission_detail", submission_pk=new.pk)
