#!/bin/sh
set -e

case "$1" in
'web-dev')
    python manage.py migrate
    python manage.py loaddata reporters
    python manage.py createcachetable
    echo ""
    echo "  Scanning Portal running at: http://localhost:8002"
    echo ""
    exec python manage.py runserver 0.0.0.0:8000
    ;;
'web-prod')
    exec gunicorn scanning.asgi:application \
        --chdir /opt/scanning/ \
        --user www-data \
        --group www-data \
        --workers ${NUM_WORKERS:-4} \
        --worker-class scanning.workers.UvicornWorker \
        --timeout 180 \
        --max-requests ${MAX_REQUESTS:-2500} \
        --max-requests-jitter 100 \
        --bind 0.0.0.0:8000
    ;;
*)
    # Pass through to manage.py for cron jobs, e.g.:
    #   docker exec scanning-django python manage.py createsuperuser
    exec python manage.py "$@"
    ;;
esac
