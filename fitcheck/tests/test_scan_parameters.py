"""Tests for the admin-tunable scan/result limits (Settings -> Scan & Result
Limits): the singleton model, the settings page, and the consumers that used
to carry these bounds as hard-coded constants."""

from unittest import mock

from django.contrib.auth.models import Permission, User
from django.test import RequestFactory, TestCase
from django.urls import reverse

from ..models import ScanParameters
from .testdata.factories import create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


def _grant(user, codename):
    user.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="fitcheck", codename=codename
        )
    )
    return User.objects.get(pk=user.pk)  # refresh perm cache


class TestScanParametersModel(TestCase):
    def test_current_creates_singleton_with_defaults(self):
        params = ScanParameters.current()
        self.assertEqual(params.pk, 1)
        self.assertEqual(params.member_scan_esi_budget, 25)
        self.assertEqual(params.audit_ships_per_post, 50)
        self.assertEqual(params.abyssal_lookups_per_ship, 25)
        self.assertEqual(params.results_per_page, 50)

    def test_save_always_targets_the_single_row(self):
        ScanParameters(member_scan_esi_budget=5).save()
        ScanParameters(member_scan_esi_budget=7).save()
        self.assertEqual(ScanParameters.objects.count(), 1)
        self.assertEqual(ScanParameters.current().member_scan_esi_budget, 7)


class TestScanParametersPage(TestCase):
    URL = "fitcheck:scan_parameters"

    def test_requires_manage_policies(self):
        user = create_user("param_nobody")
        self.client.force_login(user)
        response = self.client.get(reverse(self.URL))
        self.assertEqual(response.status_code, 302)  # AA perm decorator redirects

    def test_get_renders_defaults(self):
        user = _grant(create_user("param_admin"), "manage_policies")
        self.client.force_login(user)
        response = self.client.get(reverse(self.URL))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Scan &amp; Result Limits")
        self.assertEqual(
            response.context["form"].instance.member_scan_esi_budget, 25
        )

    def test_post_saves_and_rereads(self):
        user = _grant(create_user("param_editor"), "manage_policies")
        self.client.force_login(user)
        response = self.client.post(
            reverse(self.URL),
            {
                "member_scan_esi_budget": 40,
                "audit_ships_per_post": 10,
                "abyssal_lookups_per_ship": 5,
                "results_per_page": 25,
            },
        )
        self.assertRedirects(response, reverse(self.URL))
        params = ScanParameters.current()
        self.assertEqual(params.member_scan_esi_budget, 40)
        self.assertEqual(params.audit_ships_per_post, 10)
        self.assertEqual(params.abyssal_lookups_per_ship, 5)
        self.assertEqual(params.results_per_page, 25)


class TestPaginateUsesParameter(TestCase):
    def test_default_page_size_comes_from_parameters(self):
        from ..views.common import paginate

        params = ScanParameters.current()
        params.results_per_page = 10
        params.save()
        request = RequestFactory().get("/")
        page_obj, _elided, _qs = paginate(request, list(range(25)))
        self.assertEqual(page_obj.paginator.per_page, 10)
        self.assertEqual(len(page_obj.object_list), 10)

    def test_explicit_per_page_still_wins(self):
        from ..views.common import paginate

        request = RequestFactory().get("/")
        page_obj, _elided, _qs = paginate(request, list(range(25)), per_page=7)
        self.assertEqual(page_obj.paginator.per_page, 7)


class TestAuditCapParameter(TestCase):
    """audit_ships_per_post bounds Phase-2 grading on the member-inventory
    page - lowering it to 2 grades exactly 2 of 3 ticked ships."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_audit_stops_at_configured_cap(self):
        from allianceauth.eveonline.models import EveCharacter

        from ..models import FitSubmission
        from ..services.esi_assets import OwnedShip
        from ..services.fit_data import ParsedFit

        params = ScanParameters.current()
        params.audit_ships_per_post = 2
        params.save()

        user = create_user("audit_capped")
        main = EveCharacter.objects.create(
            character_id=10001, character_name="Main 10001",
            corporation_id=2001, corporation_name="Test Corp",
            corporation_ticker="TC", alliance_id=99,
            alliance_name="Test Alliance", alliance_ticker="TA",
            security_status=0,
        )
        user.profile.main_character = main
        user.profile.save()
        user = _grant(user, "view_member_inventory")
        EveCharacter.objects.create(
            character_id=50010, character_name="Member 50010",
            corporation_id=2001, corporation_name="Corp",
            corporation_ticker="CRP", alliance_id=99,
            alliance_name="Alliance", alliance_ticker="ALI",
            security_status=0,
        )
        fit = create_fit(None, T.HARBINGER, name="Cap Target")

        ships = [
            OwnedShip(
                character_id=50010, character_name="Member 50010",
                item_id=70001 + i, type_id=T.HARBINGER, type_name="Harbinger",
                group_name="Battlecruiser", ship_name=f"Harb {i}",
                location_name="Jita",
            )
            for i in range(3)
        ]
        inv = mock.Mock(
            ships=ships, characters_without_token=[], esi_fallback_skipped=[],
            errors={}, error_limited=False,
        )

        def parsed_for(_owner, _cid, iid, **_kwargs):
            return ParsedFit(
                ship_type_id=T.HARBINGER, fit_name="Member's Harb",
                items=[], source_ship_item_id=iid,
            )

        with mock.patch(
            "fitcheck.services.esi_assets.get_inventory_for_characters",
            return_value=inv,
        ), mock.patch(
            "fitcheck.services.esi_assets.tokens_by_character", return_value={}
        ), mock.patch(
            "fitcheck.services.esi_assets.resolve_contents",
            return_value=[{"item_id": 70001, "type_id": T.HARBINGER}],
        ), mock.patch(
            "fitcheck.services.esi_assets.build_parsed_fit",
            side_effect=parsed_for,
        ):
            self.client.force_login(user)
            response = self.client.post(
                reverse("fitcheck:member_inventory_for_fit", args=[fit.pk]),
                {"ships": ["50010:70001", "50010:70002", "50010:70003"]},
            )

        self.assertEqual(response.status_code, 200)
        audited = [r for r in response.context["ship_rows"] if r["audited"]]
        self.assertEqual(len(audited), 2)
        self.assertEqual(
            FitSubmission.objects.filter(doctrine_fit=fit).count(), 2
        )
