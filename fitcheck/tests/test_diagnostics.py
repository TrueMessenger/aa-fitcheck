"""The admin Diagnostics page + shared diagnostics service (read-only)."""

from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..services import corptools_source, diagnostics
from .testdata.factories import create_user
from .testdata.fake_corptools.models import CharacterAsset, CharacterAudit
from .testdata.sde_fixtures import T, create_sde_testdata


class HealthSummaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_health_summary_has_expected_keys_and_reflects_fixtures(self):
        h = diagnostics.health_summary()
        for key in (
            "fitcheck_version", "corptools_installed", "asset_source",
            "sde_loaded", "sde_type_total", "sde_category_counts",
            "deploy_warnings", "enforcement", "pending_submissions",
            "active_doctrines", "active_fits",
        ):
            self.assertIn(key, h)
        self.assertTrue(h["sde_loaded"])  # fixtures load ship types
        self.assertGreater(h["sde_category_counts"]["ship"], 0)
        self.assertIsNotNone(h["enforcement"])


class InventoryReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.char = EveCharacter.objects.create(
            character_id=90000222, character_name="Doc Pilot",
            corporation_id=2001, corporation_name="C", corporation_ticker="C",
        )
        cls.audit = CharacterAudit.objects.create(character=cls.char)

    def _add_ship(self):
        CharacterAsset.objects.create(
            character=self.audit, singleton=True, item_id=7001,
            location_flag="Hangar", location_id=60003760, location_type="station",
            quantity=1, type_id=T.HARBINGER, name="Brawl",
        )

    def test_serving_verdict_when_corptools_has_the_ship(self):
        self._add_ship()
        self.audit.set_update_time("assets")
        self.audit.save()
        with mock.patch.object(corptools_source, "corptools_installed", lambda: True):
            r = diagnostics.inventory_report(self.char.character_id)
        self.assertEqual(r["corptools"]["ship_rows_sde_filtered"], 1)
        self.assertIn("should list them", r["verdict"])

    def test_not_synced_verdict_when_assets_never_synced(self):
        self._add_ship()  # ship rows exist but assets_synced_at is empty
        with mock.patch.object(corptools_source, "corptools_installed", lambda: True):
            r = diagnostics.inventory_report(self.char.character_id)
        self.assertIsNone(r["corptools"]["assets_synced_at"])
        self.assertIn("not synced", r["verdict"].lower())


class DiagnosticsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.member = create_user("diag-member", permissions=("basic_access",))
        cls.admin = create_user("diag-admin", permissions=("basic_access", "manage_policies"))
        cls.char = EveCharacter.objects.create(
            character_id=90000333, character_name="Web Pilot",
            corporation_id=2001, corporation_name="C", corporation_ticker="C",
        )
        CharacterOwnership.objects.create(
            user=cls.admin, character=cls.char, owner_hash="diag-hash-1"
        )
        audit = CharacterAudit.objects.create(character=cls.char)
        audit.set_update_time("assets")
        audit.save()
        CharacterAsset.objects.create(
            character=audit, singleton=True, item_id=8001,
            location_flag="Hangar", location_id=60003760, location_type="station",
            quantity=1, type_id=T.HARBINGER, name="Brawl",
        )

    def test_requires_manage_policies(self):
        self.client.force_login(self.member)
        self.assertEqual(
            self.client.get(reverse("fitcheck:diagnostics")).status_code, 302
        )

    def test_admin_sees_health_panel(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("fitcheck:diagnostics"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Static data (SDE)")
        self.assertContains(resp, "Inventory doctor")

    def test_inventory_doctor_form_reports_ship(self):
        self.client.force_login(self.admin)
        with mock.patch.object(corptools_source, "corptools_installed", lambda: True):
            resp = self.client.get(
                reverse("fitcheck:diagnostics"), {"character": "Web Pilot"}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Verdict")
        self.assertContains(resp, "should list them")

    def test_inventory_doctor_unknown_character(self):
        self.client.force_login(self.admin)
        resp = self.client.get(
            reverse("fitcheck:diagnostics"), {"character": "Nobody At All"}
        )
        self.assertContains(resp, "No character matches")
