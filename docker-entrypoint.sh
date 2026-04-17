#!/bin/sh
set -e
python3 /app/_write_cron_env.py
/usr/sbin/cron
exec python -u /app/discovery.py
