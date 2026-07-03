"""Persistence layer around the pure compliance engine: create submissions,
run checks, store findings, keep the audit log."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from eveuniverse.models import EveType

from allianceauth.eveonline.models import EveCharacter

from ..models import (
    ComplianceFinding,
    Doctrine,
    DoctrineFit,
    FitAssignment,
    FitSubmission,
    SubmissionActionLog,
    SubmissionItem,
)
from ..signals import compliance_changed
from .compliance import check_fit, check_fit_for_doctrine
from .doctrine_import import _get_or_create_eve_type
from .fit_data import FitItem, ParsedFit


def _emit_compliance_changed(
    submission: FitSubmission,
    *,
    old_verdict: str | None,
    old_status: str | None,
    actor: User | None,
) -> None:
    compliance_changed.send(
        sender=FitSubmission,
        submission=submission,
        user=submission.user,
        fit=submission.doctrine_fit,
        doctrine=submission.doctrine,
        old_verdict=old_verdict,
        new_verdict=submission.verdict,
        old_status=old_status,
        new_status=submission.status,
        actor=actor,
    )


def _current_policy_version(doctrine_fit: DoctrineFit, doctrine: Doctrine | None) -> int:
    """The live policy-ladder value a new or re-checked submission snapshots:
    the (doctrine, fit) assignment's version when grading a doctrine snapshot,
    else the fit's own source-policy version."""
    if doctrine is None:
        return doctrine_fit.source_policy_version
    version = (
        FitAssignment.objects.filter(doctrine=doctrine, fit=doctrine_fit)
        .values_list("version", flat=True)
        .first()
    )
    return version or 0


@transaction.atomic
def submit_fit(
    user: User,
    doctrine_fit: DoctrineFit,
    parsed: ParsedFit,
    *,
    source: str = FitSubmission.Source.EFT,
    eft_text: str = "",
    character: EveCharacter | None = None,
    esi_fitting_id: int | None = None,
    doctrine: Doctrine | None = None,
) -> FitSubmission:
    """Create a submission from a parsed fit and run the engine on it.

    When `doctrine` is given the submission is graded against that
    (doctrine, fit) policy snapshot; otherwise it grades against the fit's
    source-level defaults."""
    submission = FitSubmission.objects.create(
        user=user,
        character=character or getattr(user.profile, "main_character", None),
        doctrine_fit=doctrine_fit,
        doctrine=doctrine,
        fit_version=doctrine_fit.version,
        policy_version=_current_policy_version(doctrine_fit, doctrine),
        source=source,
        eft_text=eft_text,
        esi_fitting_id=esi_fitting_id,
        esi_ship_item_id=parsed.source_ship_item_id,
        ship_type=(
            _get_or_create_eve_type(parsed.ship_type_id) if parsed.ship_type_id else None
        ),
        frigate_escape_bay_type_id=parsed.frigate_escape_bay_type_id,
        verdict=FitSubmission.Verdict.ERROR,  # replaced below
    )
    for item in parsed.items:
        SubmissionItem.objects.create(
            submission=submission,
            section=item.section,
            eve_type=_get_or_create_eve_type(item.type_id),
            quantity=item.quantity,
            charge_type=(
                _get_or_create_eve_type(item.charge_type_id) if item.charge_type_id else None
            ),
            mutated_attributes=(
                {str(k): v for k, v in item.mutated_attributes.items()}
                if item.mutated_attributes is not None
                else None
            ),
            mutation_source=item.mutation_source,
        )
    SubmissionActionLog.objects.create(
        submission=submission, actor=user, action=SubmissionActionLog.Action.SUBMITTED
    )
    _run_and_store(submission, parsed, SubmissionActionLog.Action.AUTO_CHECKED)
    _emit_compliance_changed(
        submission, old_verdict=None, old_status=None, actor=user
    )
    return submission


@transaction.atomic
def recheck_submission(submission: FitSubmission, actor: User | None = None) -> FitSubmission:
    """Re-run the engine using the stored items (after doctrine/policy changes)."""
    parsed = parsed_fit_from_submission(submission)
    old_verdict = submission.verdict
    old_status = submission.status
    submission.fit_version = submission.doctrine_fit.version
    submission.policy_version = _current_policy_version(
        submission.doctrine_fit, submission.doctrine
    )
    _run_and_store(submission, parsed, SubmissionActionLog.Action.RECHECKED, actor=actor)
    _emit_compliance_changed(
        submission, old_verdict=old_verdict, old_status=old_status, actor=actor
    )
    return submission


def parsed_fit_from_submission(submission: FitSubmission) -> ParsedFit:
    items = [
        FitItem(
            section=row.section,
            type_id=row.eve_type_id,
            quantity=row.quantity,
            charge_type_id=row.charge_type_id,
            mutated_attributes=(
                {int(k): v for k, v in row.mutated_attributes.items()}
                if row.mutated_attributes is not None
                else None
            ),
            mutation_source=row.mutation_source,
        )
        for row in submission.items.all()
    ]
    return ParsedFit(
        ship_type_id=submission.ship_type_id,
        fit_name=f"Submission #{submission.pk}",
        items=items,
        source_ship_item_id=submission.esi_ship_item_id,
    )


def build_finding_rows(
    result, submission: FitSubmission | None = None
) -> list[ComplianceFinding]:
    """The engine's ``Finding`` dataclasses mapped onto (unsaved)
    ``ComplianceFinding`` rows - the single translation between the pure
    engine result and the model the templates render. With ``submission=None``
    the rows are render-only (sandbox checks); callers persisting them pass
    the submission and ``bulk_create`` the list."""
    eve_type_cache: dict[int, EveType] = {}

    def cached_type(type_id: int | None) -> EveType | None:
        if type_id is None:
            return None
        if type_id not in eve_type_cache:
            eve_type_cache[type_id] = _get_or_create_eve_type(type_id)
        return eve_type_cache[type_id]

    return [
        ComplianceFinding(
            submission=submission,
            section=finding.section,
            code=finding.code,
            expected_type=cached_type(finding.expected_type_id),
            actual_type=cached_type(finding.actual_type_id),
            expected_qty=finding.expected_qty,
            actual_qty=finding.actual_qty,
            message=finding.message[:500],
            allowed_alternatives=finding.allowed_alternatives,
            attribute_results=finding.attribute_results,
            sort_order=index,
        )
        for index, finding in enumerate(result.findings)
    ]


def _run_and_store(
    submission: FitSubmission,
    parsed: ParsedFit,
    log_action: str,
    actor: User | None = None,
) -> None:
    if submission.doctrine_id:
        result = check_fit_for_doctrine(
            parsed, submission.doctrine_fit, submission.doctrine
        )
    else:
        result = check_fit(parsed, submission.doctrine_fit)
    submission.verdict = result.verdict
    submission.save(update_fields=["verdict", "fit_version", "policy_version"])

    submission.findings.all().delete()
    ComplianceFinding.objects.bulk_create(build_finding_rows(result, submission))
    SubmissionActionLog.objects.create(
        submission=submission,
        actor=actor,
        action=log_action,
        comment=f"Verdict: {submission.get_verdict_display()}",
    )


def matching_fits_for(user, ship_type_id: int):
    """Active fitting standards for this hull the user is allowed to check
    against: standards in any visible doctrine, plus standalone standards."""
    visible = Doctrine.objects.visible_to(user).active()
    return (
        DoctrineFit.objects.filter(is_active=True, ship_type_id=ship_type_id)
        .filter(Q(doctrines__isnull=True) | Q(doctrines__in=visible))
        .distinct()
        .order_by("name")
    )


def gradeable_doctrines_for(fit: DoctrineFit, user) -> list[Doctrine]:
    """The active doctrines this fit belongs to that the user may see - each
    one carries its own (doctrine, fit) policy snapshot to grade against.
    Empty when the fit is standalone or no membership is visible."""
    visible = Doctrine.objects.visible_to(user).active()
    return list(fit.doctrines.filter(pk__in=visible).order_by("name"))


def validate_parsed_ship(
    user,
    parsed: ParsedFit,
    *,
    character: EveCharacter | None = None,
    eft_text: str = "",
) -> list[FitSubmission]:
    """Check one owned ship against every matching standard. One ship can
    satisfy several fittings across several doctrines - each (fit, doctrine)
    pair gets its own submission graded against that doctrine's policy
    snapshot. A standalone fit (in no visible doctrine) grades once against
    its source-level defaults."""
    submissions = []
    for fit in matching_fits_for(user, parsed.ship_type_id):
        doctrines = gradeable_doctrines_for(fit, user)
        if doctrines:
            for doctrine in doctrines:
                submissions.append(
                    submit_fit(
                        user,
                        fit,
                        parsed,
                        source=FitSubmission.Source.ESI,
                        eft_text=eft_text,
                        character=character,
                        doctrine=doctrine,
                    )
                )
        else:
            submissions.append(
                submit_fit(
                    user,
                    fit,
                    parsed,
                    source=FitSubmission.Source.ESI,
                    eft_text=eft_text,
                    character=character,
                )
            )
    return submissions


def build_deficit_multibuy(submission: FitSubmission) -> str:
    """An EVE-multibuy-ready list (one `<name> <qty>` per line) of the modules /
    ammo / refit / booster gaps in a submission, so a reviewer can tell the pilot
    exactly what to acquire to pass."""
    from collections import defaultdict

    from ..constants import Section

    Code = ComplianceFinding.Code
    deficits: dict[str, int] = defaultdict(int)
    for finding in submission.findings.select_related("expected_type"):
        if finding.expected_type_id is None:
            continue
        name = finding.expected_type.name if finding.expected_type else str(
            finding.expected_type_id
        )
        if finding.code in (Code.MISSING, Code.IMPLANT_MISSING):
            qty = finding.expected_qty or 1
        elif finding.code == Code.QTY_SHORT:
            qty = (finding.expected_qty or 0) - (finding.actual_qty or 0)
        elif finding.code == Code.NOT_ALLOWED:
            qty = finding.actual_qty or finding.expected_qty or 1
        elif finding.code == Code.UNVERIFIED and finding.section == Section.BOOSTER:
            qty = finding.expected_qty or 1
        else:
            continue
        if qty > 0:
            deficits[name] += qty
    return "\n".join(f"{name} {qty}" for name, qty in sorted(deficits.items()))


@transaction.atomic
def review_submission(
    submission: FitSubmission, reviewer: User, approve: bool, comment: str = ""
) -> FitSubmission:
    # Asymmetric comment rule: approving releases the pilot into the doctrine
    # and needs no justification, but rejecting always has to say *why* so the
    # pilot can refit accordingly.
    if not approve and not comment.strip():
        raise ValueError("A comment is required when rejecting a submission.")
    old_status = submission.status
    old_verdict = submission.verdict
    submission.status = (
        FitSubmission.Status.APPROVED if approve else FitSubmission.Status.REJECTED
    )
    submission.reviewed_by = reviewer
    submission.reviewed_at = timezone.now()
    submission.review_comment = comment
    submission.save(
        update_fields=["status", "reviewed_by", "reviewed_at", "review_comment"]
    )
    SubmissionActionLog.objects.create(
        submission=submission,
        actor=reviewer,
        action=(
            SubmissionActionLog.Action.APPROVED
            if approve
            else SubmissionActionLog.Action.REJECTED
        ),
        comment=comment,
    )
    _emit_compliance_changed(
        submission, old_verdict=old_verdict, old_status=old_status, actor=reviewer
    )
    return submission
