#!/usr/bin/env python3
"""
Music Discovery Bridge
======================
Pulls your top artists from ListenBrainz, finds similar artists,
and automatically adds new ones to Lidarr for download.

Flow:
  1. Fetch your top artists from ListenBrainz stats (half-yearly window)
  2. For each top artist with a MusicBrainz ID, get similar artists (Labs API)
  3. Score candidates by similarity + how many of your artists link to them
  4. Filter out artists already in Lidarr
  5. Add the top N new artists to Lidarr, tagged as "discovered"
  6. Trigger Lidarr to search for their latest album
"""

import json
import os
import threading
import time
import logging
import sqlite3
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from flask import Flask, jsonify, request
import requests

# ── Logging ──────────────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DISCOVERY_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "discovery.db")
SETTINGS_PATH = DATA_DIR / "settings.json"
LOG_PATH = DATA_DIR / "discovery.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH),
    ],
)
log = logging.getLogger("discovery")

# ── Config (from environment variables) ──────────────────────────────────────
LISTENBRAINZ_USERNAME = os.environ["LISTENBRAINZ_USERNAME"]
LISTENBRAINZ_TOKEN = os.environ.get("LISTENBRAINZ_TOKEN", "").strip()
LIDARR_URL = os.environ.get("LIDARR_URL", "http://lidarr:8686")
LIDARR_API_KEY = os.environ["LIDARR_API_KEY"]
LIDARR_ROOT_PATH = os.environ.get("LIDARR_ROOT_PATH", "/music")

# Optional GUI auth: if set, require matching `?token=` or `X-Discovery-Token` header
DISCOVERY_GUI_TOKEN = os.environ.get("DISCOVERY_GUI_TOKEN", "").strip()
DISCOVERY_GUI_PORT = int(os.environ.get("DISCOVERY_GUI_PORT", "8765"))

LISTENBRAINZ_BASE = "https://api.listenbrainz.org"
LISTENBRAINZ_LABS_BASE = "https://labs.api.listenbrainz.org"

DEFAULT_SETTINGS = {
    # "most_listened" (stats), "loved" (hearted tracks), "both" (combined)
    "seeds_mode": os.environ.get("DISCOVERY_SEED_MODE", os.environ.get("DISCOVERY_SOURCE", "loved")),
    "top_artists_count": int(os.environ.get("TOP_ARTISTS_COUNT", "20")),
    "loved_feedback_count": int(os.environ.get("LOVED_FEEDBACK_COUNT", os.environ.get("LOVED_TRACKS_LIMIT", "200"))),
    "listenbrainz_stats_range": os.environ.get("LISTENBRAINZ_STATS_RANGE", "half_yearly"),
    "similar_per_artist": int(os.environ.get("SIMILAR_PER_ARTIST", "10")),
    "max_new_artists": int(os.environ.get("MAX_NEW_ARTISTS", "5")),
    "min_similarity": float(os.environ.get("MIN_SIMILARITY", "0.25")),
    "run_interval_seconds": int(os.environ.get("RUN_INTERVAL_SECONDS", str(24 * 60 * 60))),
    "similar_algorithm": os.environ.get(
        "LISTENBRAINZ_SIMILAR_ALGORITHM",
        "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30",
    ),
}

_settings_lock = threading.Lock()
_run_lock = threading.Lock()
_scheduler_stop = threading.Event()
_last_run_lock = threading.Lock()
_last_run: dict | None = None
_log_buffer: deque[str] = deque(maxlen=400)


def _respect_listenbrainz_rate_limit_headers(r: requests.Response) -> None:
    """Sleep if ListenBrainz reports almost no quota left (X-RateLimit-* headers)."""
    try:
        rem = int(r.headers.get("X-RateLimit-Remaining", "999"))
        if rem <= 1:
            reset_in = r.headers.get("X-RateLimit-Reset-In")
            if reset_in:
                w = float(reset_in) + 0.5
                log.info("ListenBrainz rate limit nearly exhausted; sleeping %.1fs", w)
                time.sleep(min(w, 120))
    except (TypeError, ValueError):
        pass


class BufferLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _log_buffer.append(self.format(record))
        except Exception:
            pass


def _ensure_buffer_logging() -> None:
    h = BufferLogHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(h)


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)  # type: ignore[arg-type]
        else:
            out[k] = v
    return out


def load_settings_file() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning("Could not read settings file %s: %s", SETTINGS_PATH, e)
        return {}


def save_settings_file(updates: dict) -> None:
    with _settings_lock:
        current = {**DEFAULT_SETTINGS, **load_settings_file()}
        merged = _deep_merge(current, updates)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, sort_keys=True)


def effective_settings() -> dict:
    with _settings_lock:
        return {**DEFAULT_SETTINGS, **load_settings_file()}


def _coerce_settings(raw: dict) -> dict:
    out: dict = {}
    mode = str(raw.get("seeds_mode", DEFAULT_SETTINGS["seeds_mode"])).lower()
    if mode == "top":
        mode = "most_listened"
    if mode not in ("most_listened", "loved", "both"):
        mode = "most_listened"
    out["seeds_mode"] = mode

    def _as_int(key: str, default: int, lower: int = 1, upper: int = 10_000) -> int:
        try:
            value = int(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(lower, min(upper, value))

    def _as_float_01(key: str, default: float) -> float:
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(0.0, min(1.0, value))

    out["top_artists_count"] = _as_int("top_artists_count", DEFAULT_SETTINGS["top_artists_count"], 1, 500)
    out["loved_feedback_count"] = _as_int(
        "loved_feedback_count", DEFAULT_SETTINGS["loved_feedback_count"], 1, 2000
    )
    out["similar_per_artist"] = _as_int("similar_per_artist", DEFAULT_SETTINGS["similar_per_artist"], 1, 100)
    out["max_new_artists"] = _as_int("max_new_artists", DEFAULT_SETTINGS["max_new_artists"], 1, 100)
    out["run_interval_seconds"] = _as_int(
        "run_interval_seconds", DEFAULT_SETTINGS["run_interval_seconds"], 60, 86400 * 14
    )
    out["min_similarity"] = _as_float_01("min_similarity", float(DEFAULT_SETTINGS["min_similarity"]))

    ranges = {
        "week",
        "month",
        "quarter",
        "half_yearly",
        "year",
        "this_week",
        "this_month",
        "this_year",
        "all_time",
    }
    range_value = str(raw.get("listenbrainz_stats_range", DEFAULT_SETTINGS["listenbrainz_stats_range"]))
    out["listenbrainz_stats_range"] = (
        range_value if range_value in ranges else DEFAULT_SETTINGS["listenbrainz_stats_range"]
    )
    algorithm = str(raw.get("similar_algorithm", DEFAULT_SETTINGS["similar_algorithm"])).strip()
    out["similar_algorithm"] = algorithm[:500] or DEFAULT_SETTINGS["similar_algorithm"]
    return out


def _set_last_run(data: dict) -> None:
    with _last_run_lock:
        global _last_run
        _last_run = data


def _get_last_run() -> dict | None:
    with _last_run_lock:
        return dict(_last_run) if _last_run else None


# ── Database (tracks what we've already added so we don't re-add) ─────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS added_artists (
            mbid        TEXT,
            name        TEXT NOT NULL,
            added_at    TEXT NOT NULL,
            lidarr_id   INTEGER,
            PRIMARY KEY (mbid, name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at      TEXT NOT NULL,
            artists_added INTEGER,
            status      TEXT
        )
    """)
    conn.commit()
    return conn


def was_already_added(conn, name: str, mbid: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM added_artists WHERE name = ? OR (mbid != '' AND mbid = ?)",
        (name.lower(), mbid),
    ).fetchone()
    return row is not None


def record_added(conn, name: str, mbid: str, lidarr_id: int):
    conn.execute(
        "INSERT OR IGNORE INTO added_artists (mbid, name, added_at, lidarr_id) VALUES (?,?,?,?)",
        (mbid, name.lower(), datetime.utcnow().isoformat(), lidarr_id),
    )
    conn.commit()


# ── ListenBrainz helpers ───────────────────────────────────────────────────────
def listenbrainz_get(path: str, params: dict | None = None) -> dict:
    """GET main ListenBrainz API (api.listenbrainz.org). Returns JSON object or {}."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"{LISTENBRAINZ_BASE}{path}"
    headers = {"Content-Type": "application/json; charset=UTF-8"}
    if LISTENBRAINZ_TOKEN:
        headers["Authorization"] = f"Token {LISTENBRAINZ_TOKEN}"

    for attempt in range(3):
        try:
            r = requests.get(url, params=params or {}, headers=headers, timeout=15)
            if r.status_code == 429:
                reset = r.headers.get("X-RateLimit-Reset-In") or r.headers.get("Retry-After")
                try:
                    wait = float(reset) + 1 if reset else 2 ** attempt
                except (TypeError, ValueError):
                    wait = 2 ** attempt
                log.warning("ListenBrainz rate limited, sleeping %.1fs", min(wait, 120))
                time.sleep(min(wait, 120))
                continue
            if r.status_code == 204:
                log.warning("ListenBrainz returned 204 (no statistics yet for this user/range).")
                return {}
            r.raise_for_status()
            _respect_listenbrainz_rate_limit_headers(r)
            data = r.json()
            if not isinstance(data, dict):
                return {}
            if data.get("error"):
                log.warning(
                    "ListenBrainz error%s: %s",
                    f" {data.get('code')}" if data.get("code") else "",
                    data.get("error"),
                )
                return {}
            return data
        except Exception as e:
            log.warning("ListenBrainz request failed (attempt %d): %s", attempt + 1, e)
            time.sleep(2 ** attempt)
    return {}


def get_top_artists(username: str, count: int, stats_range: str) -> list[dict]:
    """
    Return user's top artists as [{"name": ..., "mbid": ..., "listen_count": int}, ...]
    from ListenBrainz user statistics.
    """
    path = f"/1/stats/user/{quote(username, safe='')}/artists"
    data = listenbrainz_get(
        path,
        params={"count": count, "range": stats_range},
    )
    raw = (data.get("payload") or {}).get("artists") or []
    artists: list[dict] = []
    for a in raw:
        name = a.get("artist_name") or a.get("name") or ""
        mbid = a.get("artist_mbid") or a.get("mbid") or ""
        try:
            lc = int(a.get("listen_count", 0))
        except (TypeError, ValueError):
            lc = 0
        if name:
            artists.append({"name": name, "mbid": mbid, "listen_count": lc})
    log.info("Fetched %d top artists for user '%s'", len(artists), username)
    return artists


def get_loved_artists(username: str, limit: int) -> list[dict]:
    """
    Return artists from the user's loved/hearted recordings on ListenBrainz.
    Steps:
      1. Paginate through GET /1/feedback/user/{user}/get-feedback?score=1
      2. Batch-lookup artist info via POST /1/metadata/recording/ for recording MBIDs
      3. Deduplicate by artist, counting loved tracks per artist as weight
    Returns [{"name": ..., "mbid": ..., "listen_count": int}, ...] sorted by loved-count desc.
    """
    # Step 1 — collect recording MBIDs from loved feedback (paginated, 100 per page)
    recording_mbids: list[str] = []
    offset = 0
    page_size = 100
    while len(recording_mbids) < limit:
        path = f"/1/feedback/user/{quote(username, safe='')}/get-feedback"
        data = listenbrainz_get(
            path,
            params={"score": 1, "count": page_size, "offset": offset},
        )
        items = data.get("feedback") or []
        if not items:
            break
        for fb in items:
            mbid = fb.get("recording_mbid") or ""
            if mbid:
                recording_mbids.append(mbid)
        offset += page_size
        if len(items) < page_size:
            break

    if not recording_mbids:
        log.info("No loved recordings found for user '%s'", username)
        return []

    recording_mbids = recording_mbids[:limit]
    log.info("Fetched %d loved recording MBIDs for user '%s'", len(recording_mbids), username)

    # Step 2 — batch-lookup artist metadata (chunks of 50 to stay within API limits)
    artist_counts: dict[str, dict] = {}  # keyed by artist_mbid
    chunk_size = 50
    for i in range(0, len(recording_mbids), chunk_size):
        chunk = recording_mbids[i : i + chunk_size]
        url = f"{LISTENBRAINZ_BASE}/1/metadata/recording/"
        headers = {"Content-Type": "application/json"}
        if LISTENBRAINZ_TOKEN:
            headers["Authorization"] = f"Token {LISTENBRAINZ_TOKEN}"
        try:
            r = requests.post(
                url, headers=headers,
                json={"recording_mbids": chunk, "inc": "artist"},
                timeout=15,
            )
            r.raise_for_status()
            meta = r.json()
        except Exception as e:
            log.warning("Metadata lookup failed for chunk starting at %d: %s", i, e)
            time.sleep(1)
            continue

        for rec_mbid, rec_data in meta.items():
            artist_info = rec_data.get("artist") or {}
            artist_name = artist_info.get("name") or ""
            artists_list = artist_info.get("artists") or []
            artist_mbid = ""
            if artists_list:
                artist_mbid = artists_list[0].get("artist_mbid") or ""

            if not artist_name:
                continue

            key = artist_mbid or artist_name.lower()
            if key in artist_counts:
                artist_counts[key]["listen_count"] += 1
            else:
                artist_counts[key] = {
                    "name": artist_name,
                    "mbid": artist_mbid,
                    "listen_count": 1,
                }
        time.sleep(0.5)

    result = sorted(artist_counts.values(), key=lambda x: x["listen_count"], reverse=True)
    log.info("Resolved %d unique artists from loved recordings", len(result))
    return result


def get_similar_artists(artist_mbid: str, limit: int, algorithm: str) -> list[dict]:
    """Return similar artists as [{"name": ..., "mbid": ..., "match": float}] via Labs API."""
    url = f"{LISTENBRAINZ_LABS_BASE}/similar-artists/json"
    params = {"artist_mbids": artist_mbid, "algorithm": algorithm}
    headers = {"Content-Type": "application/json; charset=UTF-8"}
    if LISTENBRAINZ_TOKEN:
        headers["Authorization"] = f"Token {LISTENBRAINZ_TOKEN}"

    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            if r.status_code == 429:
                reset = r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset-In")
                try:
                    wait = float(reset) + 1 if reset else 2 ** attempt
                except (TypeError, ValueError):
                    wait = 2 ** attempt
                log.warning("ListenBrainz Labs rate limited, sleeping %.1fs", min(wait, 120))
                time.sleep(min(wait, 120))
                continue
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            log.warning("ListenBrainz Labs request failed (attempt %d): %s", attempt + 1, e)
            time.sleep(2 ** attempt)
    else:
        time.sleep(0.25)
        return []

    if not isinstance(data, list):
        log.warning("Unexpected similar-artists response (not a list)")
        time.sleep(0.25)
        return []

    rows = data[:limit]
    numeric_scores: list[float] = []
    for row in rows:
        s = row.get("score")
        if s is not None:
            try:
                numeric_scores.append(abs(float(s)))
            except (TypeError, ValueError):
                pass
    max_s = max(numeric_scores) if numeric_scores else 0.0

    out: list[dict] = []
    n = len(rows)
    for i, row in enumerate(rows):
        name = row.get("name") or row.get("artist_name") or ""
        mbid = row.get("artist_mbid") or row.get("mbid") or ""
        if max_s > 0:
            try:
                raw = row.get("score")
                if raw is not None:
                    match = abs(float(raw)) / max_s
                else:
                    match = (1.0 - (i / n)) if n else 0.0
            except (TypeError, ValueError):
                match = (1.0 - (i / n)) if n else 0.0
        else:
            match = (1.0 - (i / n)) if n else 0.0
        out.append({"name": name, "mbid": mbid, "match": float(match)})

    time.sleep(0.25)
    return out


# ── Lidarr helpers ────────────────────────────────────────────────────────────
def lidarr_get(path: str, **params) -> list | dict | None:
    url = f"{LIDARR_URL}/api/v1/{path}"
    headers = {"X-Api-Key": LIDARR_API_KEY}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error("Lidarr GET %s failed: %s", path, e)
        return None


def lidarr_post(path: str, body: dict) -> dict | None:
    url = f"{LIDARR_URL}/api/v1/{path}"
    headers = {"X-Api-Key": LIDARR_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error("Lidarr POST %s failed: %s", path, e)
        return None


def get_lidarr_artist_names() -> set[str]:
    """Return lowercase names of all artists currently in Lidarr."""
    artists = lidarr_get("artist") or []
    return {a["artistName"].lower() for a in artists}


def get_lidarr_profiles() -> tuple[int, int]:
    """Return (quality_profile_id, metadata_profile_id) — uses first available."""
    quality = lidarr_get("qualityprofile") or []
    metadata = lidarr_get("metadataprofile") or []
    q_id = quality[0]["id"] if quality else 1
    m_id = metadata[0]["id"] if metadata else 1
    return q_id, m_id


def get_or_create_discovered_tag() -> int:
    """Get the 'discovered' tag id in Lidarr, creating it if needed."""
    tags = lidarr_get("tag") or []
    for tag in tags:
        if tag["label"].lower() == "discovered":
            return tag["id"]
    # Create it
    result = lidarr_post("tag", {"label": "discovered"})
    if result:
        log.info("Created 'discovered' tag in Lidarr (id=%d)", result["id"])
        return result["id"]
    return None


def lookup_artist_in_lidarr(artist_name: str) -> dict | None:
    """Search Lidarr's MusicBrainz lookup for an artist."""
    results = lidarr_get("artist/lookup", term=artist_name)
    if results:
        return results[0]  # best match
    return None


def add_artist_to_lidarr(artist_data: dict, quality_id: int, metadata_id: int, tag_id: int) -> dict | None:
    """Add a looked-up artist to Lidarr, monitoring only their latest album."""
    clean = {k: v for k, v in artist_data.items()
             if k not in ("albums", "rootFolderPath", "path", "addOptions", "id")}
    body = {
        **clean,
        "qualityProfileId": quality_id,
        "metadataProfileId": metadata_id,
        "rootFolderPath": LIDARR_ROOT_PATH,
        "monitored": True,
        "monitorNewItems": "new",
        "tags": [tag_id] if tag_id else [],
        "addOptions": {
            "monitor": "latest",               # only grab the newest album
            "searchForMissingAlbums": True,    # trigger download immediately
        },
    }
    log.debug("Lidarr add body keys: %s", list(body.keys()))
    return lidarr_post("artist", body)


# ── Core discovery logic ──────────────────────────────────────────────────────
def build_candidate_pool(
    top_artists: list[dict], similar_per_artist: int, min_similarity: float, similar_algorithm: str
) -> dict[str, dict]:
    """
    For each top artist, fetch similar artists and accumulate a score.
    Score = sum of similarity values across all seeds that recommend this artist.
    Returns: {artist_name_lower: {"name": str, "mbid": str, "score": float}}
    """
    candidates: dict[str, dict] = {}

    for seed in top_artists:
        seed_name = seed.get("name", "")
        seed_mbid = (seed.get("mbid") or "").strip()
        if not seed_name:
            continue
        if not seed_mbid:
            log.warning(
                "Skipping seed artist %r: no MusicBrainz ID in ListenBrainz stats",
                seed_name,
            )
            continue
        log.info("Finding artists similar to: %s", seed_name)
        similar = get_similar_artists(seed_mbid, similar_per_artist, similar_algorithm)

        for artist in similar:
            name = artist.get("name", "")
            mbid = artist.get("mbid", "")
            try:
                score = float(artist.get("match", 0))
            except (ValueError, TypeError):
                score = 0.0

            if score < min_similarity or not name:
                continue

            key = name.lower()
            if key in candidates:
                candidates[key]["score"] += score  # compound score
            else:
                candidates[key] = {"name": name, "mbid": mbid, "score": score}

    return candidates


def run_discovery(config: dict | None = None) -> dict:
    cfg = _coerce_settings(config or effective_settings())
    mode = cfg["seeds_mode"]

    if not _run_lock.acquire(blocking=False):
        return {"status": "busy"}

    started_at = datetime.utcnow().isoformat()
    _set_last_run({"status": "running", "started_at": started_at, "config": cfg})

    try:
        log.info("=" * 60)
        log.info("Starting discovery run (seed_mode=%s)", mode)
        conn = init_db()

        # 1. Gather seed artists based on configured mode
        seed_artists: list[dict] = []
        if mode in ("loved", "both"):
            loved = get_loved_artists(LISTENBRAINZ_USERNAME, int(cfg["loved_feedback_count"]))
            seed_artists.extend(loved)
            log.info("Loved-artist seeds: %d", len(loved))

        if mode in ("most_listened", "both"):
            top = get_top_artists(
                LISTENBRAINZ_USERNAME,
                int(cfg["top_artists_count"]),
                str(cfg["listenbrainz_stats_range"]),
            )
            seed_artists.extend(top)
            log.info("Most-listened seeds: %d", len(top))

        if not seed_artists:
            msg = {
                "loved": "No loved/hearted recordings found on ListenBrainz. Heart some songs in your player first.",
                "most_listened": "No top artists returned from ListenBrainz. Is your history empty, or are statistics not computed yet?",
                "both": "No seed artists found from loved recordings or play history.",
            }
            status = "no_seeds"
            log.warning(msg.get(mode, msg["both"]))
            conn.execute(
                "INSERT INTO runs (run_at, artists_added, status) VALUES (?,?,?)",
                (datetime.utcnow().isoformat(), 0, status),
            )
            conn.commit()
            result = {"status": status, "artists_added": 0, "seeds_used": 0}
            _set_last_run({**result, "started_at": started_at, "finished_at": datetime.utcnow().isoformat()})
            return result

        # Deduplicate seeds by name (keep first occurrence, preserving source priority)
        seen: set[str] = set()
        deduped: list[dict] = []
        for a in seed_artists:
            key = a["name"].lower()
            if key not in seen:
                seen.add(key)
                deduped.append(a)
        seed_artists = deduped

        seed_artist_names = {a["name"].lower() for a in seed_artists}

        # 2. Build candidate pool from similar artists
        candidates = build_candidate_pool(
            seed_artists,
            int(cfg["similar_per_artist"]),
            float(cfg["min_similarity"]),
            str(cfg["similar_algorithm"]),
        )
        log.info("Candidate pool size: %d artists", len(candidates))

        # 3. Remove artists already in Lidarr or already added by this service
        lidarr_names = get_lidarr_artist_names()
        log.info("Artists already in Lidarr: %d", len(lidarr_names))

        filtered = {
            k: v
            for k, v in candidates.items()
            if k not in lidarr_names
            and k not in seed_artist_names
            and not was_already_added(conn, v["name"], v["mbid"])
        }
        log.info("Candidates after filtering: %d", len(filtered))

        # 4. Sort by score, take top N
        ranked = sorted(filtered.values(), key=lambda x: x["score"], reverse=True)
        to_add = ranked[: int(cfg["max_new_artists"])]

        if not to_add:
            status = "no_candidates"
            log.info("No new artists to add this run.")
            conn.execute(
                "INSERT INTO runs (run_at, artists_added, status) VALUES (?,?,?)",
                (datetime.utcnow().isoformat(), 0, status),
            )
            conn.commit()
            result = {
                "status": status,
                "artists_added": 0,
                "seeds_used": len(seed_artists),
                "candidates": len(candidates),
                "filtered": len(filtered),
            }
            _set_last_run({**result, "started_at": started_at, "finished_at": datetime.utcnow().isoformat()})
            return result

        # 5. Fetch Lidarr config once
        quality_id, metadata_id = get_lidarr_profiles()
        tag_id = get_or_create_discovered_tag()

        # 6. Add each artist to Lidarr
        added_count = 0
        for candidate in to_add:
            name = candidate["name"]
            score = candidate["score"]
            log.info("Adding artist: %s (score=%.3f)", name, score)

            artist_data = lookup_artist_in_lidarr(name)
            if not artist_data:
                log.warning("Could not find '%s' in MusicBrainz via Lidarr, skipping.", name)
                continue

            result = add_artist_to_lidarr(artist_data, quality_id, metadata_id, tag_id)
            if result and result.get("id"):
                lidarr_id = result["id"]
                record_added(conn, name, candidate["mbid"], lidarr_id)
                log.info("Added '%s' to Lidarr (id=%d)", name, lidarr_id)
                added_count += 1
            else:
                log.warning("Failed to add '%s' to Lidarr.", name)

            time.sleep(1)

        conn.execute(
            "INSERT INTO runs (run_at, artists_added, status) VALUES (?,?,?)",
            (datetime.utcnow().isoformat(), added_count, "ok"),
        )
        conn.commit()
        conn.close()

        log.info("Discovery run complete. Added %d new artists.", added_count)
        result = {
            "status": "ok",
            "artists_added": added_count,
            "seeds_used": len(seed_artists),
            "candidates": len(candidates),
            "filtered": len(filtered),
        }
        _set_last_run({**result, "started_at": started_at, "finished_at": datetime.utcnow().isoformat()})
        return result

    except Exception as e:
        log.exception("Unhandled error during discovery run: %s", e)
        result = {"status": "error", "error": str(e)}
        _set_last_run({**result, "started_at": started_at, "finished_at": datetime.utcnow().isoformat()})
        return result
    finally:
        _run_lock.release()


def _scheduler_loop() -> None:
    while not _scheduler_stop.is_set():
        cfg = _coerce_settings(effective_settings())
        run_discovery(cfg)
        sleep_seconds = int(cfg["run_interval_seconds"])
        log.info("Next run in %d seconds.", sleep_seconds)
        if _scheduler_stop.wait(timeout=sleep_seconds):
            return


def start_scheduler_background() -> None:
    t = threading.Thread(target=_scheduler_loop, name="discovery-scheduler", daemon=True)
    t.start()


def _gui_auth_ok(req) -> bool:
    if not DISCOVERY_GUI_TOKEN:
        return True
    q = req.args.get("token", "")
    h = req.headers.get("X-Discovery-Token", "")
    return q == DISCOVERY_GUI_TOKEN or h == DISCOVERY_GUI_TOKEN


def _tail_log(lines: int) -> list[str]:
    if lines <= 0:
        lines = 1
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-lines:]
    except Exception:
        return list(_log_buffer)[-lines:]


def create_app() -> Flask:
    app = Flask(__name__)

    @app.before_request
    def _auth_gate():
        if request.path == "/":
            return None
        if _gui_auth_ok(request):
            return None
        return jsonify({"error": "unauthorized"}), 401

    @app.get("/")
    def index():
        # Keep this intentionally lightweight; API endpoints hold full data.
        return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Music Discovery</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; max-width: 1000px; }
    h1 { margin-bottom: 8px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 12px; }
    label { display: block; margin-top: 8px; font-size: 0.9rem; color: #444; }
    input, select { width: 100%; padding: 6px; margin-top: 4px; }
    button { margin-top: 10px; padding: 8px 10px; }
    pre { background: #111; color: #ddd; padding: 10px; overflow: auto; max-height: 280px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
    th, td { border-bottom: 1px solid #eee; padding: 6px; text-align: left; }
    .muted { color: #666; font-size: 0.85rem; }
  </style>
</head>
<body>
  <h1>Music Discovery</h1>
  <p class="muted">Use ?token=YOUR_TOKEN if DISCOVERY_GUI_TOKEN is enabled.</p>

  <div class="grid">
    <div class="card">
      <h3>Run</h3>
      <button id="btnRun">Run discovery now</button>
      <div id="runNote" class="muted"></div>
      <h4>Last run</h4>
      <pre id="lastRun"></pre>
    </div>
    <div class="card">
      <h3>Settings</h3>
      <form id="settingsForm">
        <label>Seed mode
          <select name="seeds_mode">
            <option value="most_listened">most_listened</option>
            <option value="loved">loved</option>
            <option value="both">both</option>
          </select>
        </label>
        <label>Top artists count <input name="top_artists_count" type="number" min="1"></label>
        <label>Loved feedback count <input name="loved_feedback_count" type="number" min="1"></label>
        <label>Stats range <input name="listenbrainz_stats_range"></label>
        <label>Similar per artist <input name="similar_per_artist" type="number" min="1"></label>
        <label>Max new artists <input name="max_new_artists" type="number" min="1"></label>
        <label>Min similarity (0-1) <input name="min_similarity" type="number" step="0.01" min="0" max="1"></label>
        <label>Run interval seconds <input name="run_interval_seconds" type="number" min="60"></label>
        <label>Similar algorithm <input name="similar_algorithm"></label>
        <button type="submit">Save settings</button>
      </form>
      <div id="saveNote" class="muted"></div>
    </div>
  </div>

  <div class="card" style="margin-top:16px;">
    <h3>Recent runs</h3>
    <table><thead><tr><th>run_at</th><th>status</th><th>added</th></tr></thead><tbody id="runsBody"></tbody></table>
  </div>
  <div class="card" style="margin-top:16px;">
    <h3>Recently added artists</h3>
    <table><thead><tr><th>name</th><th>added_at</th><th>lidarr_id</th></tr></thead><tbody id="addedBody"></tbody></table>
  </div>
  <div class="card" style="margin-top:16px;">
    <h3>Log tail</h3>
    <button id="btnLog">Refresh log</button>
    <pre id="logBox"></pre>
  </div>

  <script>
    const token = new URLSearchParams(window.location.search).get('token') || '';
    async function api(path, opts={}) {
      const u = token ? `${path}${path.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}` : path;
      return fetch(u, opts);
    }
    function fill(form, data) {
      for (const [k,v] of Object.entries(data)) {
        if (form.elements[k]) form.elements[k].value = v;
      }
    }
    async function loadConfig() {
      const r = await api('/api/config');
      const j = await r.json();
      fill(document.getElementById('settingsForm'), j.config || {});
    }
    async function loadLast() {
      const r = await api('/api/last');
      const j = await r.json();
      document.getElementById('lastRun').textContent = JSON.stringify(j.last_run || {}, null, 2);
    }
    async function loadRuns() {
      const r = await api('/api/runs?limit=25');
      const j = await r.json();
      document.getElementById('runsBody').innerHTML = (j.runs || [])
        .map(x => `<tr><td>${x.run_at || ''}</td><td>${x.status || ''}</td><td>${x.artists_added ?? ''}</td></tr>`).join('');
    }
    async function loadAdded() {
      const r = await api('/api/added?limit=60');
      const j = await r.json();
      document.getElementById('addedBody').innerHTML = (j.added || [])
        .map(x => `<tr><td>${x.name || ''}</td><td>${x.added_at || ''}</td><td>${x.lidarr_id ?? ''}</td></tr>`).join('');
    }
    async function loadLog() {
      const r = await api('/api/log?lines=200');
      const j = await r.json();
      document.getElementById('logBox').textContent = (j.lines || []).join('');
    }

    document.getElementById('btnRun').onclick = async () => {
      const note = document.getElementById('runNote');
      note.textContent = 'Starting...';
      const r = await api('/api/run', {method:'POST'});
      const j = await r.json();
      note.textContent = j.status || j.error || 'started';
      setTimeout(loadLast, 1000);
    };

    document.getElementById('settingsForm').onsubmit = async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      const payload = {};
      for (const [k, v] of fd.entries()) payload[k] = v;
      ['top_artists_count','loved_feedback_count','similar_per_artist','max_new_artists','run_interval_seconds']
        .forEach(k => payload[k] = parseInt(payload[k], 10));
      payload.min_similarity = parseFloat(payload.min_similarity);
      const r = await api('/api/config', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(payload)
      });
      const j = await r.json();
      document.getElementById('saveNote').textContent = j.ok ? 'Saved.' : (j.error || 'Error');
      await loadConfig();
    };

    document.getElementById('btnLog').onclick = loadLog;
    loadConfig(); loadLast(); loadRuns(); loadAdded(); loadLog();
    setInterval(loadLast, 8000);
    setInterval(loadRuns, 15000);
  </script>
</body>
</html>
"""

    @app.get("/api/config")
    def api_config_get():
        return jsonify({"config": _coerce_settings(effective_settings())})

    @app.post("/api/config")
    def api_config_set():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "invalid_json"}), 400
        save_settings_file(_coerce_settings(body))
        return jsonify({"ok": True, "config": _coerce_settings(effective_settings())})

    @app.post("/api/run")
    def api_run():
        if _run_lock.locked():
            return jsonify({"error": "discovery run already in progress"}), 409

        def _bg():
            run_discovery(_coerce_settings(effective_settings()))

        threading.Thread(target=_bg, name="manual-discovery-run", daemon=True).start()
        return jsonify({"status": "started"})

    @app.get("/api/last")
    def api_last():
        return jsonify({"last_run": _get_last_run()})

    @app.get("/api/runs")
    def api_runs():
        try:
            limit = max(1, min(200, int(request.args.get("limit", "50"))))
        except ValueError:
            limit = 50
        conn = init_db()
        rows = conn.execute(
            "SELECT run_at, artists_added, status FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return jsonify(
            {
                "runs": [
                    {"run_at": r[0], "artists_added": r[1], "status": r[2]}
                    for r in rows
                ]
            }
        )

    @app.get("/api/added")
    def api_added():
        try:
            limit = max(1, min(200, int(request.args.get("limit", "80"))))
        except ValueError:
            limit = 80
        conn = init_db()
        rows = conn.execute(
            "SELECT name, mbid, added_at, lidarr_id FROM added_artists ORDER BY added_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return jsonify(
            {
                "added": [
                    {"name": r[0], "mbid": r[1], "added_at": r[2], "lidarr_id": r[3]}
                    for r in rows
                ]
            }
        )

    @app.get("/api/log")
    def api_log():
        try:
            lines = int(request.args.get("lines", "200"))
        except ValueError:
            lines = 200
        return jsonify({"lines": _tail_log(max(1, min(1000, lines)))})

    return app


def main() -> None:
    _ensure_buffer_logging()
    log.info("Music Discovery Bridge starting up")
    log.info("  ListenBrainz user: %s", LISTENBRAINZ_USERNAME)
    log.info("  Lidarr URL       : %s", LIDARR_URL)
    cfg = _coerce_settings(effective_settings())
    log.info("  Seed mode        : %s", cfg["seeds_mode"])
    log.info("  Run interval     : %ds", int(cfg["run_interval_seconds"]))
    log.info("  GUI port         : %d (set DISCOVERY_GUI_PORT=0 to disable)", DISCOVERY_GUI_PORT)

    start_scheduler_background()

    if DISCOVERY_GUI_PORT <= 0:
        log.info("Web UI disabled (DISCOVERY_GUI_PORT<=0); scheduler will keep running.")
        while True:
            time.sleep(3600)

    app = create_app()
    app.run(host="0.0.0.0", port=DISCOVERY_GUI_PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
