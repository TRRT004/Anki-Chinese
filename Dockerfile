FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install Python dependencies (layer-cached until requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install cron (slim image has none)
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

# Copy application and crontab
COPY sync.py .
COPY crontab /etc/cron.d/anki-sync
RUN chmod 0644 /etc/cron.d/anki-sync

VOLUME ["/output", "/collection"]

# At startup: snapshot all Docker env vars so the cron job can see them,
# then run cron in the foreground (PID 1).
CMD ["sh", "-c", "printenv > /etc/environment && cron -f"]
