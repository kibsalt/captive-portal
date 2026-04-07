#!/bin/bash
set -e

# Update nginx to listen on the correct port
sed -i "s/listen 8480;/listen ${PORTAL_PORT:-8480};/" /etc/nginx/nginx.conf

# Start nginx in background
nginx -g 'daemon on;'

# Start gunicorn (Flask app)
exec gunicorn \
    --bind 127.0.0.1:5000 \
    --workers ${GUNICORN_WORKERS:-4} \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    "app:create_app()"
