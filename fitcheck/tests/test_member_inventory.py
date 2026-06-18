"""Tests for the proactive member-inventory feature.

Covers perm-driven scope resolution (alliance-wide vs corp-only vs none),
view permission gating, and the corp dropdown's visibility rule. The ESI
fan-out itself (build_parsed_fit + validate_parsed_ship) is mocked - the
view's branching logic is the part under test here.
"""

from unittest import mock

from django.contrib.auth.models import Permission, User
from django.test import RequestFactory, TestCase
from django.urls import reverse

from allianceauth.eveonline.models import EveCharacter

from ..views.manage import _resolve_target_charset
from .testdata.factories import create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


def _attach_main(user, *, character_id=10001, alliance_id=99, corporation_id=2001):
    """Replace the auto-created main with one whose alliance/corp matches
    what each test wants."""
    main = EveCharacter.objects.create(
        character_id=character_id,
        character_name=f"Main {character_id}",
        corporation_id=corporation_id,
        corporation_name="Test Corp",
        corporation_ticker="TC",
        alliance_id=alliance_id,
        alliance_name="Test Alliance",
        alliance_ticker="TA",
        security_status=0,
    )
    user.profile.main_character = main
    user.profile.save()
    return main


def _make_member(*, character_id, alliance_id, corporation_id):
    return EveCharacter.objects.create(
        character_id=character_id,
        character_name=f"Member {character_id}",
        corporation_id=corporation_id,
        corporation_name="Corp",
        corporation_ticker="CRP",
        alliance_id=alliance_id,
        alliance_name="Alliance",
        alliance_ticker="ALI",
        security_status=0,
    )


def _grant(user, codename):
    user.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="fitcheck", codename=codename
        )
    )
    return User.objects.get(pk=user.pk)  # refresh perm cache


class TestResolveTargetCharset(TestCase):
    """The scoping helper translates one of three perm shapes into a queryset."""

    def test_no_perm_returns_empty(self):
        user = create_user("noperm")
        _attach_main(user)
        self.assertFalse(_resolve_target_charset(user).exists())

    def test_alliance_perm_returns_alliance_members(self):
        user = create_user("alliance_admin")
        _attach_main(user, alliance_id=99, corporation_id=2001)
        user = _grant(user, "view_member_inventory")

        _make_member(character_id=20001, alliance_id=99, corporation_id=2001)
        _make_member(character_id=20002, alliance_id=99, corporation_id=2002)
        _make_member(character_id=20003, alliance_id=88, corporation_id=2003)  # other alliance

        result = _resolve_target_charset(user)
        char_ids = set(result.values_list("character_id", flat=True))
        self.assertIn(20001, char_ids)
        self.assertIn(20002, char_ids)
        self.assertNotIn(20003, char_ids)

    def test_corp_perm_returns_only_own_corp(self):
        user = create_user("corp_director")
        _attach_main(user, alliance_id=99, corporation_id=2001)
        user = _grant(user, "view_own_corp_inventory")

        _make_member(character_id=30001, alliance_id=99, corporation_id=2001)
        _make_member(character_id=30002, alliance_id=99, corporation_id=2002)  # sibling corp

        result = _resolve_target_charset(user)
        char_ids = set(result.values_list("character_id", flat=True))
        self.assertIn(30001, char_ids)
        self.assertNotIn(30002, char_ids)

    def test_alliance_perm_falls_back_to_corp_when_no_alliance(self):
        """Pilots in NPC corps have no alliance_id. Rather than 403, scope
        down to their corp so single-corp shops still get value."""
        user = create_user("npc_corp_admin")
        _attach_main(user, alliance_id=None, corporation_id=2001)
        user = _grant(user, "view_member_inventory")

        _make_member(character_id=40001, alliance_id=None, corporation_id=2001)
        _make_member(character_id=40002, alliance_id=None, corporation_id=2002)

        result = _resolve_target_charset(user)
        char_ids = set(result.values_list("character_id", flat=True))
        self.assertIn(40001, char_ids)
        self.assertNotIn(40002, char_ids)


class TestMemberInventoryView(TestCase):
    """End-to-end: perm gating, corp dropdown visibility, search filters."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        # Patch the ESI helpers so the test never hits the wire.
        self.patches = [
            mock.patch(
                "fitcheck.services.esi_assets.get_inventory_for_characters",
                return_value=mock.Mock(
                    ships=[],
                    characters_without_token=[],
                    errors={},
                    error_limited=False,
                    assets_by_character={},
                    token_by_character={},
                ),
            ),
            mock.patch(
                "fitcheck.services.esi_assets.tokens_by_character",
                return_value={},
            ),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_view_403s_without_perm(self):
        user = create_user("nobody")
        _attach_main(user)
        fit = create_fit(None, T.HARBINGER)
        self.client.force_login(user)
        response = self.client.get(
            reverse("fitcheck:member_inventory_for_fit", args=[fit.pk])
        )
        self.assertEqual(response.status_code, 403)

    def test_alliance_user_sees_corp_dropdown(self):
        user = create_user("alliance")
        _attach_main(user, alliance_id=99, corporation_id=2001)
        user = _grant(user, "view_member_inventory")
        _make_member(character_id=50001, alliance_id=99, corporation_id=2001)

        fit = create_fit(None, T.HARBINGER)
        self.client.force_login(user)
        response = self.client.get(
            reverse("fitcheck:member_inventory_for_fit", args=[fit.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["show_corp_filter"])

    def test_corp_only_user_hides_corp_dropdown(self):
        user = create_user("director")
        _attach_main(user, alliance_id=99, corporation_id=2001)
        user = _grant(user, "view_own_corp_inventory")
        _make_member(character_id=60001, alliance_id=99, corporation_id=2001)

        fit = create_fit(None, T.HARBINGER)
        self.client.force_login(user)
        response = self.client.get(
            reverse("fitcheck:member_inventory_for_fit", args=[fit.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["show_corp_filter"])

    def test_scan_grades_each_ship_against_this_fit(self):
        """Regression for C1: with ships present, the per-fit scan must grade each
        one against THIS fit via submit_fit and persist one FitSubmission. The
        previous code called validate_parsed_ship with unsupported kwargs (and a
        list return) - a runtime TypeError the empty-ships tests never exercised."""
        from ..models import FitSubmission
        from ..services.esi_assets import OwnedShip
        from ..services.fit_data import ParsedFit

        user = create_user("alliance_scan")
        _attach_main(user, alliance_id=99, corporation_id=2001)
        user = _grant(user, "view_member_inventory")
        _make_member(character_id=50010, alliance_id=99, corporation_id=2001)
        fit = create_fit(None, T.HARBINGER, name="Scan Target")

        ship = OwnedShip(
            character_id=50010, character_name="Member 50010", item_id=70001,
            type_id=T.HARBINGER, type_name="Harbinger", group_name="Battlecruiser",
            ship_name="Member's Harb", location_name="Jita",
        )
        inv = mock.Mock(
            ships=[ship], characters_without_token=[], errors={},
            error_limited=False, assets_by_character={50010: []},
            token_by_character={50010: object()},
        )
        with mock.patch(
            "fitcheck.services.esi_assets.get_inventory_for_characters",
            return_value=inv,
        ), mock.patch(
            "fitcheck.services.esi_assets.build_parsed_fit",
            return_value=ParsedFit(
                ship_type_id=T.HARBINGER, fit_name="Member's Harb",
                items=[], source_ship_item_id=70001,
            ),
        ):
            self.client.force_login(user)
            response = self.client.get(
                reverse("fitcheck:member_inventory_for_fit", args=[fit.pk])
            )

        self.assertEqual(response.status_code, 200)
        rows = response.context["ship_rows"]
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["submission_pk"])
        self.assertTrue(
            FitSubmission.objects.filter(
                doctrine_fit=fit, esi_ship_item_id=70001
            ).exists()
        )
