"""Category-driven visibility: the Selected (OR) / Required (AND) group rules,
combined with OR, plus the uncategorized=public default and manager bypass."""

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from ..managers import visible_categories_among, visible_categories_for
from ..models import Doctrine, DoctrineCategory, DoctrineFit
from .testdata.factories import create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


def _gids(user):
    return set(user.groups.values_list("id", flat=True))


class CategoryAdmitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ga = Group.objects.create(name="A")
        cls.gb = Group.objects.create(name="B")
        cls.admin = Group.objects.create(name="Admins")

    def _user(self, *groups, name="m"):
        user = create_user(name)
        for g in groups:
            user.groups.add(g)
        return user

    def test_public_category_admits_all(self):
        cat = DoctrineCategory.objects.create(name="Pub")
        self.assertTrue(cat.admits(_gids(self._user())))

    def test_selected_is_or(self):
        cat = DoctrineCategory.objects.create(name="Sel")
        cat.selected_groups.add(self.ga)
        self.assertTrue(cat.admits(_gids(self._user(self.ga, name="a"))))
        self.assertFalse(cat.admits(_gids(self._user(self.gb, name="b"))))

    def test_required_is_all(self):
        cat = DoctrineCategory.objects.create(name="Req")
        cat.required_groups.add(self.ga, self.gb)
        self.assertTrue(cat.admits(_gids(self._user(self.ga, self.gb, name="ab"))))
        self.assertFalse(cat.admits(_gids(self._user(self.ga, name="aonly"))))

    def test_selected_or_required_combination(self):
        cat = DoctrineCategory.objects.create(name="Combo")
        cat.selected_groups.add(self.ga, self.gb, self.admin)
        cat.required_groups.add(self.ga, self.gb)
        # Admins only -> matches a Selected group (OR), admitted.
        self.assertTrue(cat.admits(_gids(self._user(self.admin, name="adm"))))
        # A+B -> has all Required, admitted.
        self.assertTrue(cat.admits(_gids(self._user(self.ga, self.gb, name="ab2"))))
        # Unrelated group -> neither path, denied.
        gx = Group.objects.create(name="X")
        self.assertFalse(cat.admits(_gids(self._user(gx, name="x"))))


class VisibilityQuerysetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def _gated_doctrine(self, group):
        cat = DoctrineCategory.objects.create(name="Cap")
        cat.selected_groups.add(group)
        doctrine = create_doctrine(name="Gated")
        doctrine.categories.add(cat)
        return doctrine

    def test_uncategorized_is_public(self):
        doctrine = create_doctrine(name="Open")
        fit = create_fit(None, T.ORACLE, name="Free")
        member = create_user("mem")
        self.assertIn(doctrine, Doctrine.objects.visible_to(member))
        self.assertIn(fit, DoctrineFit.objects.visible_to(member))

    def test_gated_doctrine_hidden_then_shown(self):
        group = Group.objects.create(name="Caps")
        doctrine = self._gated_doctrine(group)
        member = create_user("mem2")
        self.assertNotIn(doctrine, Doctrine.objects.visible_to(member))
        member.groups.add(group)
        self.assertIn(doctrine, Doctrine.objects.visible_to(member))

    def test_fit_gated_via_its_doctrine_category(self):
        group = Group.objects.create(name="Caps")
        doctrine = self._gated_doctrine(group)
        fit = create_fit(doctrine, T.ORACLE, name="GatedFit")  # no direct category
        member = create_user("mem3")
        self.assertNotIn(fit, DoctrineFit.objects.visible_to(member))
        member.groups.add(group)
        self.assertIn(fit, DoctrineFit.objects.visible_to(member))

    def test_manager_bypass_sees_gated(self):
        group = Group.objects.create(name="Caps")
        doctrine = self._gated_doctrine(group)
        fit = create_fit(doctrine, T.ORACLE, name="GatedFit")
        mgr = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.assertIn(doctrine, Doctrine.objects.visible_to(mgr))
        self.assertIn(fit, DoctrineFit.objects.visible_to(mgr))

    def test_multi_category_or(self):
        g1 = Group.objects.create(name="G1")
        g2 = Group.objects.create(name="G2")
        c1 = DoctrineCategory.objects.create(name="C1")
        c1.selected_groups.add(g1)
        c2 = DoctrineCategory.objects.create(name="C2")
        c2.selected_groups.add(g2)
        doctrine = create_doctrine(name="Multi")
        doctrine.categories.add(c1, c2)
        member = create_user("mem4")
        member.groups.add(g2)
        # Admitted via c2 even though c1 would deny.
        self.assertIn(doctrine, Doctrine.objects.visible_to(member))


class CategoryManagementTests(TestCase):
    """The standalone category create/edit/delete management UI."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.mgr = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.client.force_login(self.mgr)
        self.doctrine = create_doctrine(name="Caps")
        self.fit = create_fit(None, T.ORACLE, name="Cyno Alt")
        self.group = Group.objects.create(name="Compliant")

    def test_list_and_create_pages_render(self):
        from django.urls import reverse
        self.assertEqual(self.client.get(reverse("fitcheck:category_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("fitcheck:category_create")).status_code, 200)

    def test_create_links_groups_fits_and_doctrines(self):
        from django.urls import reverse
        resp = self.client.post(
            reverse("fitcheck:category_create"),
            {
                "name": "Anti-Cap Dread",
                "color": "#dc3545",
                "selected_groups": [self.group.pk],
                "required_groups": [],
                "fits": [self.fit.pk],
                "doctrines": [self.doctrine.pk],
            },
        )
        self.assertEqual(resp.status_code, 302)
        cat = DoctrineCategory.objects.get(name="Anti-Cap Dread")
        self.assertEqual(list(cat.selected_groups.all()), [self.group])
        self.assertIn(self.fit, cat.fits.all())
        self.assertIn(self.doctrine, cat.doctrines.all())  # reverse M2M set in the view

    def test_delete(self):
        from django.urls import reverse
        cat = DoctrineCategory.objects.create(name="Temp")
        resp = self.client.post(reverse("fitcheck:category_delete", args=[cat.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(DoctrineCategory.objects.filter(pk=cat.pk).exists())


class VisibleCategoriesForTests(TestCase):
    """`visible_categories_for` backs the Doctrines page filter chips: a
    category must both admit the user and carry something they can currently
    see, or a manager."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_admitted_category_with_visible_doctrine_is_shown(self):
        group = Group.objects.create(name="ChipAdmitted")
        cat = DoctrineCategory.objects.create(name="Chip Admitted")
        cat.selected_groups.add(group)
        doctrine = create_doctrine(name="Chip Doctrine A")
        doctrine.categories.add(cat)
        member = create_user("chiphelper1")
        member.groups.add(group)
        self.assertIn(cat, visible_categories_for(member))

    def test_restricted_category_is_hidden(self):
        group = Group.objects.create(name="ChipRestricted")
        cat = DoctrineCategory.objects.create(name="Chip Restricted")
        cat.selected_groups.add(group)
        doctrine = create_doctrine(name="Chip Doctrine B")
        doctrine.categories.add(cat)
        member = create_user("chiphelper2")
        self.assertNotIn(cat, visible_categories_for(member))

    def test_admitted_but_empty_category_is_hidden(self):
        group = Group.objects.create(name="ChipEmptyGroup")
        cat = DoctrineCategory.objects.create(name="Chip Empty")
        cat.selected_groups.add(group)
        member = create_user("chiphelper3")
        member.groups.add(group)
        self.assertNotIn(cat, visible_categories_for(member))
        # Still hidden with only an inactive doctrine attached.
        doctrine = create_doctrine(name="Chip Doctrine C", is_active=False)
        doctrine.categories.add(cat)
        self.assertNotIn(cat, visible_categories_for(member))

    def test_public_category_with_visible_doctrine_is_shown(self):
        cat = DoctrineCategory.objects.create(name="Chip Public")
        doctrine = create_doctrine(name="Chip Doctrine D")
        doctrine.categories.add(cat)
        member = create_user("chiphelper4")
        self.assertIn(cat, visible_categories_for(member))

    def test_manager_sees_restricted_and_empty_categories_too(self):
        group = Group.objects.create(name="ChipMgrGroup")
        restricted = DoctrineCategory.objects.create(name="Chip Mgr Restricted")
        restricted.selected_groups.add(group)
        doctrine = create_doctrine(name="Chip Doctrine E")
        doctrine.categories.add(restricted)
        empty = DoctrineCategory.objects.create(name="Chip Mgr Empty")
        mgr = create_user("chiphelpermgr", permissions=("basic_access", "manage_doctrines"))
        result = visible_categories_for(mgr)
        self.assertIn(restricted, result)
        self.assertIn(empty, result)


class IndexChipRenderingTests(TestCase):
    """The Doctrines page chip bar itself: same rules as
    `visible_categories_for`, asserted against the rendered response."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        self.group = Group.objects.create(name="IndexChipGroup")
        self.other_group = Group.objects.create(name="IndexChipOtherGroup")

        self.admitted_cat = DoctrineCategory.objects.create(name="Index Chip Admitted")
        self.admitted_cat.selected_groups.add(self.group)
        doctrine = create_doctrine(name="Index Chip Doctrine")
        doctrine.categories.add(self.admitted_cat)
        create_fit(doctrine, T.ORACLE, name="Index Chip Fit")

        self.restricted_cat = DoctrineCategory.objects.create(name="Index Chip Restricted")
        self.restricted_cat.selected_groups.add(self.other_group)
        restricted_doctrine = create_doctrine(name="Index Restricted Doctrine")
        restricted_doctrine.categories.add(self.restricted_cat)
        create_fit(restricted_doctrine, T.ORACLE, name="Index Restricted Fit")

        self.empty_cat = DoctrineCategory.objects.create(name="Index Chip Empty")
        self.empty_cat.selected_groups.add(self.group)

        self.public_cat = DoctrineCategory.objects.create(name="Index Chip Public")
        public_doctrine = create_doctrine(name="Index Public Doctrine")
        public_doctrine.categories.add(self.public_cat)
        create_fit(public_doctrine, T.ORACLE, name="Index Public Fit")

        self.member = create_user("indexchipmem")
        self.member.groups.add(self.group)
        self.manager = create_user(
            "indexchipmgr", permissions=("basic_access", "manage_doctrines")
        )

    def test_member_sees_only_admitted_nonempty_and_public_chips(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("fitcheck:index"))
        self.assertContains(response, "Index Chip Admitted")
        self.assertContains(response, "Index Chip Public")
        self.assertNotContains(response, "Index Chip Restricted")
        self.assertNotContains(response, "Index Chip Empty")

    def test_manager_sees_every_chip(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("fitcheck:index"))
        for name in (
            "Index Chip Admitted",
            "Index Chip Restricted",
            "Index Chip Empty",
            "Index Chip Public",
        ):
            self.assertContains(response, name)


class CategoryBadgeLeakTests(TestCase):
    """A doctrine/fit that's visible via one admitted category (OR across
    categories) must not leak the name of another category it also carries
    that this viewer isn't admitted to - on the index cards, the doctrine
    detail page, or the fit detail page."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        self.admitted_group = Group.objects.create(name="BadgeAdmittedGroup")
        self.restricted_group = Group.objects.create(name="BadgeRestrictedGroup")
        self.admitted_cat = DoctrineCategory.objects.create(name="Badge Admitted Cat")
        self.admitted_cat.selected_groups.add(self.admitted_group)
        self.restricted_cat = DoctrineCategory.objects.create(name="Badge Restricted Cat")
        self.restricted_cat.selected_groups.add(self.restricted_group)

        self.doctrine = create_doctrine(name="Badge Doctrine")
        self.doctrine.categories.add(self.admitted_cat, self.restricted_cat)
        self.fit = create_fit(self.doctrine, T.ORACLE, name="Badge Fit")

        self.member = create_user("badgemem")
        self.member.groups.add(self.admitted_group)
        self.manager = create_user("badgemgr", permissions=("basic_access", "manage_doctrines"))

    def test_visible_categories_among_filters_to_admitted(self):
        categories = list(self.doctrine.categories.all())
        self.assertEqual(
            visible_categories_among(self.member, categories), [self.admitted_cat]
        )
        self.assertEqual(
            set(visible_categories_among(self.manager, categories)),
            {self.admitted_cat, self.restricted_cat},
        )

    def test_index_card_hides_restricted_category_badge(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("fitcheck:index"))
        self.assertContains(response, "Badge Admitted Cat")
        self.assertNotContains(response, "Badge Restricted Cat")

    def test_doctrine_detail_hides_restricted_category_badge(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:doctrine_detail", args=[self.doctrine.pk])
        )
        self.assertContains(response, "Badge Admitted Cat")
        self.assertNotContains(response, "Badge Restricted Cat")

    def test_fit_detail_hides_restricted_category_badge(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("fitcheck:fit_detail", args=[self.fit.pk]))
        self.assertContains(response, "Badge Admitted Cat")
        self.assertNotContains(response, "Badge Restricted Cat")

    def test_manager_sees_both_badges_everywhere(self):
        self.client.force_login(self.manager)
        for url in (
            reverse("fitcheck:index"),
            reverse("fitcheck:doctrine_detail", args=[self.doctrine.pk]),
            reverse("fitcheck:fit_detail", args=[self.fit.pk]),
        ):
            response = self.client.get(url)
            self.assertContains(response, "Badge Admitted Cat")
            self.assertContains(response, "Badge Restricted Cat")
