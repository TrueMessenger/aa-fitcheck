"""Dev/test settings: Alliance Auth template base plus this app, sqlite and no redis."""

import os

from allianceauth.project_template.project_name.settings.base import *  # noqa: F401,F403

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WSGI_APPLICATION = "testauth.wsgi.application"

INSTALLED_APPS += [  # noqa: F405
    "eveuniverse",
    "app_utils",
    # Optional soft dependency - present in the dev/test site so the Secure
    # Groups smart filter (FitComplianceFilter) is exercised by the suite.
    "securegroups",
    "fitcheck",
    # Test-only stub providing app_label "corptools" so corptools_source's real
    # ORM reads are exercised by the suite (no real aa-corptools in dev/test).
    "fitcheck.tests.testdata.fake_corptools.apps.FakeCorptoolsConfig",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.path.join(BASE_DIR, "testauth.sqlite3"),
    }
}

# In-memory redis so AA's startup redis checks and cache usage work without a server.
import fakeredis  # noqa: E402
import redis  # noqa: E402

# fakeredis has no INFO command; AA's system checks need redis_version from it.
_real_info = redis.Redis.info


def _info_with_fallback(self, *args, **kwargs):
    try:
        return _real_info(self, *args, **kwargs)
    except redis.exceptions.ResponseError:
        return {"redis_version": "7.4.0"}


redis.Redis.info = _info_with_fallback

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/1",
        "OPTIONS": {
            "CONNECTION_POOL_KWARGS": {"connection_class": fakeredis.FakeConnection},
        },
    }
}
SESSION_ENGINE = "django.contrib.sessions.backends.db"

CELERY_ALWAYS_EAGER = True
CELERY_TASK_ALWAYS_EAGER = True
CELERY_EAGER_PROPAGATES_EXCEPTIONS = True

# Real values come from env vars (set them before runserver); dummies keep tests working.
ESI_SSO_CLIENT_ID = os.environ.get("ESI_SSO_CLIENT_ID", "dummy")
ESI_SSO_CLIENT_SECRET = os.environ.get("ESI_SSO_CLIENT_SECRET", "dummy")
ESI_SSO_CALLBACK_URL = os.environ.get(
    "ESI_SSO_CALLBACK_URL", "http://localhost:8000/sso/callback/"
)
ESI_USER_CONTACT_EMAIL = os.environ.get("ESI_USER_CONTACT_EMAIL", "dev@example.com")

# Manifest storage needs collectstatic; use plain storage for dev/tests.
STORAGES["staticfiles"]["BACKEND"] = (  # noqa: F405
    "django.contrib.staticfiles.storage.StaticFilesStorage"
)

DEBUG = True
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False
SITE_URL = "http://localhost:8000"
CSRF_TRUSTED_ORIGINS = [SITE_URL]

# No SMTP server in dev: skip email verification, print any mail to the console.
REGISTRATION_VERIFY_EMAIL = False
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "fitcheck": {"handlers": ["console"], "level": "INFO"},
    },
}
