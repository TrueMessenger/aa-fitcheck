from django.apps import AppConfig

from . import __version__


class FitcheckConfig(AppConfig):
    name = "fitcheck"
    label = "fitcheck"
    verbose_name = f"Fit Check v{__version__}"
    default_auto_field = "django.db.models.AutoField"
