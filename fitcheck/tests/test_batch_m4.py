"""Tests for the M4.x UI/feature batch: tag colours, abyssal modal naming/icons,
the Update Fit view, and the Fittings & Standards rename."""

from django.test import TestCase
from django.urls import reverse

from ..models import ArchivedFitVersion, DoctrineCategory
from ..models.doctrine import SubstitutionPolicy
from ..services.doctrine_import import import_fit
from ..services.substitutions import abyssal_name_for_item, attribute_icon
from .testdata.factories import add_item, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


class CategoryColorTests(TestCase):
    def test_text_color_contrast(self):
        # Light background -> dark text; dark background -> light text.
        self.assertEqual(DoctrineCategory(color="#ffc107").text_color, "#000000")
        self.assertEqual(DoctrineCategory(color="#0dcaf0").text_color, "#000000")
        self.assertEqual(DoctrineCategory(color="#212529").text_color, "#ffffff")
        self.assertEqual(DoctrineCategory(color="#dc3545").text_color, "#ffffff")

    def test_text_color_handles_bad_input(self):
        self.assertEqual(DoctrineCategory(color="").text_color, "#ffffff")
        self.assertEqual(DoctrineCategory(color="not-hex").text_color, "#ffffff")

    def test_category_add_returns_color(self):
        user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.client.force_login(user)
        resp = self.client.post(
            reverse("fitcheck:category_add"),
            {"name": "Shiny", "color": "#198754"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["color"], "#198754")
        self.assertEqual(data["text_color"], "#ffffff")
        self.assertEqual(DoctrineCategory.objects.get(name="Shiny").color, "#198754")


class AbyssalNamingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_abyssal_name_from_sde(self):
        fit = create_fit(None, T.HARBINGER)
        item = add_item(fit, "MED", T.WEB_II, 1, policy=SubstitutionPolicy.MEET_OR_BEAT)
        type_id, name = abyssal_name_for_item(item)
        self.assertEqual(type_id, T.WEB_ABYSSAL)
        self.assertEqual(name, "Abyssal Stasis Webifier")

    def test_no_mapping_returns_none(self):
        fit = create_fit(None, T.HARBINGER)
        item = add_item(fit, "LOW", T.HEAT_SINK_II, 1)
        self.assertEqual(abyssal_name_for_item(item), (None, None))

    def test_attribute_icon_keywords(self):
        self.assertEqual(attribute_icon("CPU usage"), "fa-microchip")
        self.assertEqual(attribute_icon("Optimal Range"), "fa-ruler-horizontal")
        self.assertEqual(attribute_icon("Maximum Velocity Bonus"), "fa-gauge-high")
        self.assertEqual(attribute_icon("Some Unknown Attribute"), "fa-gauge")

    def test_candidates_endpoint_includes_naming_and_icons(self):
        user = create_user("mgr2", permissions=("basic_access", "manage_doctrines"))
        self.client.force_login(user)
        fit = create_fit(None, T.HARBINGER)
        item = add_item(
            fit, "MED", T.WEB_II, 1,
            policy=SubstitutionPolicy.MEET_OR_BEAT,
            checked_attributes=[20, 54],
        )
        resp = self.client.get(
            reverse("fitcheck:attribute_candidates", args=[item.pk])
        )
        data = resp.json()
        self.assertEqual(data["base_type_id"], T.WEB_II)
        self.assertEqual(data["abyssal_type_id"], T.WEB_ABYSSAL)
        self.assertEqual(data["abyssal_name"], "Abyssal Stasis Webifier")
        self.assertTrue(all("icon" in row for row in data["attributes"]))


class UpdateFitViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.user = create_user("mgr3", permissions=("basic_access", "manage_doctrines"))

    def test_get_prefills_and_post_updates(self):
        self.client.force_login(self.user)
        fit = import_fit("[Harbinger, V]\nHeat Sink II\nCap Recharger II\n", self.user)
        url = reverse("fitcheck:manage_fit_update", args=[fit.pk])

        get = self.client.get(url)
        self.assertEqual(get.status_code, 200)
        self.assertContains(get, "Heat Sink II")

        post = self.client.post(
            url, {"eft_text": "[Harbinger, V]\nHeat Sink II\nStasis Webifier II\n"}
        )
        self.assertEqual(post.status_code, 302)
        fit.refresh_from_db()
        self.assertEqual(fit.version, 2)
        self.assertTrue(ArchivedFitVersion.objects.filter(fit=fit, version=1).exists())
        self.assertTrue(fit.items.filter(module_type_id=T.WEB_II).exists())
        self.assertFalse(fit.items.filter(module_type_id=T.CAP_RECHARGER_II).exists())


class TabRenameTests(TestCase):
    def test_standards_list_uses_new_name(self):
        user = create_user("mgr4", permissions=("basic_access", "manage_doctrines"))
        self.client.force_login(user)
        resp = self.client.get(reverse("fitcheck:standards_list"))
        self.assertContains(resp, "Fittings &amp; Standards")


class TemplateSmokeTests(TestCase):
    """Render the new templates so syntax errors surface in CI, not just live."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.user = create_user("smoke", permissions=("basic_access", "manage_doctrines"))

    def test_doctrine_create_renders_with_color_picker(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("fitcheck:doctrine_create"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "coloris")

    def test_fit_archives_renders(self):
        self.client.force_login(self.user)
        fit = import_fit("[Harbinger, A]\nHeat Sink II\n", self.user)
        resp = self.client.get(reverse("fitcheck:fit_archives", args=[fit.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Version History")

    def test_fit_items_renders_with_attr_modal(self):
        self.client.force_login(self.user)
        fit = import_fit("[Harbinger, A]\nStasis Webifier II\n", self.user)
        resp = self.client.get(reverse("fitcheck:manage_fit_items", args=[fit.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Abyssal Attribute Requirements")
