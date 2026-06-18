from django.db import models


class General(models.Model):
    """Holder for app-level permissions only - never instantiated."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("basic_access", "Can access Fit Check"),
            ("manage_doctrines", "Can manage doctrines and fitting standards"),
            ("review_submissions", "Can review fit submissions"),
            (
                "secure_group_management",
                "Secure Group Doctrine Management - can view and decide on submitted "
                "ships but cannot create doctrines or change fitting standards",
            ),
            ("manage_policies", "Plugin admin - can manage compliance policies"),
            ("view_compliance_reports", "Can view org-wide compliance reports"),
            (
                "view_member_inventory",
                "Can browse alliance members' ships and run proactive fit checks",
            ),
            (
                "view_own_corp_inventory",
                "Can browse own-corporation members' ships and run proactive fit "
                "checks (scoped to the user's main character's corporation)",
            ),
        )
