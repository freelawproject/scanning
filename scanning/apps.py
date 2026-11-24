from django.apps import AppConfig


class ScanningConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "scanning"

    def ready(self) -> None:
        # The login signal that stamps a volunteer's session.
        from scanning import volunteers  # noqa: F401
