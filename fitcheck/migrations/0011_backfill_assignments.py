"""Backfill FitAssignment + AssignmentItemPolicy + AssignmentItemOverride
from the existing (M2M doctrines, DoctrineFitItem, FitItemOverride) data.

Per-(doctrine, fit) snapshots replace the bare M2M as the source of truth
for per-doctrine policy. The M2M itself stays in place for read-side
compatibility, but new attachments go through FitAssignment.
"""

from django.db import migrations


def backfill_assignments(apps, schema_editor):
    DoctrineFit = apps.get_model("fitcheck", "DoctrineFit")
    FitAssignment = apps.get_model("fitcheck", "FitAssignment")
    AssignmentItemPolicy = apps.get_model("fitcheck", "AssignmentItemPolicy")
    AssignmentItemOverride = apps.get_model("fitcheck", "AssignmentItemOverride")

    for fit in DoctrineFit.objects.prefetch_related("doctrines", "items__overrides"):
        for doctrine in fit.doctrines.all():
            assignment, _ = FitAssignment.objects.get_or_create(
                doctrine=doctrine, fit=fit
            )
            for item in fit.items.all():
                policy, _ = AssignmentItemPolicy.objects.get_or_create(
                    assignment=assignment,
                    source_item=item,
                    defaults={
                        "section": item.section,
                        "module_type_id": item.module_type_id,
                        "quantity": item.quantity,
                        "charge_type_id": item.charge_type_id,
                        "policy": item.policy,
                        "min_meta_level": item.min_meta_level,
                        "allowed_meta_groups": item.allowed_meta_groups,
                        "checked_attributes": item.checked_attributes,
                        "allow_mutated": item.allow_mutated,
                        "min_quantity_pct": item.min_quantity_pct,
                        "notes": item.notes,
                    },
                )
                for override in item.overrides.all():
                    AssignmentItemOverride.objects.get_or_create(
                        assignment_item=policy,
                        alt_type_id=override.alt_type_id,
                        defaults={"mode": override.mode},
                    )


def drop_assignments(apps, schema_editor):
    FitAssignment = apps.get_model("fitcheck", "FitAssignment")
    FitAssignment.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("fitcheck", "0010_fit_assignments"),
    ]

    operations = [
        migrations.RunPython(backfill_assignments, drop_assignments),
    ]
