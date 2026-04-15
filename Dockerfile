FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY discovery.py .

# Persistent data volume (SQLite DB + log file)
VOLUME ["/data"]

# Web UI (set DISCOVERY_GUI_PORT=0 to disable server)
EXPOSE 8765

CMD ["python", "-u", "discovery.py"]
