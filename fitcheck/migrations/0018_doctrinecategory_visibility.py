from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "0001_initial"),
        ("fitcheck", "0017_tag_color_fit_archive"),
    ]

    operations = [
        # --- DoctrineTag becomes DoctrineCategory (data-preserving rename) ---
        migrations.RenameModel(old_name="DoctrineTag", new_name="DoctrineCategory"),
        migrations.AlterModelOptions(
            name="doctrinecategory",
            options={
                "default_permissions": (),
                "ordering": ["name"],
                "verbose_name_plural": "doctrine categories",
            },
        ),
        migrations.RenameField(model_name="doctrine", old_name="tags", new_name="categories"),
        migrations.AlterField(
            model_name="doctrine",
            name="categories",
            field=models.ManyToManyField(
                blank=True,
                help_text="Categories this doctrine belongs to; their groups gate visibility.",
                related_name="doctrines",
                to="fitcheck.doctrinecategory",
            ),
        ),
        # --- Category becomes a visibility object ---
        migrations.AddField(
            model_name="doctrinecategory",
            name="selected_groups",
            field=models.ManyToManyField(
                blank=True,
                help_text="Pilots in ANY of these Auth groups may see this category's fits/doctrines.",
                related_name="+",
                to="auth.group",
            ),
        ),
        migrations.AddField(
            model_name="doctrinecategory",
            name="required_groups",
            field=models.ManyToManyField(
                blank=True,
                help_text="Pilots must have ALL of these Auth groups to see this category's fits/doctrines.",
                related_name="+",
                to="auth.group",
            ),
        ),
        migrations.AddField(
            model_name="doctrinecategory",
            name="fits",
            field=models.ManyToManyField(
                blank=True,
                help_text="Fittings in this category (gated by the groups above).",
                related_name="categories",
                to="fitcheck.doctrinefit",
            ),
        ),
        # --- Visibility moves off the Doctrine onto Categories ---
        migrations.RemoveField(model_name="doctrine", name="groups"),
        migrations.RemoveField(model_name="doctrine", name="states"),
    ]
