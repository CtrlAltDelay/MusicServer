#!/usr/bin/env python3
"""Write container environment to /app/cron-env.json for cron jobs (cron has no Docker env)."""
import json
import os
from pathlib import Path

p = Path("/app/cron-env.json")
p.write_text(json.dumps(dict(os.environ)), encoding="utf-8")
os.chmod(p, 0o600)
