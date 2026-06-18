"""Backfill allowed_meta_groups for the reversed meta-group semantics.

The field used to mean "empty = all groups allowed"; it now means "checked =
allowed, empty = none". To preserve existing behavior, populate any row with an
empty list to its policy-appropriate default: Exact -> the module's own meta
group (cosmetic; the engine ignores meta groups under Exact); everything else ->
all six offered groups. Rows that already carry a non-empty list keep their
(now-identical) meaning and are left untouched.
"""

from django.db import migrations

ALL_GROUPS = [1, 2, 3, 4, 5, 6]  # Tech I, Tech II, Storyline, Faction, Officer, Deadspace
EXACT = "EX"


def backfill(apps, schema_editor):
    SdeType = apps.get_model("fitcheck", "SdeType")
    meta = dict(SdeType.objects.values_list("type_id", "meta_group_id"))
    for model_name in ("DoctrineFitItem", "AssignmentItemPolicy"):
        Model = apps.get_model("fitcheck", model_name)
        for item in Model.objects.all():
            if item.allowed_meta_groups:
                continue  # non-empty: meaning is unchanged by the reversal
            if item.policy == EXACT:
                own = meta.get(item.module_type_id)
                item.allowed_meta_groups = [own] if own else []
            else:
                item.allowed_meta_groups = list(ALL_GROUPS)
            item.save(update_fields=["allowed_meta_groups"])


class Migration(migrations.Migration):

    dependencies = [
        ("fitcheck", "0012_fitsubmission_doctrine_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
