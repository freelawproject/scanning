import environ

env = environ.FileAwareEnv()
DEVELOPMENT = env.bool("DEVELOPMENT", default=True)

# Log level for the top-level ``scanning`` logger. Default INFO.
# Set SCANNING_LOG_LEVEL=DEBUG to see per-poll traces from the
# RunPod client (``poll runpod job <id> -> IN_QUEUE`` every few
# seconds) and other verbose scanning-side debug output.
SCANNING_LOG_LEVEL = env.str("SCANNING_LOG_LEVEL", default="INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": '%(levelname)s %(asctime)s (%(pathname)s %(funcName)s): "%(message)s"'
        },
        "simple": {"format": "%(levelname)s %(message)s"},
        "django.server": {
            "()": "django.utils.log.ServerFormatter",
            "format": "[%(server_time)s] %(message)s",
        },
    },
    "handlers": {
        "null": {"level": "DEBUG", "class": "logging.NullHandler"},
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "django.server": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "django.server",
        },
    },
    "loggers": {
        "django.security.DisallowedHost": {
            "handlers": ["null"],
            "propagate": False,
        },
        "django.server": {
            "handlers": ["django.server"],
            "level": "INFO",
            "propagate": False,
        },
        "scanning": {
            "handlers": ["console"],
            "level": SCANNING_LOG_LEVEL,
            "propagate": True,
        },
    },
}

if DEVELOPMENT:
    LOGGING["handlers"]["console"]["formatter"] = "verbose"
