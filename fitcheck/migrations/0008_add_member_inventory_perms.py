"""Add the two permissions that gate the proactive member-inventory feature.

The auth post-migrate signal normally creates `Meta.permissions` rows
automatically, but only when the app reports something has changed. For an
unmanaged model (General) the signal can skip this run, so we make creation
explicit and idempotent here."""

from django.db import migrations


_PERMS = [
    (
        "view_member_inventory",
        "Can browse alliance members' ships and run proactive fit checks",
    ),
    (
        "view_own_corp_inventory",
        "Can browse own-corporation members' ships and run proactive fit "
        "checks (scoped to the user's main character's corporation)",
    ),
]


def add_perms(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    ct, _ = ContentType.objects.get_or_create(app_label="fitcheck", model="general")
    for codename, name in _PERMS:
        Permission.objects.get_or_create(
            content_type=ct, codename=codename, defaults={"name": name}
        )


def remove_perms(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    Permission.objects.filter(
        content_type__app_label="fitcheck",
        codename__in=[c for c, _ in _PERMS],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("fitcheck", "0007_fitsubmission_frigate_escape_bay_type_id"),
    ]

    operations = [
        migrations.RunPython(add_perms, remove_perms),
    ]
