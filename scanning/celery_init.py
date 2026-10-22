"""The Celery app (#503).

The module is ``celery_init`` and not ``celery``, and the package's
``__init__`` imports the app, the way CourtListener's ``cl`` package
does, so a ``shared_task`` anywhere binds to this app and the worker
is ``celery -A scanning worker``.

There is no beat: ``run_daemon`` is the clock, as CourtListener's
periodic work is a management command and never a beat schedule. With
``CELERY_ENABLED`` on, the daemon sends the tick tasks of
``scanning.tasks.TICKS`` on their intervals instead of running the
passes itself, so the one daemon deployment stays, its shutdown rules
stay, and the workers are the only new deployments.

The queues (``scanning.tasks``):

- ``fast``: polls, glues, promotions, the RunPod waves. Seconds.
- ``io``: the doctor wave, which holds a socket per conversion.
- ``heavy``: the scan actions (shard, apply, compute, create opinions),
  the Mistral render and the bad-page score. Minutes, and the only
  queue that scales by replica count.
- ``opinions``: the opinion OCR glue, the ensemble, the PDFs and the
  figure cuts, which share the local mirror of a volume and so run on
  one pod.
"""

import os

from celery import Celery
from celery.signals import setup_logging

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "scanning.settings")

app = Celery("scanning")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["scanning"])


@setup_logging.connect
def _leave_logging_to_django(**kwargs) -> None:
    """Keep Celery's handler off the root logger.

    A receiver on this signal is what tells Celery not to configure
    logging at all; ``worker_hijack_root_logger=False`` alone only
    keeps Django's handlers, beside Celery's own, and every record is
    then written twice. Django's ``LOGGING`` is the one configuration.

    :param kwargs: The signal's arguments, unused.
    :return: None.
    """
