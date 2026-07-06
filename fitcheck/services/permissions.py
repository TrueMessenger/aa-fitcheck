"""Permission → user resolution."""

from django.contrib.auth.models import Permission, User
from django.db.models import Q, QuerySet


def users_with_permission(
    permission: Permission, include_superusers: bool = True
) -> QuerySet:
    """All users holding ``permission`` directly, via a group, or via their
    Alliance Auth state.

    Local replacement for ``app_utils.django.users_with_permission``: since
    Alliance Auth 5.2, ``State.permissions`` targets AA's ``Permission``
    *proxy* model, so the reverse ``state_set`` accessor app-utils relies on
    no longer exists on ``django.contrib.auth`` ``Permission`` instances.
    Forward pk lookups don't depend on where Django attaches the reverse
    descriptor, so they work on both sides of that change.
    """
    query = (
        Q(user_permissions__pk=permission.pk)
        | Q(groups__permissions__pk=permission.pk)
        | Q(profile__state__permissions__pk=permission.pk)
    )
    if include_superusers:
        query |= Q(is_superuser=True)
    return User.objects.filter(query).distinct()
