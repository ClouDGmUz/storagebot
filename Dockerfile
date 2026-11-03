FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/storage.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py db.py ./

# Persistent storage is mounted at /data by the platform
# (docker-compose volume locally, Railway Volume in the cloud).
# No VOLUME/USER here: Railway rejects VOLUME and mounts volumes as root.
RUN mkdir -p /data

CMD ["python", "bot.py"]
