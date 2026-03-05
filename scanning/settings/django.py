from pathlib import Path

import environ

from .project.testing import TESTING

env = environ.FileAwareEnv()

DEVELOPMENT = env.bool("DEVELOPMENT", default=False)

# SECURITY: no default in production. Django will refuse to start if
# the SECRET_KEY env var is missing, preventing accidental use of a
# weak key.
if DEVELOPMENT:
    SECRET_KEY = env(
        "SECRET_KEY", default="dev-only-insecure-key-not-for-production"
    )
else:
    SECRET_KEY = env("SECRET_KEY")

############
# Database #
############
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", default="scanning"),
        "USER": env("DB_USER", default="postgres"),
        "PASSWORD": env("DB_PASSWORD", default="postgres"),
        "CONN_MAX_AGE": 0,
        "HOST": env("DB_HOST", default="scanning-postgres"),
        "OPTIONS": {
            "sslmode": env("DB_SSL_MODE", default="require"),
        },
    },
}

####################
# Cache & Sessions #
####################
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
        "OPTIONS": {"MAX_ENTRIES": 25_000},
    },
}

SESSION_ENGINE = "django.contrib.sessions.backends.db"

#####################################
# Directories, Apps, and Middleware #
#####################################
INSTALL_ROOT = Path(__file__).resolve().parents[2]
STATICFILES_DIRS = (INSTALL_ROOT / "scanning/assets/static-global/",)
DEBUG = env.bool("DEBUG", default=False)
MEDIA_ROOT = env(
    "MEDIA_ROOT", default=str(INSTALL_ROOT / "scanning/assets/media/")
)
MEDIA_URL = "/media/"
STATIC_URL = env.str("STATIC_URL", default="/static/")
STATIC_ROOT = INSTALL_ROOT / "scanning/assets/static/"
TEMPLATE_ROOT = INSTALL_ROOT / "scanning/assets/templates/"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
}

if not any([TESTING, DEBUG]):
    STORAGES["default"] = {
        "BACKEND": "scanning.storage.PrivateS3Storage",
    }
    STORAGES["staticfiles"] = {
        "BACKEND": "scanning.storage.SubDirectoryS3ManifestStaticStorage",
    }
else:
    STORAGES["staticfiles"] = {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    }

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            str(TEMPLATE_ROOT),
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": (
                "django.contrib.messages.context_processors.messages",
                "django.contrib.auth.context_processors.auth",
                "django.template.context_processors.request",
                "django.template.context_processors.static",
                "scanning.context_processors.inject_settings",
            ),
            "debug": DEBUG,
        },
    }
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "scanning.urls"

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.humanize",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "storages",
    "django_cotton",
    "scanning",
    "tailwind",
]

if DEVELOPMENT:
    INSTALLED_APPS.append("django_extensions")

    # used to hot reload css changes
    INSTALLED_APPS.append("django_browser_reload")
    MIDDLEWARE.append(
        "django_browser_reload.middleware.BrowserReloadMiddleware"
    )

TAILWIND_APP_NAME = "scanning"
TAILWIND_CSS_PATH = "css/tailwind_styles.css"

ASGI_APPLICATION = "scanning.asgi.application"

################
# Misc. Django #
################
SITE_ID = 1
USE_I18N = False
DEFAULT_CHARSET = "utf-8"
LANGUAGE_CODE = "en-us"
USE_TZ = True
DATETIME_FORMAT = "N j, Y, P e"

TIME_ZONE = env("TIMEZONE", default="America/Los_Angeles")

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Map Django message level tags to our CSS class names
from django.contrib.messages import (  # noqa: E402
    constants as message_constants,
)

MESSAGE_TAGS = {
    message_constants.ERROR: "danger",
}

if TESTING:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
