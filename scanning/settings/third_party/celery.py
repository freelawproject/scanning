"""Celery (#503).

The daemon's passes become Celery tasks, and the database stays the
queue of record: every task is a poll over the rows or a claim by
compare-and-swap, so a lost message costs a delay until the next
tick and a duplicate delivery loses its swap. The broker is therefore
never a source of truth, and nothing here stores results.

``CELERY_ENABLED`` is the one switch. Off (the default, and always in
tests), the views write their status and the daemon's tick picks it
up, as before; on, the views also send the task that claims it at
once (``tasks.dispatch_scan``), and ``run_daemon`` sends the ticks of
``tasks.TICKS`` to the workers instead of running them.
"""

import environ

from ..project.testing import TESTING

env = environ.FileAwareEnv()

#: Whether the views dispatch work to Celery. Off in tests, so a view
#: test never needs a broker.
CELERY_ENABLED = env.bool("CELERY_ENABLED", default=False) and not TESTING

CELERY_BROKER_URL = env(
    "CELERY_BROKER_URL", default="redis://scanning-redis:6379/0"
)
#: No result backend: a task answers through the rows it writes.
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True

#: A task is acknowledged when it ends, so a worker killed mid-task
#: hands it back. The pass it ran is idempotent and CAS-guarded, so the
#: second run is a no-op or a retry, never a double write.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
#: One task at a time per process: a prefetched heavy task would sit
#: behind a running one for minutes.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
#: Redis redelivers a task not acknowledged within this window. It
#: must exceed the longest task (an apply build of a large volume), or
#: a second worker starts the same scan and loses the claim. The
#: socket timeouts bound what a view pays when Redis is down: a send
#: fails in seconds, and ``tasks.has_room`` then holds every send back
#: for ``BROKER_BACKOFF_SECONDS``.
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": env.int(
        "CELERY_VISIBILITY_TIMEOUT", default=4 * 60 * 60
    ),
    "socket_connect_timeout": 2,
    "socket_timeout": 5,
}
CELERY_BROKER_CONNECTION_TIMEOUT = 2
#: A send never retries in the request: the tick claims the row.
CELERY_TASK_PUBLISH_RETRY = False
#: The throttle (#503), CourtListener's ``CeleryThrottle`` for senders
#: that cannot sleep: a tick or a dispatch is not sent while its queue
#: already holds this many messages, because the rows are the queue of
#: record and the next tick finds them. It bounds the broker's memory
#: at about a kilobyte a message per queue, whatever a bulk re-queue or
#: a worker outage does.
CELERY_QUEUE_MAX_DEPTH = env.int("CELERY_QUEUE_MAX_DEPTH", default=500)
#: Celery retries nothing: the rows carry the attempt counters.
CELERY_TASK_DEFAULT_QUEUE = "fast"
CELERY_TASK_CREATE_MISSING_QUEUES = True
CELERY_TIMEZONE = "UTC"
#: Logging is Django's ``LOGGING`` alone. Off, Celery leaves the root
#: logger's handlers in place; it would still add one of its own, so
#: ``scanning.celery_init`` answers its ``setup_logging`` signal with
#: nothing, or every record is written twice.
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
