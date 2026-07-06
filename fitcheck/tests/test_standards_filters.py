from django.test import TestCase
from django.urls import reverse

from ..models import DoctrineCategory
from .testdata.factories import create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


class StandardsFiltersTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.manager = create_user("manager", permissions=["basic_access", "manage_doctrines"])
        cls.doctrine_a = create_doctrine(name="Alliance Armor")
        cls.doctrine_b = create_doctrine(name="Shield Fleet")
        # Distinct hull groups so group filtering/sorting has something to
        # discriminate on: Harbinger/Oracle/Hel all share the default "ships"
        # group in the fixture, so use Nightmare (group 27) and Wolf (group
        # 324) for the other two fits instead.
        cls.fit_harbinger = create_fit(cls.doctrine_a, T.HARBINGER, name="Armor Harbinger")
        cls.fit_oracle = create_fit(cls.doctrine_b, T.NIGHTMARE, name="Shield Oracle")
        cls.fit_standalone = create_fit(None, T.WOLF, name="Standalone Hel")

    def _get(self, **params):
        self.client.force_login(self.manager)
        return self.client.get(reverse("fitcheck:standards_list"), params)


class TestNameSearch(StandardsFiltersTestCase):
    def test_matches_are_shown(self):
        response = self._get(q="Harbinger")
        self.assertContains(response, "Armor Harbinger")

    def test_non_matches_are_hidden(self):
        response = self._get(q="Harbinger")
        self.assertNotContains(response, "Shield Oracle")
        self.assertNotContains(response, "Standalone Hel")


class TestDoctrineFilter(StandardsFiltersTestCase):
    def test_filters_to_one_doctrine(self):
        response = self._get(doctrine=self.doctrine_a.pk)
        self.assertContains(response, "Armor Harbinger")
        self.assertNotContains(response, "Shield Oracle")
        self.assertNotContains(response, "Standalone Hel")

    def test_doctrine_none_shows_only_standalone(self):
        response = self._get(doctrine="none")
        self.assertContains(response, "Standalone Hel")
        self.assertNotContains(response, "Armor Harbinger")
        self.assertNotContains(response, "Shield Oracle")


class TestHullClassFilter(StandardsFiltersTestCase):
    def test_filters_by_hull_group(self):
        harbinger_group_id = self.fit_harbinger.ship_type.eve_group_id
        response = self._get(group=harbinger_group_id)
        self.assertContains(response, "Armor Harbinger")
        self.assertNotContains(response, "Shield Oracle")
        self.assertNotContains(response, "Standalone Hel")


class TestCategoryFilter(StandardsFiltersTestCase):
    def test_filters_by_category(self):
        category = DoctrineCategory.objects.create(name="Caps")
        self.fit_oracle.categories.add(category)
        response = self._get(category=category.pk)
        self.assertContains(response, "Shield Oracle")
        self.assertNotContains(response, "Armor Harbinger")
        self.assertNotContains(response, "Standalone Hel")


class TestCombinedFilters(StandardsFiltersTestCase):
    def test_filters_and_together(self):
        # A second fit on doctrine_a with a name that also matches "Shield",
        # so q + doctrine narrows down to exactly one row when combined.
        create_fit(self.doctrine_a, T.ORACLE, name="Shield-Backup Oracle")
        response = self._get(q="Shield", doctrine=self.doctrine_a.pk)
        self.assertContains(response, "Shield-Backup Oracle")
        self.assertNotContains(response, "Shield Oracle")


class TestSorting(StandardsFiltersTestCase):
    def test_sort_by_hull_ascending(self):
        response = self._get(sort="hull")
        names_in_order = [
            fit.name for fit in response.context["page_obj"]
        ]
        hull_names = [
            self.fit_harbinger.ship_type.name,
            self.fit_oracle.ship_type.name,
            self.fit_standalone.ship_type.name,
        ]
        expected = [
            name
            for _hull, name in sorted(
                zip(
                    hull_names,
                    ["Armor Harbinger", "Shield Oracle", "Standalone Hel"],
                )
            )
        ]
        self.assertEqual(names_in_order, expected)

    def test_sort_by_hull_descending(self):
        asc_response = self._get(sort="hull")
        desc_response = self._get(sort="-hull")
        asc_names = [fit.name for fit in asc_response.context["page_obj"]]
        desc_names = [fit.name for fit in desc_response.context["page_obj"]]
        self.assertEqual(desc_names, list(reversed(asc_names)))

    def test_invalid_sort_falls_back_to_name(self):
        response = self._get(sort="evil__injection")
        self.assertEqual(response.status_code, 200)
        names_in_order = [fit.name for fit in response.context["page_obj"]]
        self.assertEqual(names_in_order, sorted(names_in_order))
        self.assertEqual(response.context["active_sort"], "")


class TestPagination(StandardsFiltersTestCase):
    def test_pagination_splits_across_pages(self):
        for i in range(55):
            create_fit(None, T.HEL, name=f"Bulk Fit {i:03d}")
        response = self._get()
        self.assertEqual(len(response.context["page_obj"]), 50)

        response_page_2 = self._get(page=2)
        self.assertEqual(len(response_page_2.context["page_obj"]), 8)  # 55 + 3 base fits - 50


class TestDoctrinePillOverflow(StandardsFiltersTestCase):
    def test_pill_doctrines_capped_and_overflow_holds_rest(self):
        for i in range(10):
            create_doctrine(name=f"Doctrine {i:02d}")
        response = self._get()
        self.assertEqual(len(response.context["pill_doctrines"]), 8)
        # 2 base doctrines + 10 new = 12 total, 8 shown as pills, 4 overflow.
        self.assertEqual(len(response.context["overflow_doctrines"]), 4)


class TestHasFilters(StandardsFiltersTestCase):
    def test_false_on_bare_get(self):
        response = self._get()
        self.assertFalse(response.context["has_filters"])

    def test_true_with_a_filter(self):
        response = self._get(q="Harbinger")
        self.assertTrue(response.context["has_filters"])
