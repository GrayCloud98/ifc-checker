FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["gunicorn", "-w", "1", "-k", "gthread", "--threads", "4", "--max-requests", "50", "--max-requests-jitter", "10", "--worker-tmp-dir", "/dev/shm", "-t", "180", "-b", "0.0.0.0:8000", "main:app"]
