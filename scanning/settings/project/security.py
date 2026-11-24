import socket

import environ

from ..django import (
    DATABASES,
    DEVELOPMENT,
    INSTALLED_APPS,
    MIDDLEWARE,
    TESTING,
)

env = environ.FileAwareEnv()

ALLOWED_HOSTS: list[str] = env(
    "ALLOWED_HOSTS", default=["localhost", "127.0.0.1"]
)

SECURE_HSTS_SECONDS = 63_072_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"

if DEVELOPMENT:
    X_FRAME_OPTIONS = "SAMEORIGIN"
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    SESSION_COOKIE_DOMAIN = None
    if not TESTING:
        INSTALLED_APPS.append("debug_toolbar")
        MIDDLEWARE.append("debug_toolbar.middleware.DebugToolbarMiddleware")
        # Without the cache panel: under the async dev server it wraps the
        # cache again on every request (debug-toolbar 6.2), and a page
        # that reads the cache dies of RecursionError after a few dozen.
        DEBUG_TOOLBAR_CONFIG = {
            "DISABLE_PANELS": {
                "debug_toolbar.panels.redirects.RedirectsPanel",
                "debug_toolbar.panels.profiling.ProfilingPanel",
                "debug_toolbar.panels.cache.CachePanel",
            },
        }

    try:
        hostname, _, ips = socket.gethostbyname_ex(socket.gethostname())
        INTERNAL_IPS = [".".join(ip.split(".")[:-1] + ["1"]) for ip in ips] + [
            "127.0.0.1"
        ]
    except socket.gaierror:
        INTERNAL_IPS = ["127.0.0.1"]

    if TESTING:
        db = DATABASES["default"]
        db["ENCODING"] = "UTF8"
        db["TEST_ENCODING"] = "UTF8"
        db["CONN_MAX_AGE"] = 0
else:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
