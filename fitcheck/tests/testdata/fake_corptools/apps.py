from django.apps import AppConfig


class FakeCorptoolsConfig(AppConfig):
    """Registers the stub models under app_label ``corptools`` so
    ``django.apps.apps.get_model("corptools", ...)`` (used by
    ``fitcheck.services.corptools_source``) resolves to them and the real ORM
    filtering is exercised by the test suite. There is no real aa-corptools in
    the dev/test site."""

    name = "fitcheck.tests.testdata.fake_corptools"
    label = "corptools"
    verbose_name = "Corp Tools (test stub)"
    default_auto_field = "django.db.models.BigAutoField"
