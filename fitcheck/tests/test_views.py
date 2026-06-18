from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from ..constants import Section
from ..models import Doctrine, DoctrineCategory, FitSubmission
from ..services.check_runner import submit_fit
from ..services.eft_parser import parse_eft
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata

EFT_GOOD = "[Harbinger, Mine]\nHeat Sink II\nHeat Sink II\nImperial Navy Heat Sink\n"


class ViewTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Armor Brawl")
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        cls.member = create_user("member")
        cls.manager = create_user("manager", permissions=["basic_access", "manage_doctrines"])
        cls.reviewer = create_user(
            "reviewer", permissions=["basic_access", "review_submissions"]
        )
        cls.secure_group_admin = create_user(
            "sgadmin", permissions=["basic_access", "secure_group_management"]
        )
        cls.outsider = create_user("outsider", permissions=[])

    def _member_submission(self):
        return submit_fit(
            self.member,
            self.fit,
            parse_eft(EFT_GOOD),
            eft_text=EFT_GOOD,
            doctrine=self.doctrine,
        )


class TestMemberViews(ViewTestCase):
    def test_index_requires_permission(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("fitcheck:index")).status_code, 302)

        self.client.force_login(self.member)
        response = self.client.get(reverse("fitcheck:index"))
        self.assertContains(response, self.doctrine.name)

    def test_category_targeting_hides_doctrine(self):
        gated = create_doctrine(name="Gated")
        gated_fit = create_fit(gated, T.ORACLE, name="Secret Oracle")
        group = Group.objects.create(name="Special Team")
        cat = DoctrineCategory.objects.create(name="Capitals")
        cat.selected_groups.add(group)
        gated.categories.add(cat)

        self.client.force_login(self.member)
        response = self.client.get(reverse("fitcheck:index"))
        self.assertNotContains(response, "Gated")
        # Direct-URL access to the gated fit is also denied.
        self.assertEqual(
            self.client.get(
                reverse("fitcheck:fit_detail", args=[gated_fit.pk])
            ).status_code,
            403,
        )

        # Granting a Selected group admits the member.
        self.member.groups.add(group)
        response = self.client.get(reverse("fitcheck:index"))
        self.assertContains(response, "Gated")
        self.assertEqual(
            self.client.get(
                reverse("fitcheck:fit_detail", args=[gated_fit.pk])
            ).status_code,
            200,
        )

    def test_standalone_fit_is_visible_to_members(self):
        standalone = create_fit(None, T.ORACLE, name="Baseline Oracle")
        self.client.force_login(self.member)
        response = self.client.get(reverse("fitcheck:fit_detail", args=[standalone.pk]))
        self.assertContains(response, "Baseline Oracle")

    def test_fit_detail_shows_alternatives(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("fitcheck:fit_detail", args=[self.fit.pk]))
        self.assertContains(response, "Heat Sink II")
        self.assertContains(response, "Imperial Navy Heat Sink")

    def test_submit_eft_is_staff_only(self):
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("fitcheck:submit_eft", args=[self.fit.pk]), {"eft_text": EFT_GOOD}
        )
        self.assertEqual(response.status_code, 403)

    def test_submit_eft_flow_for_reviewer(self):
        self.client.force_login(self.reviewer)
        response = self.client.post(
            reverse("fitcheck:submit_eft", args=[self.fit.pk]),
            {"eft_text": EFT_GOOD},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        submission = FitSubmission.objects.get(user=self.reviewer)
        self.assertEqual(submission.verdict, FitSubmission.Verdict.COMPLIANT_SUBS)
        self.assertContains(response, "Compliant with substitutions")
        self.assertContains(response, "Imperial Navy Heat Sink")

    def test_pilot_fittings_shows_only_own_submissions(self):
        mine = self._member_submission()
        other_user = create_user("other")
        theirs = submit_fit(other_user, self.fit, parse_eft(EFT_GOOD), eft_text=EFT_GOOD)

        self.client.force_login(self.member)
        response = self.client.get(reverse("fitcheck:pilot_fittings"))
        self.assertContains(
            response, reverse("fitcheck:submission_detail", args=[mine.pk])
        )
        self.assertNotContains(
            response, reverse("fitcheck:submission_detail", args=[theirs.pk])
        )

    def test_submission_detail_is_private(self):
        submission = self._member_submission()

        other = create_user("snooper")
        self.client.force_login(other)
        response = self.client.get(
            reverse("fitcheck:submission_detail", args=[submission.pk])
        )
        self.assertEqual(response.status_code, 403)

        for allowed_user in (self.reviewer, self.secure_group_admin):
            self.client.force_login(allowed_user)
            response = self.client.get(
                reverse("fitcheck:submission_detail", args=[submission.pk])
            )
            self.assertEqual(response.status_code, 200)


class TestInventoryHullPrefilter(ViewTestCase):
    """The 'Validate my ships' button on a fitting passes ?type_id= so the
    inventory page lands pre-filtered to that hull."""

    def _patched_inventory(self):
        """Build a fake ShipInventory with one Harbinger and one Oracle so we
        can assert that type_id=Harbinger excludes the Oracle."""
        from unittest.mock import patch

        from ..services.esi_assets import OwnedShip, ShipInventory
        from .testdata.sde_fixtures import T

        inv = ShipInventory()
        inv.ships = [
            OwnedShip(
                character_id=1, character_name="Pilot A", item_id=1,
                type_id=T.HARBINGER, type_name="Harbinger",
                group_name="Combat Battlecruiser", ship_name="",
                location_name="Jita IV - 4-4",
            ),
            OwnedShip(
                character_id=1, character_name="Pilot A", item_id=2,
                type_id=T.ORACLE, type_name="Oracle",
                group_name="Attack Battlecruiser", ship_name="",
                location_name="Jita IV - 4-4",
            ),
        ]
        return patch(
            "fitcheck.services.esi_assets.get_ship_inventory", return_value=inv
        )

    def test_inventory_pre_filters_by_type_id_param(self):
        from .testdata.sde_fixtures import T

        self.client.force_login(self.member)
        with self._patched_inventory():
            response = self.client.get(
                reverse("fitcheck:ship_inventory"), {"type_id": T.HARBINGER}
            )
        self.assertContains(response, "Harbinger")
        self.assertNotContains(response, "Oracle")
        # The pill says "Pre-filtered to Harbinger" and offers a clear link.
        self.assertContains(response, "Pre-filtered to")
        self.assertContains(response, "Show all ships")

    def test_invalid_type_id_query_is_ignored_gracefully(self):
        self.client.force_login(self.member)
        with self._patched_inventory():
            response = self.client.get(
                reverse("fitcheck:ship_inventory"), {"type_id": "not-a-number"}
            )
        self.assertEqual(response.status_code, 200)
        # Both ships are visible since the bad filter was dropped.
        self.assertContains(response, "Harbinger")
        self.assertContains(response, "Oracle")

    def test_fit_detail_validate_button_includes_type_id(self):
        """The button on a fitting page must encode ?type_id= so the link
        actually pre-filters."""
        from .testdata.sde_fixtures import T

        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:fit_detail", args=[self.fit.pk])
        )
        expected = (
            reverse("fitcheck:ship_inventory") + f"?type_id={T.HARBINGER}"
        )
        self.assertContains(response, expected)


class TestManageViews(ViewTestCase):
    def test_standards_require_permission(self):
        self.client.force_login(self.member)
        self.assertEqual(
            self.client.get(reverse("fitcheck:standards_list")).status_code, 302
        )
        self.client.force_login(self.manager)
        self.assertEqual(
            self.client.get(reverse("fitcheck:standards_list")).status_code, 200
        )

    def test_secure_group_role_cannot_manage(self):
        self.client.force_login(self.secure_group_admin)
        self.assertEqual(
            self.client.get(reverse("fitcheck:standards_list")).status_code, 302
        )
        self.assertEqual(
            self.client.get(reverse("fitcheck:doctrine_create")).status_code, 302
        )

    def test_create_doctrine_and_import_fit(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:doctrine_create"),
            {"name": "Shield Supers", "description": "", "is_active": "on"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        doctrine = Doctrine.objects.get(name="Shield Supers")

        response = self.client.post(
            reverse("fitcheck:manage_fit_import", args=[doctrine.pk]),
            {
                "eft_text": "[Hel, Standard]\nTemplar II x9\n",
                "name": "",
                "default_policy": "VA",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        fit = doctrine.fits.get()
        self.assertEqual(fit.name, "Standard")
        self.assertEqual(fit.ship_type_id, T.HEL)

    def test_standalone_import_needs_no_doctrine(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:standard_import"),
            {"eft_text": "[Hel, Baseline Hel]\nTemplar II x9\n", "name": "", "default_policy": "VA"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        from ..models import DoctrineFit

        fit = DoctrineFit.objects.get(name="Baseline Hel")
        self.assertEqual(fit.doctrines.count(), 0)

    def test_assign_and_remove_fitting(self):
        standalone = create_fit(None, T.ORACLE, name="Loose Oracle")
        self.client.force_login(self.manager)
        self.client.post(
            reverse("fitcheck:doctrine_assign_fit", args=[self.doctrine.pk]),
            {"fit": standalone.pk},
        )
        self.assertIn(self.doctrine, standalone.doctrines.all())

        self.client.post(
            reverse("fitcheck:doctrine_remove_fit", args=[self.doctrine.pk, standalone.pk])
        )
        standalone.refresh_from_db()
        self.assertEqual(standalone.doctrines.count(), 0)
        # the fitting itself survives removal
        self.assertTrue(type(standalone).objects.filter(pk=standalone.pk).exists())

    def test_import_shows_parse_errors(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:manage_fit_import", args=[self.doctrine.pk]),
            {"eft_text": "[Harbinger, X]\nNot A Module\n", "name": "", "default_policy": "VA"},
        )
        self.assertContains(response, "Not A Module")
        self.assertEqual(self.doctrine.fits.count(), 1)  # only the fixture fit


class TestShipNameDerivation(ViewTestCase):
    """The in-game custom ship name is derived from the EFT header so reviewers
    can distinguish two same-hull submissions from one pilot."""

    def test_ship_name_property_parses_eft_header(self):
        submission = self._member_submission()
        submission.eft_text = "[Harbinger, Brick Maintainer Harb]\nHeat Sink II\n"
        submission.save(update_fields=["eft_text"])
        # cached_property: refetch from DB to bypass the cached blank value.
        from ..models import FitSubmission as FS

        submission = FS.objects.get(pk=submission.pk)
        self.assertEqual(submission.ship_name, "Brick Maintainer Harb")

    def test_ship_name_handles_blank_eft_text(self):
        submission = self._member_submission()
        submission.eft_text = ""
        submission.save(update_fields=["eft_text"])
        from ..models import FitSubmission as FS

        self.assertEqual(FS.objects.get(pk=submission.pk).ship_name, "")

    def test_ship_name_handles_malformed_header(self):
        submission = self._member_submission()
        submission.eft_text = "This is not a valid EFT header\nHeat Sink II\n"
        submission.save(update_fields=["eft_text"])
        from ..models import FitSubmission as FS

        self.assertEqual(FS.objects.get(pk=submission.pk).ship_name, "")

    def test_queue_renders_ship_name_under_type(self):
        submission = self._member_submission()
        submission.eft_text = "[Harbinger, Brick Maintainer]\nHeat Sink II\n"
        submission.save(update_fields=["eft_text"])
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse("fitcheck:review_queue"))
        self.assertContains(response, "Brick Maintainer")


class TestReviewViews(ViewTestCase):
    def test_queue_permission_and_content(self):
        submission = self._member_submission()
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("fitcheck:review_queue")).status_code, 403)

        for allowed_user in (self.reviewer, self.secure_group_admin):
            self.client.force_login(allowed_user)
            response = self.client.get(reverse("fitcheck:review_queue"))
            self.assertContains(response, "member")
            self.assertContains(response, str(submission.pk))

    def test_queue_filters(self):
        self._member_submission()
        self.client.force_login(self.reviewer)
        url = reverse("fitcheck:review_queue")
        self.assertContains(self.client.get(url, {"pilot": "member"}), "Armor Brawl")
        self.assertNotContains(
            self.client.get(url, {"pilot": "nobody-by-this-name"}), "Armor Brawl"
        )
        self.assertContains(
            self.client.get(url, {"doctrine": self.doctrine.pk}), "Armor Brawl"
        )
        self.assertContains(self.client.get(url, {"ship": "Harbinger"}), "Armor Brawl")

    def test_approve_flow_works_for_secure_group_role(self):
        submission = self._member_submission()
        self.client.force_login(self.secure_group_admin)
        response = self.client.post(
            reverse("fitcheck:review_decide", args=[submission.pk]),
            {"decision": "approve", "comment": "ok"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        submission.refresh_from_db()
        self.assertEqual(submission.status, FitSubmission.Status.APPROVED)

    def test_approve_without_comment_succeeds_even_for_non_compliant(self):
        """Approving an auto-NON_COMPLIANT submission with an empty comment
        is now allowed - the FC-waiver text requirement is gone for approves."""
        bad_eft = "[Harbinger, Bad]\nHeat Sink II\n"
        submission = submit_fit(
            self.member, self.fit, parse_eft(bad_eft), eft_text=bad_eft
        )
        self.assertEqual(submission.verdict, FitSubmission.Verdict.NON_COMPLIANT)

        self.client.force_login(self.reviewer)
        self.client.post(
            reverse("fitcheck:review_decide", args=[submission.pk]),
            {"decision": "approve", "comment": ""},
        )
        submission.refresh_from_db()
        self.assertEqual(submission.status, FitSubmission.Status.APPROVED)

    def test_reject_without_comment_keeps_pending(self):
        """Rejecting without a comment still blocks - server enforces it."""
        bad_eft = "[Harbinger, Bad]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"
        submission = submit_fit(
            self.member, self.fit, parse_eft(bad_eft), eft_text=bad_eft
        )
        self.client.force_login(self.reviewer)
        self.client.post(
            reverse("fitcheck:review_decide", args=[submission.pk]),
            {"decision": "reject", "comment": ""},
        )
        submission.refresh_from_db()
        self.assertEqual(submission.status, FitSubmission.Status.PENDING)


class TestSubmitEftDoctrineSelector(ViewTestCase):
    """The submit/test-bench page offers a doctrine selector; choosing one or
    more doctrines fans out into one submission per (fit, doctrine), each
    graded against that doctrine's policy snapshot."""

    def setUp(self):
        super().setUp()
        from ..services.assignments import attach_fit_to_doctrine

        self.second = create_doctrine(name="Second Doctrine")
        attach_fit_to_doctrine(self.fit, self.doctrine, user=self.manager)
        attach_fit_to_doctrine(self.fit, self.second, user=self.manager)

    def test_selector_lists_the_fits_doctrines(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("fitcheck:submit_eft", args=[self.fit.pk]))
        self.assertContains(response, "Grade against")
        self.assertContains(response, "Second Doctrine")

    def test_multiple_doctrines_fan_out(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:submit_eft", args=[self.fit.pk]),
            {
                "eft_text": EFT_GOOD,
                "doctrines": [str(self.doctrine.pk), str(self.second.pk)],
            },
        )
        self.assertEqual(response.status_code, 200)  # per-doctrine results table
        subs = FitSubmission.objects.filter(user=self.manager, doctrine_fit=self.fit)
        self.assertEqual(subs.count(), 2)
        self.assertEqual(
            {s.doctrine_id for s in subs}, {self.doctrine.pk, self.second.pk}
        )

    def test_single_doctrine_redirects_to_detail(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:submit_eft", args=[self.fit.pk]),
            {"eft_text": EFT_GOOD, "doctrines": [str(self.doctrine.pk)]},
        )
        self.assertEqual(response.status_code, 302)
        sub = FitSubmission.objects.filter(user=self.manager).latest("created_at")
        self.assertEqual(sub.doctrine_id, self.doctrine.pk)

    def test_no_doctrine_grades_source_defaults(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:submit_eft", args=[self.fit.pk]),
            {"eft_text": EFT_GOOD},
        )
        self.assertEqual(response.status_code, 302)
        sub = FitSubmission.objects.filter(user=self.manager).latest("created_at")
        self.assertIsNone(sub.doctrine)


class TestPageRenderSmoke(ViewTestCase):
    """Every page in the reworked UI renders without template errors."""

    def test_member_pages(self):
        self.client.force_login(self.member)
        for name, args in [
            ("index", []),
            ("doctrine_detail", [self.doctrine.pk]),
            ("fit_detail", [self.fit.pk]),
            ("pilot_fittings", []),
            ("ship_inventory", []),  # no ESI tokens -> empty inventory page
        ]:
            response = self.client.get(reverse(f"fitcheck:{name}", args=args))
            self.assertEqual(response.status_code, 200, name)

    def test_manager_pages(self):
        self.client.force_login(self.manager)
        for name, args in [
            ("doctrine_create", []),
            ("standards_list", []),
            ("standard_import", []),
            ("manage_fit_import", [self.doctrine.pk]),
            ("manage_fit_settings", [self.fit.pk]),
            ("manage_fit_items", [self.fit.pk]),
            ("submit_eft", [self.fit.pk]),
        ]:
            response = self.client.get(reverse(f"fitcheck:{name}", args=args))
            self.assertEqual(response.status_code, 200, name)
        response = self.client.get(
            reverse("fitcheck:doctrine_create"), {"mode": "direct"}
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.get(reverse("fitcheck:ship_search"), {"q": "Harb"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["results"])

    def test_policy_pages(self):
        admin = create_user("padmin2", permissions=["basic_access", "manage_policies"])
        self.client.force_login(admin)
        for name in ("policy_list", "policy_create"):
            response = self.client.get(reverse(f"fitcheck:{name}"))
            self.assertEqual(response.status_code, 200, name)


class TestPolicyEditorViews(ViewTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.policy_admin = create_user(
            "padmin", permissions=["basic_access", "manage_policies"]
        )

    def test_policy_editor_is_admin_only(self):
        for blocked in (self.member, self.manager, self.reviewer, self.secure_group_admin):
            self.client.force_login(blocked)
            self.assertEqual(
                self.client.get(reverse("fitcheck:policy_list")).status_code, 302
            )
        self.client.force_login(self.policy_admin)
        self.assertEqual(self.client.get(reverse("fitcheck:policy_list")).status_code, 200)

    def test_create_policy_with_slot_rules(self):
        self.client.force_login(self.policy_admin)
        data = {
            "name": "Strict Highs",
            "description": "",
            "HIGH-enforcement": "EX",
            "MED-enforcement": "GE",
            "MED-allow_mutated": "on",
            "LOW-enforcement": "ME",
            "LOW-min_meta_level": "5",
            "CARGO-enforcement": "AN",
            "CARGO-min_quantity_pct": "66",
        }
        response = self.client.post(reverse("fitcheck:policy_create"), data, follow=True)
        self.assertEqual(response.status_code, 200)

        from ..models import CompliancePolicy

        policy = CompliancePolicy.objects.get(name="Strict Highs")
        rules = {rule.section: rule for rule in policy.rules.all()}
        self.assertEqual(set(rules), {"HIGH", "MED", "LOW", "CARGO"})
        self.assertEqual(rules["HIGH"].enforcement, "EX")
        self.assertEqual(rules["LOW"].min_meta_level, 5)
        self.assertEqual(rules["CARGO"].enforcement, "AN")


class TestBuiltinPolicies(ViewTestCase):
    """Pre-built (seeded) policies are flagged is_builtin, editable/deletable only
    by superusers, and still selectable for applying to a fit."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.policy_admin = create_user(
            "builtin_padmin", permissions=["basic_access", "manage_policies"]
        )
        su = create_user("builtin_super", permissions=["basic_access"])
        su.is_superuser = True
        su.is_staff = True
        su.save()
        from django.contrib.auth.models import User
        cls.superuser = User.objects.get(pk=su.pk)  # refresh perm cache

    def _a_builtin(self):
        from ..models import CompliancePolicy
        return CompliancePolicy.objects.filter(is_builtin=True).first()

    def test_four_builtins_seeded_with_rules(self):
        from ..models import CompliancePolicy
        builtins = CompliancePolicy.objects.filter(is_builtin=True)
        self.assertEqual(
            set(builtins.values_list("name", flat=True)),
            {"Strict", "Standard", "Flexible", "No Enforcement"},
        )
        for p in builtins:
            self.assertEqual(p.rules.count(), 10)

    def test_manager_cannot_edit_or_delete_builtin(self):
        builtin = self._a_builtin()
        self.client.force_login(self.policy_admin)
        self.assertEqual(
            self.client.get(reverse("fitcheck:policy_edit", args=[builtin.pk])).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse("fitcheck:policy_delete", args=[builtin.pk])).status_code,
            403,
        )
        from ..models import CompliancePolicy
        self.assertTrue(CompliancePolicy.objects.filter(pk=builtin.pk).exists())

    def test_superuser_can_edit_builtin(self):
        builtin = self._a_builtin()
        self.client.force_login(self.superuser)
        self.assertEqual(
            self.client.get(reverse("fitcheck:policy_edit", args=[builtin.pk])).status_code,
            200,
        )

    def test_superuser_also_cannot_delete_builtin(self):
        """Built-ins can never be deleted - not even by a superuser. Disable instead."""
        builtin = self._a_builtin()
        self.client.force_login(self.superuser)
        self.assertEqual(
            self.client.post(reverse("fitcheck:policy_delete", args=[builtin.pk])).status_code,
            403,
        )
        from ..models import CompliancePolicy
        self.assertTrue(CompliancePolicy.objects.filter(pk=builtin.pk).exists())

    def test_manager_can_disable_and_enable_builtin(self):
        """A built-in can't be deleted but a manager may disable/re-enable it."""
        from ..models import CompliancePolicy

        builtin = self._a_builtin()
        self.client.force_login(self.policy_admin)
        self.client.post(reverse("fitcheck:policy_toggle_disabled", args=[builtin.pk]))
        builtin.refresh_from_db()
        self.assertTrue(builtin.is_disabled)
        self.client.post(reverse("fitcheck:policy_toggle_disabled", args=[builtin.pk]))
        builtin.refresh_from_db()
        self.assertFalse(builtin.is_disabled)

    def test_disabled_policy_not_offered_in_apply_form(self):
        from ..forms import ApplyPolicyForm
        from ..models import CompliancePolicy

        builtin = self._a_builtin()
        self.client.force_login(self.policy_admin)
        self.client.post(reverse("fitcheck:policy_toggle_disabled", args=[builtin.pk]))
        names = set(
            ApplyPolicyForm().fields["policy"].queryset.values_list("name", flat=True)
        )
        self.assertNotIn(builtin.name, names)
        # Re-enable and confirm it's offered again.
        self.client.post(reverse("fitcheck:policy_toggle_disabled", args=[builtin.pk]))
        names = set(
            ApplyPolicyForm().fields["policy"].queryset.values_list("name", flat=True)
        )
        self.assertIn(builtin.name, names)

    def test_member_cannot_toggle_disabled(self):
        builtin = self._a_builtin()
        self.client.force_login(self.member)
        self.assertEqual(
            self.client.post(
                reverse("fitcheck:policy_toggle_disabled", args=[builtin.pk])
            ).status_code,
            302,
        )
        builtin.refresh_from_db()
        self.assertFalse(builtin.is_disabled)

    def test_manager_can_still_edit_a_custom_policy(self):
        """The built-in guard must not block ordinary custom-policy editing."""
        from ..models import CompliancePolicy
        custom = CompliancePolicy.objects.create(name="My Custom", is_builtin=False)
        self.client.force_login(self.policy_admin)
        self.assertEqual(
            self.client.get(reverse("fitcheck:policy_edit", args=[custom.pk])).status_code,
            200,
        )

    def test_builtins_offered_in_apply_policy_form(self):
        from ..forms import ApplyPolicyForm
        names = set(
            ApplyPolicyForm().fields["policy"].queryset.values_list("name", flat=True)
        )
        self.assertTrue({"Strict", "Standard", "Flexible", "No Enforcement"} <= names)


class FebFieldVisibilityTests(ViewTestCase):
    """The Frigate Escape Bay picker only renders for hulls that carry a FEB
    (battleship-class). Every other hull drops the field entirely so it neither
    shows nor accepts a value."""

    def test_field_hidden_for_non_feb_hull(self):
        from ..forms import FitSettingsForm
        # self.fit is a Harbinger (fixture group 60) - no Frigate Escape Bay.
        form = FitSettingsForm(instance=self.fit)
        self.assertNotIn("feb_frigate_type_ids", form.fields)

    def test_field_shown_with_renamed_label_for_battleship(self):
        from ..forms import FitSettingsForm
        bs_fit = create_fit(self.doctrine, T.NIGHTMARE, name="Nightmare DPS")
        form = FitSettingsForm(instance=bs_fit)
        self.assertIn("feb_frigate_type_ids", form.fields)
        self.assertEqual(
            str(form.fields["feb_frigate_type_ids"].label),
            "Frigate Escape Bay - Allowed",
        )

    def test_settings_page_omits_picker_for_non_feb_hull(self):
        # The "data-feb-picker" selector lives in the page's static <script>, so we
        # assert on the actual rendered field/label, which the form removed.
        self.client.force_login(self.manager)
        resp = self.client.get(
            reverse("fitcheck:manage_fit_settings", args=[self.fit.pk])
        )
        self.assertNotContains(resp, 'name="feb_frigate_type_ids"')
        self.assertNotContains(resp, "Frigate Escape Bay - Allowed")

    def test_settings_page_shows_picker_for_battleship(self):
        bs_fit = create_fit(self.doctrine, T.NIGHTMARE, name="Nightmare DPS")
        self.client.force_login(self.manager)
        resp = self.client.get(
            reverse("fitcheck:manage_fit_settings", args=[bs_fit.pk])
        )
        self.assertContains(resp, 'name="feb_frigate_type_ids"')
        self.assertContains(resp, "Frigate Escape Bay - Allowed")

    def test_non_feb_hull_save_ignores_posted_feb_ids(self):
        """A crafted POST of feb ids to a non-FEB hull is dropped, not saved."""
        self.client.force_login(self.manager)
        self.client.post(
            reverse("fitcheck:manage_fit_settings", args=[self.fit.pk]),
            {
                "name": self.fit.name,
                "description": "",
                "is_active": "on",
                "default_policy": self.fit.default_policy,
                "feb_frigate_type_ids": [str(T.ORACLE)],
            },
            follow=True,
        )
        self.fit.refresh_from_db()
        self.assertEqual(self.fit.feb_frigate_type_ids or [], [])
