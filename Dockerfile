FROM python:3.11-slim

# Keeps Python from buffering stdout/stderr (important for Docker log streaming)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first (layer-cached until requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY sync.py .

# /output is where the .apkg file lands; mount a host directory here to persist it
VOLUME ["/output"]

CMD ["python", "sync.py"]
