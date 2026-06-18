"""Category-driven visibility: the Selected (OR) / Required (AND) group rules,
combined with OR, plus the uncategorized=public default and manager bypass."""

from django.contrib.auth.models import Group
from django.test import TestCase

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
