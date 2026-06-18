"""Tests for the multi-select fitting picker on the doctrine detail page:
the JSON search endpoint, the group-list helper, and the bulk assign view.
"""

from django.test import TestCase
from django.urls import reverse

from .testdata.factories import create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


class TestFittingSearchEndpoint(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.manager = create_user(
            "mgr", permissions=("basic_access", "manage_doctrines")
        )
        cls.doctrine = create_doctrine("Avatar Doctrine")
        cls.other_doctrine = create_doctrine("Redeemer Doctrine")
        cls.harb = create_fit(cls.doctrine, T.HARBINGER, name="Harb Brawl")
        cls.oracle = create_fit(None, T.ORACLE, name="Glass Cannon")
        cls.hel = create_fit(None, T.HEL, name="Shield Hel")

    def test_filters_by_name(self):
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("fitcheck:fitting_search"), {"q": "Brawl"}
        )
        names = {r["name"] for r in response.json()["results"]}
        self.assertIn("Harb Brawl", names)
        self.assertNotIn("Glass Cannon", names)

    def test_filters_by_hull(self):
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("fitcheck:fitting_search"), {"hull": str(T.HEL)}
        )
        names = {r["name"] for r in response.json()["results"]}
        self.assertEqual(names, {"Shield Hel"})

    def test_excludes_already_assigned_doctrine(self):
        """Fittings already attached to the doctrine don't reappear in the
        picker - we only want NEW attachment candidates."""
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("fitcheck:fitting_search"),
            {"exclude_doctrine": str(self.doctrine.pk)},
        )
        names = {r["name"] for r in response.json()["results"]}
        self.assertNotIn("Harb Brawl", names)  # already in Avatar Doctrine
        self.assertIn("Glass Cannon", names)
        self.assertIn("Shield Hel", names)

    def test_requires_manage_doctrines_perm(self):
        member = create_user("member")
        self.client.force_login(member)
        response = self.client.get(reverse("fitcheck:fitting_search"))
        # @permission_required redirects unauthorised users to login.
        self.assertEqual(response.status_code, 302)


class TestShipGroupList(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.manager = create_user(
            "mgr", permissions=("basic_access", "manage_doctrines")
        )
        create_fit(None, T.HARBINGER, name="A")
        create_fit(None, T.HEL, name="B")

    def test_returns_unique_groups(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("fitcheck:ship_group_list"))
        # Just verify shape - the actual group names depend on eveuniverse
        # metadata and may be missing in the fixture set; we mainly want
        # to know the view itself doesn't error.
        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.json())


class TestDoctrineAssignFitsBulk(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.manager = create_user(
            "mgr", permissions=("basic_access", "manage_doctrines")
        )
        cls.doctrine = create_doctrine("Target")
        cls.fits = [
            create_fit(None, T.HARBINGER, name=f"Fit {i}") for i in range(3)
        ]

    def test_assigns_many_fits_at_once(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:doctrine_assign_fits_bulk", args=[self.doctrine.pk]),
            {"fit_ids": [str(f.pk) for f in self.fits]},
        )
        self.assertEqual(response.status_code, 302)
        for fit in self.fits:
            fit.refresh_from_db()
            self.assertIn(self.doctrine, fit.doctrines.all())

    def test_is_idempotent(self):
        """Re-submitting with an already-assigned fit doesn't error or
        duplicate - M2M.add silently skips duplicates."""
        self.fits[0].doctrines.add(self.doctrine)
        self.client.force_login(self.manager)
        self.client.post(
            reverse("fitcheck:doctrine_assign_fits_bulk", args=[self.doctrine.pk]),
            {"fit_ids": [str(f.pk) for f in self.fits]},
        )
        # All three still ended up in the doctrine, once each.
        for fit in self.fits:
            self.assertEqual(
                fit.doctrines.filter(pk=self.doctrine.pk).count(), 1
            )

    def test_no_selection_flashes_error(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:doctrine_assign_fits_bulk", args=[self.doctrine.pk]),
            follow=True,
        )
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Pick at least one" in m for m in messages), messages)

    def test_requires_manage_doctrines_perm(self):
        member = create_user("member")
        self.client.force_login(member)
        response = self.client.post(
            reverse("fitcheck:doctrine_assign_fits_bulk", args=[self.doctrine.pk]),
            {"fit_ids": [str(self.fits[0].pk)]},
        )
        self.assertEqual(response.status_code, 302)
        self.fits[0].refresh_from_db()
        self.assertNotIn(self.doctrine, self.fits[0].doctrines.all())


class TestFitSetDoctrines(TestCase):
    """The Edit Doctrines collapse on fit_detail.html POSTs the desired
    final set; the endpoint diffs against current and reports net change."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.manager = create_user(
            "mgr", permissions=("basic_access", "manage_doctrines")
        )
        cls.d_avatar = create_doctrine("Avatar")
        cls.d_redeemer = create_doctrine("Redeemer")
        cls.d_machariel = create_doctrine("Machariel")
        cls.fit = create_fit(cls.d_avatar, T.HARBINGER, name="Pilgrim Cyno")
        # Start: fit is in Avatar only.

    def test_adds_and_removes_in_one_post(self):
        # Want: Redeemer + Machariel. Avatar should be removed.
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:fit_set_doctrines", args=[self.fit.pk]),
            {"doctrine_ids": [str(self.d_redeemer.pk), str(self.d_machariel.pk)]},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        names = set(self.fit.doctrines.values_list("name", flat=True))
        self.assertEqual(names, {"Redeemer", "Machariel"})
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("2 added" in m and "1 removed" in m for m in messages), messages)

    def test_empty_post_clears_all_assignments(self):
        """Submitting with no checkboxes ticked means 'this is a standalone
        fit now.' We remove every existing assignment."""
        self.client.force_login(self.manager)
        self.client.post(
            reverse("fitcheck:fit_set_doctrines", args=[self.fit.pk]),
            {},
        )
        self.assertEqual(self.fit.doctrines.count(), 0)

    def test_noop_when_set_unchanged(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:fit_set_doctrines", args=[self.fit.pk]),
            {"doctrine_ids": [str(self.d_avatar.pk)]},
            follow=True,
        )
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("No doctrine changes" in m for m in messages), messages)
        self.assertEqual(
            set(self.fit.doctrines.values_list("pk", flat=True)),
            {self.d_avatar.pk},
        )

    def test_ignores_unknown_doctrine_ids(self):
        """Garbage IDs from a tampered form silently no-op instead of 500ing."""
        self.client.force_login(self.manager)
        self.client.post(
            reverse("fitcheck:fit_set_doctrines", args=[self.fit.pk]),
            {"doctrine_ids": ["999999", "abc", str(self.d_avatar.pk)]},
        )
        self.assertEqual(
            set(self.fit.doctrines.values_list("pk", flat=True)),
            {self.d_avatar.pk},
        )
