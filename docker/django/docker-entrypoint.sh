#!/bin/sh
set -e

case "$1" in
'web-dev')
    # Install blackletter from local mount if available
    if [ -d /opt/blackletter ]; then
        uv pip install -e /opt/blackletter 2>/dev/null || pip install -e /opt/blackletter 2>/dev/null || true
    fi
    python manage.py migrate
    python manage.py loaddata reporters
    python manage.py make_dev_data
    python manage.py createcachetable
    echo ""
    echo "  Scanning Portal running at: http://localhost:8002"
    echo ""
    exec python manage.py runserver 0.0.0.0:8000
    ;;
'run_daemon')
    # Install blackletter from local mount if available
    if [ -d /opt/blackletter ]; then
        uv pip install -e /opt/blackletter 2>/dev/null || pip install -e /opt/blackletter 2>/dev/null || true
    fi
    exec python manage.py run_daemon
    ;;
'worker')
    # A Celery worker (#503). CELERY_QUEUES names the queues it serves
    # (fast, io, heavy, opinions); CELERY_CONCURRENCY the processes.
    # A heavy process is recycled after a few tasks, because fitz and
    # blackletter hold memory a long volume leaves behind.
    exec celery -A scanning worker \
        --queues "${CELERY_QUEUES:-fast,io,heavy,opinions}" \
        --concurrency "${CELERY_CONCURRENCY:-2}" \
        --max-tasks-per-child "${CELERY_MAX_TASKS_PER_CHILD:-20}" \
        --loglevel "${CELERY_LOGLEVEL:-info}"
    ;;
'web-prod')
    mkdir -p /opt/scanning/scanning/assets/media/processed
    chown -R www-data:www-data /opt/scanning/scanning/assets/media
    # The database cache's table (CACHES in settings/django.py): the
    # header's repair count lives in it since #567. Idempotent, and a
    # second of the start; without it every page read the cache and
    # failed, on 10-31, until the table was made by hand.
    python manage.py createcachetable
    exec gunicorn scanning.asgi:application \
        --chdir /opt/scanning/ \
        --user www-data \
        --group www-data \
        --workers ${NUM_WORKERS:-4} \
        --worker-class scanning.workers.UvicornWorker \
        --preload \
        --no-control-socket \
        --timeout 180 \
        --bind 0.0.0.0:8000
    ;;
*)
    # Pass through to manage.py for cron jobs, e.g.:
    #   docker exec scanning-django python manage.py createsuperuser
    exec python manage.py "$@"
    ;;
esac
