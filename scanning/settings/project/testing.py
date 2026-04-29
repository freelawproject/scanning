import sys
import tempfile

import environ

env = environ.FileAwareEnv()

TESTING = "test" in sys.argv
if TESTING:
    DEBUG = env.bool("TESTING_DEBUG", default=False)
    PASSWORD_HASHERS = [
        "django.contrib.auth.hashers.MD5PasswordHasher",
    ]
    # Isolate the processing cache so tests don't pollute each other
    # (or the developer's real /tmp/scanning) with stray output dirs.
    PROCESSING_TMP_DIR = tempfile.mkdtemp(prefix="scanning-test-")
