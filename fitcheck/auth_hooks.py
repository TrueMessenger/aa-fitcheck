from django.utils.translation import gettext_lazy as _

from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

from . import urls


class FitcheckMenuItem(MenuItemHook):
    def __init__(self):
        super().__init__(
            text=_("Fit Check"),
            classes="fa-solid fa-clipboard-check",
            url_name="fitcheck:index",
            order=1100,
            navactive=["fitcheck:"],
        )

    def render(self, request):
        if request.user.has_perm("fitcheck.basic_access"):
            return super().render(request)
        return ""


@hooks.register("menu_item_hook")
def register_menu():
    return FitcheckMenuItem()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "fitcheck", r"^fitcheck/")


from .models.securegroups import SECUREGROUPS_INSTALLED  # noqa: E402

if SECUREGROUPS_INSTALLED:
    # Only offer the compliance filter to Secure Groups when the optional
    # allianceauth-securegroups package is installed (see models/securegroups.py).
    @hooks.register("secure_group_filters")
    def fitcheck_secure_group_filters():
        from .models.securegroups import FitComplianceFilter

        return [FitComplianceFilter]
