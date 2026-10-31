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

- ``fast``: polls, the RunPod waves, the sweeps. Seconds, and never
  a whole volume in memory.
- ``io``: the doctor wave, which holds a socket per conversion.
- ``heavy``: the scan actions (shard, apply, compute, create opinions),
  the volume glues and the promotions (``glue_runs``, which holds a
  volume's shard results in memory), the Mistral render and the
  bad-page score. Minutes, and the only queue that scales by replica
  count.
- ``opinions``: the opinion OCR glue, the ensemble, the PDFs and the
  figure cuts, which share the local mirror of a volume and so run on
  one pod.
"""

import os

from celery import Celery
from celery.signals import setup_logging, worker_init, worker_shutting_down

from scanning import stopping
from scanning.worker_heartbeat import Heartbeat

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "scanning.settings")

app = Celery("scanning")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["scanning"])
# Each worker writes a key on the broker while it lives, which the
# monitoring view reads (``scanning.worker_heartbeat``).
app.steps["consumer"].add(Heartbeat)


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


@worker_init.connect
def _share_the_stop_flag(**kwargs) -> None:
    """Make the stop flag one the pool's children read (#534).

    ``worker_init`` runs in the main process before the pool forks, so
    every child, a recycled one too, inherits the shared flag.

    :param kwargs: The signal's arguments, unused.
    :return: None.
    """
    stopping.share()


@worker_shutting_down.connect
def _ask_the_tasks_to_stop(**kwargs) -> None:
    """Set the stop flag when the worker is told to shut down (#534).

    The signal reaches the main process alone; a task in a child reads
    the shared flag between its parts and hands its row back before the
    grace period ends.

    :param kwargs: The signal's arguments, unused.
    :return: None.
    """
    stopping.request()
