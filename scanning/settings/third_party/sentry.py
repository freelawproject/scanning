import environ
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration

from ..project.testing import TESTING

env = environ.FileAwareEnv()
SENTRY_DSN = env("SENTRY_DSN", default="")
GIT_SHA = env("GIT_SHA", default="")

# Skip Sentry during test runs so intentional logger.exception calls in
# tests don't spam the project with bogus events.
if SENTRY_DSN and not TESTING:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        release=GIT_SHA or None,
        integrations=[
            DjangoIntegration(),
        ],
        ignore_errors=[KeyboardInterrupt],
        attach_stacktrace=True,
    )
