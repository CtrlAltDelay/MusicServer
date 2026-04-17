#!/usr/bin/env python3
"""
Unmonitor Lidarr artists tagged as discovery seeds when they have no ListenBrainz
listens in a recent window (default 30 days) and have been in the library longer
than that window. Does not delete media — only sets artist.monitored = false.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

# ── Config (env) ─────────────────────────────────────────────────────────────
LISTENBRAINZ_USERNAME = os.environ.get("LISTENBRAINZ_USERNAME", "").strip()
LISTENBRAINZ_TOKEN = os.environ.get("LISTENBRAINZ_TOKEN", "").strip()
LIDARR_URL = os.environ.get("LIDARR_URL", "http://lidarr:8686").rstrip("/")
LIDARR_API_KEY = os.environ.get("LIDARR_API_KEY", "").strip()

LISTENBRAINZ_BASE = "https://api.listenbrainz.org"

# Match discovery bridge data dir; in Docker, /data is usually mounted to host /opt/music/discovery
_data_dir = Path(os.environ.get("DISCOVERY_DATA_DIR", "/data"))
PRUNE_LOG_PATH = Path(
    os.environ.get("PRUNE_LOG_PATH", str(_data_dir / "pruning.log"))
)
PRUNE_TAG_LABEL = os.environ.get("PRUNE_TAG_LABEL", "discovered").strip().lower()
PRUNE_LISTEN_DAYS = int(os.environ.get("PRUNE_LISTEN_DAYS", "30"))
PRUNE_MIN_DAYS_IN_LIBRARY = int(os.environ.get("PRUNE_MIN_DAYS_IN_LIBRARY", "30"))
PRUNE_MAX_LISTENS_FETCH = int(os.environ.get("PRUNE_MAX_LISTENS_FETCH", "25000"))


def _respect_listenbrainz_rate_limit_headers(r: requests.Response) -> None:
    try:
        rem = int(r.headers.get("X-RateLimit-Remaining", "999"))
        if rem <= 1:
            reset_in = r.headers.get("X-RateLimit-Reset-In")
            if reset_in:
                w = float(reset_in) + 0.5
                time.sleep(min(w, 120))
    except (TypeError, ValueError):
        pass


def listenbrainz_get(path: str, params: dict | None = None) -> dict:
    if not path.startswith("/"):
        path = "/" + path
    url = f"{LISTENBRAINZ_BASE}{path}"
    headers = {"Content-Type": "application/json; charset=UTF-8"}
    if LISTENBRAINZ_TOKEN:
        headers["Authorization"] = f"Token {LISTENBRAINZ_TOKEN}"

    for attempt in range(5):
        try:
            r = requests.get(url, params=params or {}, headers=headers, timeout=20)
            if r.status_code == 429:
                reset = r.headers.get("X-RateLimit-Reset-In") or r.headers.get("Retry-After")
                try:
                    wait = float(reset) + 1 if reset else 2**attempt
                except (TypeError, ValueError):
                    wait = 2**attempt
                time.sleep(min(wait, 120))
                continue
            if r.status_code in (502, 503):
                time.sleep(min(float(2**attempt), 120.0))
                continue
            r.raise_for_status()
            _respect_listenbrainz_rate_limit_headers(r)
            data = r.json()
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logging.warning("ListenBrainz GET %s failed (attempt %s): %s", path, attempt + 1, e)
            time.sleep(2**attempt)
    return {}


def lidarr_get(path: str, **params) -> list | dict | None:
    url = f"{LIDARR_URL}/api/v1/{path}"
    headers = {"X-Api-Key": LIDARR_API_KEY}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error("Lidarr GET %s failed: %s", path, e)
        return None


def lidarr_get_artist(aid: int) -> dict | None:
    url = f"{LIDARR_URL}/api/v1/artist/{aid}"
    headers = {"X-Api-Key": LIDARR_API_KEY}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        logging.error("Lidarr GET artist/%s failed: %s", aid, e)
        return None


def lidarr_put_artist(artist: dict) -> bool:
    aid = artist.get("id")
    if aid is None:
        return False
    url = f"{LIDARR_URL}/api/v1/artist/{aid}"
    headers = {"X-Api-Key": LIDARR_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.put(url, headers=headers, json=artist, timeout=60)
        r.raise_for_status()
        return True
    except Exception as e:
        logging.error("Lidarr PUT artist/%s failed: %s", aid, e)
        return False


def tag_id_for_label(label: str) -> int | None:
    tags = lidarr_get("tag") or []
    want = label.lower()
    for t in tags:
        if not isinstance(t, dict):
            continue
        if str(t.get("label") or "").lower() == want:
            tid = t.get("id")
            return int(tid) if tid is not None else None
    logging.error("Lidarr has no tag %r; create it or set PRUNE_TAG_LABEL.", label)
    return None


def collect_artist_mbids_from_recent_listens(username: str, days: int) -> set[str] | None:
    """Lowercased MusicBrainz artist IDs seen on listens in [now-days, now]. None = fetch failed."""
    if days < 1:
        return set()
    min_ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    out: set[str] = set()
    fetched = 0
    max_ts: int | None = None

    while fetched < PRUNE_MAX_LISTENS_FETCH:
        count = min(100, PRUNE_MAX_LISTENS_FETCH - fetched)
        params: dict = {"count": count, "min_ts": min_ts}
        if max_ts is not None:
            params["max_ts"] = max_ts
        data = listenbrainz_get(f"/1/user/{quote(username, safe='')}/listens", params)
        if data.get("error") is not None:
            logging.error("ListenBrainz listens error: %s", data.get("error"))
            return None
        if "payload" not in data and fetched == 0:
            logging.error("ListenBrainz listens: missing payload in response.")
            return None
        payload = data.get("payload") or {}
        listens = payload.get("listens") or []
        if not listens:
            break
        for listen in listens:
            if not isinstance(listen, dict):
                continue
            tm = listen.get("track_metadata") or {}
            mm = tm.get("mbid_mapping") or {}
            artist_mbids = mm.get("artist_mbids") or []
            if not artist_mbids and mm.get("artist_mbid"):
                artist_mbids = [mm["artist_mbid"]]
            for amb in artist_mbids:
                if amb:
                    out.add(str(amb).strip().lower())
            add = tm.get("additional_info") if isinstance(tm.get("additional_info"), dict) else {}
            for key in ("artist_mbids", "artist_mbid"):
                raw = add.get(key)
                if isinstance(raw, list):
                    for x in raw:
                        if x:
                            out.add(str(x).strip().lower())
                elif raw:
                    out.add(str(raw).strip().lower())

        fetched += len(listens)
        last_ts = listens[-1].get("listened_at")
        if last_ts is None:
            break
        try:
            max_ts = int(last_ts)
        except (TypeError, ValueError):
            break
        if len(listens) < count:
            break
        time.sleep(0.12)

    logging.info(
        "ListenBrainz: collected %d distinct artist MBIDs from last %d days (%d listens fetched).",
        len(out),
        days,
        fetched,
    )
    return out


def parse_lidarr_added(artist: dict) -> datetime | None:
    raw = artist.get("added")
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def main() -> int:
    if not LISTENBRAINZ_USERNAME:
        print("LISTENBRAINZ_USERNAME is required.", file=sys.stderr)
        return 1
    if not LIDARR_API_KEY:
        print("LIDARR_API_KEY is required.", file=sys.stderr)
        return 1

    PRUNE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(PRUNE_LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )

    logging.info(
        "Prune run: tag=%r listen_window=%dd min_library_age=%dd log=%s",
        PRUNE_TAG_LABEL,
        PRUNE_LISTEN_DAYS,
        PRUNE_MIN_DAYS_IN_LIBRARY,
        PRUNE_LOG_PATH,
    )

    tag_id = tag_id_for_label(PRUNE_TAG_LABEL)
    if tag_id is None:
        return 1

    artists = lidarr_get("artist")
    if not isinstance(artists, list):
        logging.error("Unexpected Lidarr artist list response.")
        return 1

    discovered = [
        a
        for a in artists
        if isinstance(a, dict)
        and tag_id in (a.get("tags") or [])
        and a.get("monitored") is True
    ]
    logging.info("Lidarr: %d monitored artists with tag %r.", len(discovered), PRUNE_TAG_LABEL)

    listened_mbids = collect_artist_mbids_from_recent_listens(
        LISTENBRAINZ_USERNAME, PRUNE_LISTEN_DAYS
    )
    if listened_mbids is None:
        logging.error("Aborting: could not load ListenBrainz listening history.")
        return 1

    now = datetime.now(timezone.utc)
    min_age = timedelta(days=PRUNE_MIN_DAYS_IN_LIBRARY)
    unmonitored = 0
    skipped_no_mbid = 0
    skipped_recent = 0
    skipped_has_listens = 0

    for summary in discovered:
        aid = summary.get("id")
        name = summary.get("artistName") or summary.get("sortName") or "?"
        mbid_raw = (summary.get("foreignArtistId") or "").strip()
        if not mbid_raw:
            skipped_no_mbid += 1
            logging.warning("Skip artist id=%s name=%r: no MusicBrainz artist id.", aid, name)
            continue
        mbid_l = mbid_raw.lower()
        if mbid_l in listened_mbids:
            skipped_has_listens += 1
            continue

        cached_full: dict | None = None
        added_dt = parse_lidarr_added(summary)
        if added_dt is None and aid is not None:
            cached_full = lidarr_get_artist(int(aid))
            added_dt = parse_lidarr_added(cached_full or {})
        if added_dt is None:
            logging.warning(
                "Skip artist id=%s name=%r: could not parse added date; not unmonitoring.",
                aid,
                name,
            )
            continue
        if now - added_dt < min_age:
            skipped_recent += 1
            continue

        full = cached_full if cached_full is not None else (
            lidarr_get_artist(int(aid)) if aid is not None else None
        )
        if not full:
            continue
        if not full.get("monitored"):
            continue
        if tag_id not in (full.get("tags") or []):
            logging.info("Artist id=%s no longer has tag; skipping.", aid)
            continue

        full["monitored"] = False
        if lidarr_put_artist(full):
            unmonitored += 1
            logging.info(
                "Unmonitored artist id=%s name=%r mbid=%s (no listens in %dd; added %s).",
                aid,
                name,
                mbid_raw,
                PRUNE_LISTEN_DAYS,
                added_dt.date().isoformat(),
            )
        time.sleep(0.05)

    logging.info(
        "Done. Unmonitored=%d skipped_has_listens=%d skipped_recent=%d skipped_no_mbid=%d",
        unmonitored,
        skipped_has_listens,
        skipped_recent,
        skipped_no_mbid,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
