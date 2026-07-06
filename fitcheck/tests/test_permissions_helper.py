from django.contrib.auth.models import Group, Permission
from django.test import TestCase

from allianceauth.authentication.models import State

from fitcheck.services.permissions import users_with_permission

from .testdata.factories import create_user


def _basic_access() -> Permission:
    return Permission.objects.get(
        content_type__app_label="fitcheck", codename="basic_access"
    )


class TestUsersWithPermission(TestCase):
    def test_direct_permission_included(self):
        user = create_user("direct")  # factory grants basic_access directly
        self.assertIn(user, users_with_permission(_basic_access()))

    def test_group_permission_included(self):
        user = create_user("via-group", permissions=())
        group = Group.objects.create(name="Members")
        group.permissions.add(_basic_access())
        user.groups.add(group)
        self.assertIn(user, users_with_permission(_basic_access()))

    def test_state_permission_included(self):
        # The AA 5.2 regression arm: State.permissions targets AA's Permission
        # proxy there, so reverse-accessor (`state_set`) lookups break while
        # this forward lookup must keep working.
        user = create_user("via-state", permissions=())
        state = State.objects.create(name="Member State", priority=200)
        # AA 5.2's State.permissions M2M only accepts its own Permission proxy
        # instances; resolve the target model so this runs on 5.1 too.
        perm_model = State._meta.get_field("permissions").related_model
        state.permissions.add(perm_model.objects.get(pk=_basic_access().pk))
        # Write the FK directly: profile.save() triggers AA's state
        # reassessment, which would bounce the user back to Guest.
        type(user.profile).objects.filter(pk=user.profile.pk).update(state=state)
        self.assertIn(user, users_with_permission(_basic_access()))

    def test_user_without_permission_excluded(self):
        user = create_user("nobody", permissions=())
        self.assertNotIn(user, users_with_permission(_basic_access()))

    def test_superuser_toggle(self):
        user = create_user("root", permissions=())
        user.is_superuser = True
        user.save()
        self.assertIn(user, users_with_permission(_basic_access()))
        self.assertNotIn(
            user, users_with_permission(_basic_access(), include_superusers=False)
        )

    def test_no_duplicates_for_multi_path_holder(self):
        user = create_user("everything")  # direct grant
        group = Group.objects.create(name="Also Members")
        group.permissions.add(_basic_access())
        user.groups.add(group)
        matches = [u for u in users_with_permission(_basic_access()) if u == user]
        self.assertEqual(len(matches), 1)
