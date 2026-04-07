FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    nginx \
    curl \
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

# Make startup script executable
RUN chmod +x /app/start.sh

# Expose portal port
EXPOSE 8480

CMD ["/app/start.sh"]
