"""Secure Groups smart filter (optional soft dependency).

Lets an admin auto-manage an Alliance Auth group from doctrine compliance: a
user belongs to the group only while they are compliant with a chosen doctrine
(any of its fits) or a specific fit, evaluated through the public compliance API
(`fitcheck.services.api`). This realises roadmap item "Secure Groups smart
filter" / decision 21.

``allianceauth-securegroups`` is an **optional** dependency (install the
``securegroups`` extra). When it is absent this module defines nothing, the
``secure_group_filters`` hook is not registered (see ``auth_hooks``), and the
rest of fitcheck is unaffected. The matching migration is likewise gated on the
package being installed, so a securegroups-less site stays migration-consistent.
"""

from __future__ import annotations

from django.apps import apps as django_apps

# Whether the optional Secure Groups package is installed. Gates the model below
# AND its migration (migration 0026) so both worlds stay consistent.
SECUREGROUPS_INSTALLED = django_apps.is_installed("securegroups")

__all__ = ["SECUREGROUPS_INSTALLED"]


if SECUREGROUPS_INSTALLED:
    from collections import defaultdict

    from django.contrib.auth.models import User
    from django.core.exceptions import ValidationError
    from django.db import models
    from django.utils.translation import gettext_lazy as _
    from securegroups.models import FilterBase

    class FitComplianceFilter(FilterBase):
        """Secure Groups filter: a user passes while compliant with the
        configured doctrine and/or fit. Backed by ``services.api``."""

        class Meta:
            app_label = "fitcheck"
            verbose_name = _("Smart Filter: Fit Compliance")
            verbose_name_plural = verbose_name

        doctrine = models.ForeignKey(
            "fitcheck.Doctrine",
            null=True,
            blank=True,
            on_delete=models.CASCADE,
            related_name="+",
            help_text=_("Require compliance with any one fit graded under this doctrine."),
        )
        fit = models.ForeignKey(
            "fitcheck.DoctrineFit",
            null=True,
            blank=True,
            on_delete=models.CASCADE,
            related_name="+",
            help_text=_("Require compliance with this specific fitting standard."),
        )
        require_approved = models.BooleanField(
            default=False,
            help_text=_(
                "Require a reviewer-approved submission, not just a passing engine verdict."
            ),
        )
        require_current = models.BooleanField(
            default=True,
            help_text=_(
                "Require the submission to be graded against the fit's current "
                "version (ignore stale submissions)."
            ),
        )

        def clean(self):
            if self.doctrine_id is None and self.fit_id is None:
                raise ValidationError(
                    _("Pick a doctrine and/or a fit to require compliance with.")
                )

        def process_filter(self, user: User) -> bool:
            from ..services import api

            return api.is_user_compliant(
                user,
                doctrine=self.doctrine,
                fit=self.fit,
                require_approved=self.require_approved,
                require_current=self.require_current,
            )

        def audit_filter(self, users):
            from ..services import api

            out = defaultdict(lambda: {"message": "", "check": False})
            for result in api.iter_user_compliance(
                users,
                doctrine=self.doctrine,
                fit=self.fit,
                require_approved=self.require_approved,
                require_current=self.require_current,
            ):
                out[result.user_id] = {
                    "message": result.submission.get_verdict_display() if result.submission else "",
                    "check": result.is_compliant,
                }
            return out

    __all__.append("FitComplianceFilter")
