"""Tests for the Secure Groups smart filter (FitComplianceFilter).

Runs only meaningfully with allianceauth-securegroups installed; the dev/test
site (testauth) installs it, so the suite exercises the real filter.
"""

from datetime import timedelta

from django.contrib.admin.sites import site as admin_site
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from ..constants import Section
from ..models import FitSubmission
from ..models.securegroups import SECUREGROUPS_INSTALLED
from ..services.assignments import attach_fit_to_doctrine
from ..services.check_runner import review_submission, submit_fit
from ..services.eft_parser import parse_eft
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata

COMPLIANT_EFT = "[Harbinger, Mine]\nHeat Sink II\nHeat Sink II\nHeat Sink II\n"
SHORT_EFT = "[Harbinger, Mine]\nHeat Sink II\n"


class SecureGroupsInstalledTest(TestCase):
    def test_securegroups_is_installed_in_test_env(self):
        # Guards against silently skipping the filter tests if the optional
        # package ever drops out of the test environment.
        self.assertTrue(SECUREGROUPS_INSTALLED)


class FilterTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(None, T.HARBINGER, name="Armor Brawl")
        add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        attach_fit_to_doctrine(cls.fit, cls.doctrine)
        cls.pilot = create_user("pilot")
        cls.other = create_user("other")
        cls.reviewer = create_user(
            "reviewer", permissions=["basic_access", "review_submissions"]
        )

    def _filter(self, **kwargs):
        from ..models import FitComplianceFilter

        kwargs.setdefault("name", "Doctrine compliant")
        kwargs.setdefault("description", "Compliant with the doctrine")
        kwargs.setdefault("doctrine", self.doctrine)
        return FitComplianceFilter.objects.create(**kwargs)

    def _submit(self, user, eft=COMPLIANT_EFT):
        return submit_fit(
            user, self.fit, parse_eft(eft), eft_text=eft, doctrine=self.doctrine
        )


class ProcessFilterTests(FilterTestCase):
    def test_compliant_user_passes(self):
        self._submit(self.pilot)
        self.assertTrue(self._filter().process_filter(self.pilot))

    def test_non_compliant_user_fails(self):
        self._submit(self.pilot, SHORT_EFT)
        self.assertFalse(self._filter().process_filter(self.pilot))

    def test_user_with_no_submission_fails(self):
        self.assertFalse(self._filter().process_filter(self.other))

    def test_fit_target_filter_passes(self):
        self._submit(self.pilot)
        self.assertTrue(self._filter(doctrine=None, fit=self.fit).process_filter(self.pilot))

    def test_require_approved_gate(self):
        sub = self._submit(self.pilot)
        f = self._filter(require_approved=True)
        self.assertFalse(f.process_filter(self.pilot))
        review_submission(sub, self.reviewer, approve=True)
        self.assertTrue(f.process_filter(self.pilot))


class AuditFilterTests(FilterTestCase):
    def test_audit_filter_shape_and_results(self):
        self._submit(self.pilot)  # compliant
        self._submit(self.other, SHORT_EFT)  # non-compliant
        users = User.objects.filter(pk__in=[self.pilot.pk, self.other.pk])
        out = self._filter().audit_filter(users)

        self.assertTrue(out[self.pilot.pk]["check"])
        self.assertFalse(out[self.other.pk]["check"])
        # Compliant entry carries the verdict label; non-compliant is blank.
        self.assertEqual(
            out[self.pilot.pk]["message"], FitSubmission.Verdict.COMPLIANT.label
        )
        self.assertEqual(out[self.other.pk]["message"], "")

    def test_audit_filter_defaults_unknown_users_to_fail(self):
        users = User.objects.filter(pk__in=[self.other.pk])
        out = self._filter().audit_filter(users)
        # defaultdict: an unseen user id still answers False.
        self.assertFalse(out[999999]["check"])


class GrandfatherWindowTests(FilterTestCase):
    def test_future_enforce_from_passes_non_compliant_user(self):
        self._submit(self.pilot, SHORT_EFT)
        f = self._filter(enforce_from=timezone.now() + timedelta(days=90))
        self.assertTrue(f.process_filter(self.pilot))

    def test_future_enforce_from_passes_user_with_no_submission(self):
        f = self._filter(enforce_from=timezone.now() + timedelta(days=90))
        self.assertTrue(f.process_filter(self.other))

    def test_future_enforce_from_audit_all_check_true(self):
        self._submit(self.pilot)  # compliant
        self._submit(self.other, SHORT_EFT)  # non-compliant
        f = self._filter(enforce_from=timezone.now() + timedelta(days=90))
        users = User.objects.filter(pk__in=[self.pilot.pk, self.other.pk])
        out = f.audit_filter(users)

        self.assertTrue(out[self.pilot.pk]["check"])
        self.assertEqual(
            out[self.pilot.pk]["message"], FitSubmission.Verdict.COMPLIANT.label
        )
        self.assertTrue(out[self.other.pk]["check"])
        self.assertIn("Grandfathered until", out[self.other.pk]["message"])
        # defaultdict path: a user id iter_user_compliance never returned a
        # result for still has to pass during the window.
        self.assertTrue(out[999999]["check"])
        self.assertIn("Grandfathered until", out[999999]["message"])

    def test_past_enforce_from_behaves_like_no_window(self):
        self._submit(self.pilot, SHORT_EFT)
        self._submit(self.other)
        f = self._filter(enforce_from=timezone.now() - timedelta(days=1))
        self.assertFalse(f.process_filter(self.pilot))
        self.assertTrue(f.process_filter(self.other))

    def test_enforce_from_none_is_unchanged(self):
        self._submit(self.pilot, SHORT_EFT)
        f = self._filter(enforce_from=None)
        self.assertFalse(f.process_filter(self.pilot))


class AdminRegistrationTests(FilterTestCase):
    def test_filter_is_registered_in_admin(self):
        from ..models import FitComplianceFilter

        self.assertIn(FitComplianceFilter, admin_site._registry)


class ValidationTests(FilterTestCase):
    def test_clean_requires_a_target(self):
        from ..models import FitComplianceFilter

        f = FitComplianceFilter(name="x", description="y")
        with self.assertRaises(ValidationError):
            f.clean()


class HookTests(FilterTestCase):
    def test_filter_is_registered_with_securegroups(self):
        from allianceauth import hooks

        from ..models import FitComplianceFilter

        registered = []
        for hook in hooks.get_hooks("secure_group_filters"):
            registered.extend(hook())
        self.assertIn(FitComplianceFilter, registered)
