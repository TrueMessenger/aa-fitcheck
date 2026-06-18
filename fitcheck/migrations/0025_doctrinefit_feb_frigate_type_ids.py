from django.db import migrations, models


def forward(apps, schema_editor):
    """Carry the single feb_frigate_type_id into the new list field."""
    DoctrineFit = apps.get_model("fitcheck", "DoctrineFit")
    for fit in DoctrineFit.objects.exclude(feb_frigate_type_id__isnull=True):
        fit.feb_frigate_type_ids = [fit.feb_frigate_type_id]
        fit.save(update_fields=["feb_frigate_type_ids"])


def backward(apps, schema_editor):
    """Restore the first accepted frigate into the single field."""
    DoctrineFit = apps.get_model("fitcheck", "DoctrineFit")
    for fit in DoctrineFit.objects.all():
        ids = fit.feb_frigate_type_ids or []
        fit.feb_frigate_type_id = ids[0] if ids else None
        fit.save(update_fields=["feb_frigate_type_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("fitcheck", "0024_substitution_policy_labels"),
    ]

    operations = [
        migrations.AddField(
            model_name="doctrinefit",
            name="feb_frigate_type_ids",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(forward, backward),
        migrations.RemoveField(
            model_name="doctrinefit",
            name="feb_frigate_type_id",
        ),
    ]
