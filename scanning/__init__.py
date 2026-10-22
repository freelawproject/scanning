# Import the Celery app when Django starts, so a ``shared_task`` binds
# to it and ``celery -A scanning`` finds it (#503). The same line as
# CourtListener's ``cl/__init__.py``.
from .celery_init import app as celery_app  # noqa: F401
