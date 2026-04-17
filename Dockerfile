FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY discovery.py prune_library.py icon.png docker-entrypoint.sh _write_cron_env.py _prune_cron_entry.py /app/
COPY crontab-prune /etc/cron.d/prune-library

RUN chmod +x /app/docker-entrypoint.sh \
    && chmod 0644 /etc/cron.d/prune-library

# Persistent data volume (SQLite DB + log file + pruning.log)
VOLUME ["/data"]

# Web UI (set DISCOVERY_GUI_PORT=0 to disable server)
EXPOSE 8765

ENTRYPOINT ["/app/docker-entrypoint.sh"]
