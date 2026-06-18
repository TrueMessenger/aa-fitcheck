import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

# Bootstrap semantic keyword -> the hex value Bootstrap 5 renders for bg-<keyword>.
STYLE_TO_HEX = {
    "primary": "#0d6efd",
    "info": "#0dcaf0",
    "success": "#198754",
    "warning": "#ffc107",
    "danger": "#dc3545",
    "secondary": "#6c757d",
    "dark": "#212529",
}


def style_to_color(apps, schema_editor):
    DoctrineTag = apps.get_model("fitcheck", "DoctrineTag")
    for tag in DoctrineTag.objects.all():
        tag.color = STYLE_TO_HEX.get(tag.style, "#0d6efd")
        tag.save(update_fields=["color"])


def color_to_style(apps, schema_editor):
    DoctrineTag = apps.get_model("fitcheck", "DoctrineTag")
    hex_to_style = {v: k for k, v in STYLE_TO_HEX.items()}
    for tag in DoctrineTag.objects.all():
        tag.style = hex_to_style.get(tag.color, "primary")
        tag.save(update_fields=["style"])


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("fitcheck", "0016_assignmentitempolicy_attribute_bounds_and_more"),
    ]

    operations = [
        # --- DoctrineTag: bootstrap keyword -> arbitrary hex colour ---
        migrations.AddField(
            model_name="doctrinetag",
            name="color",
            field=models.CharField(
                default="#0d6efd",
                help_text="Background colour as a #rrggbb hex value.",
                max_length=7,
            ),
        ),
        migrations.RunPython(style_to_color, color_to_style),
        migrations.RemoveField(model_name="doctrinetag", name="style"),
        # --- DoctrineFit: BOM-only timestamp ---
        migrations.AddField(
            model_name="doctrinefit",
            name="bom_updated_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the fit's module list (BOM) last changed.",
                null=True,
            ),
        ),
        # --- ArchivedFitVersion ---
        migrations.CreateModel(
            name="ArchivedFitVersion",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("version", models.PositiveIntegerField()),
                ("eft_source", models.TextField()),
                ("ship_type_id", models.PositiveIntegerField()),
                ("policy_snapshot", models.JSONField(blank=True, default=dict)),
                ("archived_at", models.DateTimeField(auto_now_add=True)),
                (
                    "archived_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "fit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="archives",
                        to="fitcheck.doctrinefit",
                    ),
                ),
            ],
            options={
                "ordering": ["-version"],
                "default_permissions": (),
            },
        ),
        migrations.AddConstraint(
            model_name="archivedfitversion",
            constraint=models.UniqueConstraint(
                fields=("fit", "version"), name="fitcheck_unique_fit_archive"
            ),
        ),
    ]
