"""Helpers for attaching/detaching DoctrineFits to/from Doctrines via the
FitAssignment model. Cloning the source policies and overrides into a fresh
per-(doctrine, fit) snapshot is the whole point of the rework, so every
write path that links a fit to a doctrine should go through here.

The legacy `DoctrineFit.doctrines` M2M is kept in sync for back-compat
(read paths still iterate it for UI badges, queries, etc.), but the source
of truth for per-doctrine policies is the FitAssignment + AssignmentItemPolicy
+ AssignmentItemOverride tree.
"""

from __future__ import annotations

from django.db import transaction

from ..models import (
    AssignmentItemOverride,
    AssignmentItemPolicy,
    Doctrine,
    DoctrineFit,
    FitAssignment,
)


def _policy_kwargs_from(obj) -> dict:
    """Pull the policy fields off a DoctrineFitItem or AssignmentItemPolicy,
    copying the JSON containers so the snapshot never shares a mutable ref."""
    return {
        "policy": obj.policy,
        "allowed_meta_groups": list(obj.allowed_meta_groups or []),
        "checked_attributes": list(obj.checked_attributes or []),
        "attribute_bounds": dict(obj.attribute_bounds or {}),
        "allow_mutated": obj.allow_mutated,
        "min_quantity_pct": obj.min_quantity_pct,
        "notes": obj.notes,
    }


def _create_assignment_policy(assignment, source_item, policy_kwargs, overrides) -> None:
    """Create one AssignmentItemPolicy (+ its overrides) for `source_item`."""
    policy = AssignmentItemPolicy.objects.create(
        assignment=assignment,
        source_item=source_item,
        section=source_item.section,
        module_type_id=source_item.module_type_id,
        quantity=source_item.quantity,
        charge_type_id=source_item.charge_type_id,
        **policy_kwargs,
    )
    for override in overrides:
        AssignmentItemOverride.objects.create(
            assignment_item=policy,
            alt_type_id=override["alt_type_id"],
            mode=override["mode"],
        )


def _clone_source_items_into(assignment: FitAssignment) -> None:
    """Populate an assignment's snapshot from its fit's current source items
    (their defaults + overrides). The fresh-attach and repair paths share this."""
    for item in assignment.fit.items.prefetch_related("overrides").all():
        overrides = [
            {"alt_type_id": o.alt_type_id, "mode": o.mode} for o in item.overrides.all()
        ]
        _create_assignment_policy(assignment, item, _policy_kwargs_from(item), overrides)


@transaction.atomic
def attach_fit_to_doctrine(
    fit: DoctrineFit, doctrine: Doctrine, *, user=None
) -> FitAssignment:
    """Link a fit to a doctrine. Creates the FitAssignment + clones the fit's
    current source policies + overrides into a fresh per-doctrine snapshot,
    then overlays the doctrine's standing policy preset (if any) on top - a
    fresh attachment always reflects the doctrine's current preset, even when
    the source fit was configured differently. No version bump: nothing has
    graded against this brand-new assignment yet. Idempotent: a second call
    returns the existing assignment unchanged (the preset is NOT re-applied)."""
    fit.doctrines.add(doctrine)  # back-compat M2M
    assignment, created = FitAssignment.objects.get_or_create(
        doctrine=doctrine,
        fit=fit,
        defaults={
            "created_by": user,
            "charge_policy": fit.charge_policy,
            "charge_min_quantity_pct": fit.charge_min_quantity_pct,
        },
    )
    if not created:
        return assignment
    _clone_source_items_into(assignment)
    if doctrine.compliance_policy_id:
        from .policies import apply_policy_to_assignment  # local import: avoid a cycle

        apply_policy_to_assignment(assignment, doctrine.compliance_policy)
    return assignment


def capture_assignment_policies(fit: DoctrineFit) -> dict[int, dict]:
    """Snapshot every assignment's per-item policy + overrides, keyed by
    ``assignment_id -> {(section, module_type_id): {policy_kwargs, overrides}}``.

    Call this BEFORE a BOM rebuild deletes the source DoctrineFitItems: the
    ``AssignmentItemPolicy.source_item`` FK is ``CASCADE``, so that delete wipes
    every assignment snapshot. `rebuild_assignment_snapshots` replays this map
    onto the freshly materialised items so per-doctrine exceptions survive."""
    captures: dict[int, dict] = {}
    for assignment in fit.assignments.prefetch_related("item_policies__overrides"):
        by_key: dict[tuple, dict] = {}
        for policy in assignment.item_policies.all():
            by_key[(policy.section, policy.module_type_id)] = {
                "policy_kwargs": _policy_kwargs_from(policy),
                "overrides": [
                    {"alt_type_id": o.alt_type_id, "mode": o.mode}
                    for o in policy.overrides.all()
                ],
            }
        captures[assignment.pk] = by_key
    return captures


@transaction.atomic
def rebuild_assignment_snapshots(fit: DoctrineFit, captures: dict[int, dict]) -> None:
    """Recreate each assignment's snapshot from `fit`'s NEW source items after a
    BOM rebuild, carrying forward the captured per-assignment policy/overrides by
    ``(section, module_type_id)``. Modules new to the BOM clone the (already
    carried-forward) source item's defaults; modules dropped from the BOM fall
    away. Mirrors the source-item carry-forward in `fit_edit.apply_captured_policy`
    but per assignment, so each doctrine keeps its independent exceptions."""
    new_items = list(fit.items.prefetch_related("overrides").all())
    for assignment in fit.assignments.all():
        cap = captures.get(assignment.pk, {})
        # The CASCADE from the source-item delete already cleared these; the
        # explicit delete keeps the rebuild idempotent if called another way.
        AssignmentItemPolicy.objects.filter(assignment=assignment).delete()
        for item in new_items:
            saved = cap.get((item.section, item.module_type_id))
            if saved is not None:
                _create_assignment_policy(
                    assignment, item, saved["policy_kwargs"], saved["overrides"]
                )
            else:
                overrides = [
                    {"alt_type_id": o.alt_type_id, "mode": o.mode}
                    for o in item.overrides.all()
                ]
                _create_assignment_policy(
                    assignment, item, _policy_kwargs_from(item), overrides
                )


@transaction.atomic
def reclone_empty_assignment_snapshots() -> list[int]:
    """One-shot repair for assignments whose snapshot was wiped by a pre-fix BOM
    update (the `source_item` CASCADE). Re-clones from the fit's current source
    items for any assignment that has zero policies but whose fit still has
    items. Safe + idempotent: there is no UI to deliberately empty a snapshot
    while keeping source items, so zero-policies-with-source-items reliably means
    'damaged by the bug'. Returns the repaired assignment pks."""
    repaired: list[int] = []
    for assignment in FitAssignment.objects.select_related("fit"):
        if assignment.item_policies.exists():
            continue
        if not assignment.fit.items.exists():
            continue
        _clone_source_items_into(assignment)
        repaired.append(assignment.pk)
    return repaired


def _override_set(item) -> set[tuple[int, str]]:
    """The ``(alt_type_id, mode)`` set of an item's overrides. Works for both a
    DoctrineFitItem (FitItemOverride) and an AssignmentItemPolicy
    (AssignmentItemOverride) - both expose ``overrides`` with the same shape."""
    return {(o.alt_type_id, o.mode) for o in item.overrides.all()}


def _comparable_policy(obj) -> tuple:
    """An order-insensitive view of an item's policy fields for drift
    comparison. The list fields (meta groups / checked attributes) are sorted
    because selection order carries no meaning, and attribute_bounds is sorted
    by key, so a snapshot that merely re-ordered them is not flagged as drift."""
    kw = _policy_kwargs_from(obj)
    return (
        kw["policy"],
        tuple(sorted(kw["allowed_meta_groups"])),
        tuple(sorted(kw["checked_attributes"])),
        tuple(sorted(kw["attribute_bounds"].items(), key=lambda kv: kv[0])),
        kw["allow_mutated"],
        kw["min_quantity_pct"],
        kw["notes"],
    )


def assignment_item_differs(policy: AssignmentItemPolicy) -> bool:
    """True when an AssignmentItemPolicy no longer matches its source
    DoctrineFitItem - either a policy field or the override set has drifted.
    A missing ``source_item`` (the source row was deleted out from under the
    snapshot) counts as differing."""
    source = policy.source_item
    if source is None:
        return True
    if _comparable_policy(policy) != _comparable_policy(source):
        return True
    return _override_set(policy) != _override_set(source)


def _charge_policy_differs(assignment: FitAssignment) -> bool:
    """True when the assignment's charge-demand governance (charge_policy +
    charge_min_quantity_pct) no longer matches its source fit's - the
    assignment-level counterpart to assignment_item_differs, since these two
    fields live on FitAssignment/DoctrineFit rather than a per-item row."""
    fit = assignment.fit
    return (
        assignment.charge_policy != fit.charge_policy
        or assignment.charge_min_quantity_pct != fit.charge_min_quantity_pct
    )


def assignment_differs(assignment: FitAssignment) -> bool:
    """True when an assignment's snapshot has drifted from the fit's current
    source template: any item differs, the charge-demand pair differs, OR the
    set of (section, module_type) no longer matches the fit's source items (a
    module was added to or removed from the BOM since this snapshot was
    cloned). This is the per-(doctrine, fit) 'differs from template' state -
    distinct from the fit_version-based 'stale submission' concept."""
    if _charge_policy_differs(assignment):
        return True
    policies = list(assignment.item_policies.all())
    snapshot_keys = {(p.section, p.module_type_id) for p in policies}
    source_keys = {(i.section, i.module_type_id) for i in assignment.fit.items.all()}
    if snapshot_keys != source_keys:
        return True
    return any(assignment_item_differs(p) for p in policies)


def differing_assignments(fit: DoctrineFit) -> set[int]:
    """The pks of `fit`'s assignments whose snapshot differs from the source
    template. One pass with the prefetches the diff needs, so a 'used in N
    doctrines' panel can flag each combination without N+1 queries."""
    source_keys = {(i.section, i.module_type_id) for i in fit.items.all()}
    result: set[int] = set()
    assignments = fit.assignments.prefetch_related(
        "item_policies__overrides", "item_policies__source_item__overrides"
    )
    for assignment in assignments:
        charge_differs = (
            assignment.charge_policy != fit.charge_policy
            or assignment.charge_min_quantity_pct != fit.charge_min_quantity_pct
        )
        policies = list(assignment.item_policies.all())
        snapshot_keys = {(p.section, p.module_type_id) for p in policies}
        if (
            charge_differs
            or snapshot_keys != source_keys
            or any(assignment_item_differs(p) for p in policies)
        ):
            result.add(assignment.pk)
    return result


@transaction.atomic
def resync_assignment_from_source(assignment: FitAssignment) -> None:
    """Discard this assignment's policy snapshot and re-clone it from the fit's
    CURRENT source items (policy fields + overrides), then re-overlay the
    doctrine's standing policy preset (if any). A re-synced assignment means
    "current source template + the doctrine's standing preset" - the explicit,
    per-combination counterpart to the automatic carry-forward in
    `rebuild_assignment_snapshots`: the manager chooses when a combination
    should re-adopt the fit template, so any per-combination customizations on
    this assignment are intentionally dropped. Snapshots stay independent until
    a re-sync is requested - other assignments are untouched."""
    AssignmentItemPolicy.objects.filter(assignment=assignment).delete()
    _clone_source_items_into(assignment)
    assignment.charge_policy = assignment.fit.charge_policy
    assignment.charge_min_quantity_pct = assignment.fit.charge_min_quantity_pct
    assignment.save(update_fields=["charge_policy", "charge_min_quantity_pct"])
    if assignment.doctrine.compliance_policy_id:
        from .policies import apply_policy_to_assignment  # local import: avoid a cycle

        apply_policy_to_assignment(assignment, assignment.doctrine.compliance_policy)


@transaction.atomic
def detach_fit_from_doctrine(fit: DoctrineFit, doctrine: Doctrine) -> bool:
    """Remove the assignment + back-compat M2M link. Returns True if it was
    attached. The standalone DoctrineFit survives."""
    fit.doctrines.remove(doctrine)
    deleted_count, _ = FitAssignment.objects.filter(
        doctrine=doctrine, fit=fit
    ).delete()
    return deleted_count > 0
