FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    nginx \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directory for SQLite
RUN mkdir -p /app/data

# Copy nginx config
COPY nginx/nginx.conf /etc/nginx/nginx.conf

# Create startup script
RUN cat > /app/start.sh << 'SCRIPT'
#!/bin/bash
set -e

# Update nginx to listen on the correct port
sed -i "s/listen 80;/listen ${PORTAL_PORT:-8480};/" /etc/nginx/nginx.conf

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
SCRIPT
RUN chmod +x /app/start.sh

# Expose portal port
EXPOSE 8480

CMD ["/app/start.sh"]
