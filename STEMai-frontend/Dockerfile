# ============================================================
# STEMbotix AI — Frontend Dockerfile (Flask + Gunicorn)
# ============================================================
FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies (build-essential for any C-extension wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies first (Docker layer caching)
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the entire frontend source code
COPY . .

# The frontend listens on port 5000
EXPOSE 5000

# Run with Gunicorn (production-grade WSGI server)
# - 4 workers for concurrency
# - 120s timeout to match the proxy timeout in app.py
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--timeout", "120", "app:app"]
