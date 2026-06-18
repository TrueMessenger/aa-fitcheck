"""Standalone fittings (Doctrine<->Fit becomes M2M), doctrine tags + images,
compliance policies with slot-group rules, and new permissions."""

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def copy_doctrine_links(apps, schema_editor):
    DoctrineFit = apps.get_model("fitcheck", "DoctrineFit")
    for fit in DoctrineFit.objects.exclude(doctrine__isnull=True):
        fit.doctrines.add(fit.doctrine_id)


def restore_doctrine_links(apps, schema_editor):
    DoctrineFit = apps.get_model("fitcheck", "DoctrineFit")
    for fit in DoctrineFit.objects.all():
        first = fit.doctrines.first()
        if first:
            fit.doctrine_id = first.pk
            fit.save(update_fields=["doctrine"])


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("fitcheck", "0001_initial"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="general",
            options={
                "default_permissions": (),
                "managed": False,
                "permissions": (
                    ("basic_access", "Can access Fit Check"),
                    ("manage_doctrines", "Can manage doctrines and fitting standards"),
                    ("review_submissions", "Can review fit submissions"),
                    (
                        "secure_group_management",
                        "Secure Group Doctrine Management - can view and decide on "
                        "submitted ships but cannot create doctrines or change fitting "
                        "standards",
                    ),
                    ("manage_policies", "Plugin admin - can manage compliance policies"),
                    ("view_compliance_reports", "Can view org-wide compliance reports"),
                ),
            },
        ),
        migrations.CreateModel(
            name="DoctrineTag",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=30, unique=True)),
                (
                    "style",
                    models.CharField(
                        choices=[
                            ("primary", "Blue"),
                            ("info", "Teal"),
                            ("success", "Green"),
                            ("warning", "Yellow"),
                            ("danger", "Red"),
                            ("secondary", "Grey"),
                            ("dark", "Dark"),
                        ],
                        default="primary",
                        max_length=10,
                    ),
                ),
            ],
            options={"default_permissions": (), "ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="CompliancePolicy",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
                ("description", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "default_permissions": (),
                "ordering": ["name"],
                "verbose_name_plural": "compliance policies",
            },
        ),
        migrations.CreateModel(
            name="PolicySlotRule",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "section",
                    models.CharField(
                        choices=[
                            ("HIGH", "High slots"),
                            ("MED", "Mid slots"),
                            ("LOW", "Low slots"),
                            ("RIG", "Rigs"),
                            ("SUBSYS", "Subsystems"),
                            ("DRONE", "Drone bay"),
                            ("FIGHTER", "Fighter bay"),
                            ("CARGO", "Cargo"),
                            ("IMPLANT", "Implants"),
                        ],
                        max_length=8,
                    ),
                ),
                (
                    "enforcement",
                    models.CharField(
                        choices=[
                            ("EX", "Exact fit"),
                            ("ME", "Meta level enforcement / exception"),
                            ("GE", "Equal to or greater"),
                            ("AN", "Any (no enforcement)"),
                        ],
                        default="ME",
                        max_length=2,
                    ),
                ),
                (
                    "min_meta_level",
                    models.PositiveSmallIntegerField(
                        blank=True,
                        help_text="Meta enforcement: substitutes need at least this meta level. Empty = each module's own level.",
                        null=True,
                    ),
                ),
                (
                    "allow_mutated",
                    models.BooleanField(
                        default=True,
                        help_text="Equal-or-greater: allow abyssal/mutated modules whose rolls qualify.",
                    ),
                ),
                (
                    "min_quantity_pct",
                    models.PositiveSmallIntegerField(
                        default=100,
                        help_text="Consumable sections: pass at this percent of the listed quantity.",
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(100),
                        ],
                    ),
                ),
                (
                    "policy",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rules",
                        to="fitcheck.compliancepolicy",
                    ),
                ),
            ],
            options={"default_permissions": ()},
        ),
        migrations.AddConstraint(
            model_name="policyslotrule",
            constraint=models.UniqueConstraint(
                fields=("policy", "section"), name="fitcheck_unique_policy_rule"
            ),
        ),
        migrations.AddField(
            model_name="doctrine",
            name="image_type_id",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="EVE ship type whose render illustrates this doctrine.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="doctrine",
            name="tags",
            field=models.ManyToManyField(blank=True, related_name="doctrines", to="fitcheck.doctrinetag"),
        ),
        migrations.AlterModelOptions(
            name="doctrinefit",
            options={"default_permissions": (), "ordering": ["name"]},
        ),
        migrations.RemoveConstraint(
            model_name="doctrinefit",
            name="fitcheck_unique_fit_name",
        ),
        # Free the reverse accessor "fits" before the M2M claims it.
        migrations.AlterField(
            model_name="doctrinefit",
            name="doctrine",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="fitcheck.doctrine",
            ),
        ),
        migrations.AddField(
            model_name="doctrinefit",
            name="doctrines",
            field=models.ManyToManyField(
                blank=True,
                help_text="Doctrines this fitting belongs to. A fitting may stand alone.",
                related_name="fits",
                to="fitcheck.doctrine",
            ),
        ),
        migrations.AddField(
            model_name="doctrinefit",
            name="compliance_policy",
            field=models.ForeignKey(
                blank=True,
                help_text="The slot-group policy last applied to this fitting, if any.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fits",
                to="fitcheck.compliancepolicy",
            ),
        ),
        migrations.RunPython(copy_doctrine_links, restore_doctrine_links),
        migrations.RemoveField(
            model_name="doctrinefit",
            name="doctrine",
        ),
        migrations.AlterField(
            model_name="doctrinefit",
            name="default_policy",
            field=models.CharField(
                choices=[
                    ("EX", "Exact type only"),
                    ("VA", "Variant family (meta filtered)"),
                    ("MB", "Any equivalent that meets or beats attributes"),
                    ("AN", "No enforcement (anything accepted)"),
                ],
                default="VA",
                max_length=2,
            ),
        ),
        migrations.AlterField(
            model_name="doctrinefititem",
            name="policy",
            field=models.CharField(
                choices=[
                    ("EX", "Exact type only"),
                    ("VA", "Variant family (meta filtered)"),
                    ("MB", "Any equivalent that meets or beats attributes"),
                    ("AN", "No enforcement (anything accepted)"),
                ],
                max_length=2,
            ),
        ),
    ]
