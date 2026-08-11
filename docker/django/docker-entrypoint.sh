#!/bin/sh
set -e

# Install blackletter from the local mount when one is present, so a
# developer iterating on the library sees their checkout instead of the
# version pinned in pyproject.toml. The log line matters: without it there
# is no way to tell from the logs which of the two a container is running.
install_local_blackletter() {
    if [ -d /opt/blackletter ]; then
        echo "Installing local blackletter..."
        uv pip install -e /opt/blackletter 2>/dev/null || pip install -e /opt/blackletter 2>/dev/null || true
    fi
}

case "$1" in
'web-dev')
    install_local_blackletter
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
    install_local_blackletter
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
