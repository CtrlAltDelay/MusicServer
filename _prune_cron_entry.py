#!/usr/bin/env python3
"""Cron wrapper: load env snapshot from entrypoint, then run prune_library.py."""
import json
import os
import subprocess
import sys
from pathlib import Path

def main() -> int:
    env_path = Path("/app/cron-env.json")
    if not env_path.is_file():
        print("cron-env.json missing; container entrypoint must run first.", file=sys.stderr)
        return 1
    with env_path.open(encoding="utf-8") as f:
        blob = json.load(f)
    env = os.environ.copy()
    for k, v in blob.items():
        env[str(k)] = str(v)
    return subprocess.call(
        [sys.executable, "-u", str(Path("/app") / "prune_library.py")],
        cwd="/app",
        env=env,
    )


if __name__ == "__main__":
    raise SystemExit(main())
