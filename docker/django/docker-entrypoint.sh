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
'web-prod')
    mkdir -p /opt/scanning/scanning/assets/media/processed
    chown -R www-data:www-data /opt/scanning/scanning/assets/media
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
