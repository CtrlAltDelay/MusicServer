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
from datetime import datetime, timedelta
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
    _aa_cols = {row[1] for row in conn.execute("PRAGMA table_info(added_artists)")}
    if "run_id" not in _aa_cols:
        conn.execute("ALTER TABLE added_artists ADD COLUMN run_id INTEGER")
    conn.commit()
    return conn


def was_already_added(conn, name: str, mbid: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM added_artists WHERE name = ? OR (mbid != '' AND mbid = ?)",
        (name.lower(), mbid),
    ).fetchone()
    return row is not None


def record_added(conn, name: str, mbid: str, lidarr_id: int, run_id: int | None = None):
    conn.execute(
        "INSERT OR IGNORE INTO added_artists (mbid, name, added_at, lidarr_id, run_id) VALUES (?,?,?,?,?)",
        (mbid, name.lower(), datetime.utcnow().isoformat(), lidarr_id, run_id),
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


def listenbrainz_post(path: str, body: dict) -> requests.Response:
    if not path.startswith("/"):
        path = "/" + path
    url = f"{LISTENBRAINZ_BASE}{path}"
    headers = {"Content-Type": "application/json; charset=UTF-8"}
    if LISTENBRAINZ_TOKEN:
        headers["Authorization"] = f"Token {LISTENBRAINZ_TOKEN}"
    return requests.post(url, headers=headers, json=body, timeout=15)


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
        cur = conn.execute(
            "INSERT INTO runs (run_at, artists_added, status) VALUES (?,?,?)",
            (datetime.utcnow().isoformat(), 0, "running"),
        )
        current_run_id = int(cur.lastrowid)
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
                record_added(conn, name, candidate["mbid"], lidarr_id, current_run_id)
                log.info("Added '%s' to Lidarr (id=%d)", name, lidarr_id)
                added_count += 1
            else:
                log.warning("Failed to add '%s' to Lidarr.", name)

            time.sleep(1)

        conn.execute(
            "UPDATE runs SET run_at = ?, artists_added = ?, status = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), added_count, "ok", current_run_id),
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


def _next_run_at_iso(run_interval_seconds: int) -> str | None:
    last = _get_last_run()
    if not last:
        return None
    finished = last.get("finished_at")
    if not finished or not isinstance(finished, str):
        return None
    try:
        finished_dt = datetime.fromisoformat(finished)
    except ValueError:
        return None
    return (finished_dt + timedelta(seconds=int(run_interval_seconds))).isoformat()


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
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Discovery Bridge</title>
  <style>
    :root {
      --bg: #0f0f13;
      --surface: #16161d;
      --surface2: #1c1c26;
      --border: #2a2a36;
      --text: #e8e6ed;
      --muted: #8b8798;
      --accent: #a855f7;
      --accent-dim: #7c3aed;
      --ok: #22c55e;
      --warn: #eab308;
      --err: #ef4444;
      --radius: 10px;
      --font: "Segoe UI", system-ui, -apple-system, sans-serif;
      --mono: "Cascadia Code", "Consolas", "Ubuntu Mono", ui-monospace, monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      font-size: 15px;
      line-height: 1.5;
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .wrap { max-width: 1200px; margin: 0 auto; padding: 20px 20px 48px; }
    header {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 12px 20px;
      margin-bottom: 20px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }
    header h1 {
      margin: 0;
      font-size: 1.35rem;
      font-weight: 600;
      letter-spacing: -0.02em;
    }
    header h1 span { color: var(--accent); }
    .nav {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-left: auto;
    }
    .nav button {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--muted);
      padding: 8px 14px;
      border-radius: 8px;
      cursor: pointer;
      font: inherit;
      transition: color 0.15s, border-color 0.15s, background 0.15s;
    }
    .nav button:hover { color: var(--text); border-color: #444; }
    .nav button.active {
      color: var(--text);
      border-color: var(--accent);
      background: rgba(168, 85, 247, 0.12);
    }
    .hint { color: var(--muted); font-size: 0.8rem; margin: 0; }
    .panel { display: none; animation: fade 0.2s ease; }
    .panel.active { display: block; }
    @keyframes fade { from { opacity: 0; } to { opacity: 1; } }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 18px 20px;
      margin-bottom: 16px;
    }
    .card h2 {
      margin: 0 0 14px;
      font-size: 1rem;
      font-weight: 600;
      color: var(--text);
    }
    .status-bar {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .stat-pill {
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 12px 14px;
    }
    .stat-pill .label { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
    .stat-pill .value { font-size: 1.1rem; font-weight: 600; margin-top: 4px; }
    .pulse { animation: pulse 1.2s ease-in-out infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }
    .row-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-bottom: 16px; }
    .btn-primary {
      background: linear-gradient(180deg, var(--accent), var(--accent-dim));
      border: none;
      color: #fff;
      padding: 10px 20px;
      border-radius: 8px;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    .btn-primary:disabled { opacity: 0.45; cursor: not-allowed; }
    .spinner {
      width: 16px; height: 16px;
      border: 2px solid rgba(255,255,255,0.35);
      border-top-color: #fff;
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
      display: none;
    }
    .btn-primary.loading .spinner { display: inline-block; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .stat-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 10px;
    }
    .stat-box {
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      text-align: center;
    }
    .stat-box .n { font-size: 1.35rem; font-weight: 700; color: var(--accent); }
    .stat-box .l { font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 4px; }
    table.data {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.9rem;
    }
    table.data th, table.data td {
      padding: 10px 12px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }
    table.data th { color: var(--muted); font-weight: 600; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; }
    table.data tr.run-row { cursor: pointer; }
    table.data tr.run-row:hover { background: rgba(168, 85, 247, 0.06); }
    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .badge-ok { background: rgba(34, 197, 94, 0.2); color: var(--ok); }
    .badge-err { background: rgba(239, 68, 68, 0.2); color: var(--err); }
    .badge-warn { background: rgba(234, 179, 8, 0.2); color: var(--warn); }
    .badge-neu { background: rgba(139, 135, 152, 0.2); color: var(--muted); }
    .expand-detail td {
      background: var(--bg);
      padding: 12px 16px;
      font-size: 0.85rem;
      border-bottom: 1px solid var(--border);
    }
    .expand-detail ul { margin: 0; padding-left: 18px; }
    .search {
      width: 100%;
      max-width: 320px;
      padding: 10px 12px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--surface2);
      color: var(--text);
      font: inherit;
      margin-bottom: 14px;
    }
    .search::placeholder { color: var(--muted); }
    .settings-section { margin-bottom: 28px; }
    .settings-section h3 { margin: 0 0 14px; font-size: 0.95rem; color: var(--accent); }
    .field { margin-bottom: 18px; }
    .field label { display: block; font-weight: 600; margin-bottom: 6px; }
    .field .desc { font-size: 0.82rem; color: var(--muted); margin: 0 0 8px; line-height: 1.45; }
    .compare {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-bottom: 8px;
    }
    .compare small { display: block; color: var(--muted); font-size: 0.72rem; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.04em; }
    .field input, .field select, .field textarea {
      width: 100%;
      padding: 10px 12px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--surface2);
      color: var(--text);
      font: inherit;
    }
    .field textarea { min-height: 120px; font-family: var(--mono); font-size: 0.88rem; }
    .mono { font-family: var(--mono); font-size: 0.85rem; }
    .banner-warn {
      background: rgba(234, 179, 8, 0.12);
      border: 1px solid rgba(234, 179, 8, 0.35);
      color: #facc15;
      padding: 12px 14px;
      border-radius: var(--radius);
      margin-bottom: 16px;
      font-size: 0.9rem;
    }
    #toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      padding: 12px 18px;
      border-radius: 8px;
      font-weight: 500;
      box-shadow: 0 8px 32px rgba(0,0,0,0.45);
      z-index: 1000;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.25s;
    }
    #toast.show { opacity: 1; pointer-events: auto; }
    #toast.ok { background: var(--ok); color: #052e16; }
    #toast.err { background: var(--err); color: #fff; }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Music <span>Discovery</span> Bridge</h1>
      <nav class="nav" id="mainNav">
        <button type="button" data-tab="dash" class="active">Dashboard</button>
        <button type="button" data-tab="artists">Artists</button>
        <button type="button" data-tab="settings">Settings</button>
        <button type="button" data-tab="loves">Seed Loves</button>
      </nav>
    </header>
    <p class="hint">Auth: append <span class="mono">?token=…</span> when <span class="mono">DISCOVERY_GUI_TOKEN</span> is set.</p>

    <section id="panel-dash" class="panel active">
      <div class="status-bar" id="dashStatus"></div>
      <div class="row-actions">
        <button type="button" class="btn-primary" id="btnRun">
          <span class="spinner" aria-hidden="true"></span>
          <span id="btnRunLabel">Run Now</span>
        </button>
      </div>
      <div class="card">
        <h2>Last run</h2>
        <div class="stat-grid" id="lastRunStats"></div>
      </div>
      <div class="card">
        <h2>Recent runs</h2>
        <table class="data" id="runsTable">
          <thead><tr><th>When</th><th>Status</th><th>Added</th></tr></thead>
          <tbody id="runsBody"></tbody>
        </table>
      </div>
    </section>

    <section id="panel-artists" class="panel">
      <div class="card">
        <h2>Artists added by discovery</h2>
        <input type="search" class="search" id="artistSearch" placeholder="Filter by artist name…" autocomplete="off" />
        <table class="data">
          <thead><tr><th>Artist</th><th>Added</th><th>Lidarr</th><th>MusicBrainz</th></tr></thead>
          <tbody id="artistsBody"></tbody>
        </table>
      </div>
    </section>

    <section id="panel-settings" class="panel">
      <form id="settingsForm" class="card">
        <h2>Settings</h2>
        <div class="settings-section">
          <h3>Discovery tuning</h3>
          <div class="field">
            <label for="seeds_mode">Seed mode</label>
            <p class="desc">Where seed artists come from: play history stats, your ListenBrainz loved tracks, or both combined.</p>
            <div class="compare">
              <div><small>Current</small><select name="seeds_mode" id="seeds_mode">
                <option value="most_listened">most_listened</option>
                <option value="loved">loved</option>
                <option value="both">both</option>
              </select></div>
              <div><small>Env default</small><div class="mono env-val" data-k="seeds_mode"></div></div>
            </div>
          </div>
          <div class="field">
            <label for="top_artists_count">Top artists count</label>
            <p class="desc">How many top artists to pull from ListenBrainz statistics when using most-listened seeds.</p>
            <div class="compare">
              <div><small>Current</small><input name="top_artists_count" id="top_artists_count" type="number" min="1" /></div>
              <div><small>Env default</small><div class="mono env-val" data-k="top_artists_count"></div></div>
            </div>
          </div>
          <div class="field">
            <label for="loved_feedback_count">Loved feedback count</label>
            <p class="desc">Max loved recordings to read from ListenBrainz when building loved-artist seeds.</p>
            <div class="compare">
              <div><small>Current</small><input name="loved_feedback_count" id="loved_feedback_count" type="number" min="1" /></div>
              <div><small>Env default</small><div class="mono env-val" data-k="loved_feedback_count"></div></div>
            </div>
          </div>
          <div class="field">
            <label for="listenbrainz_stats_range">ListenBrainz stats range</label>
            <p class="desc">Time window for top-artist stats (e.g. half_yearly, year, all_time).</p>
            <div class="compare">
              <div><small>Current</small><input name="listenbrainz_stats_range" id="listenbrainz_stats_range" /></div>
              <div><small>Env default</small><div class="mono env-val" data-k="listenbrainz_stats_range"></div></div>
            </div>
          </div>
          <div class="field">
            <label for="similar_per_artist">Similar per artist</label>
            <p class="desc">How many similar artists to fetch per seed artist from ListenBrainz Labs.</p>
            <div class="compare">
              <div><small>Current</small><input name="similar_per_artist" id="similar_per_artist" type="number" min="1" /></div>
              <div><small>Env default</small><div class="mono env-val" data-k="similar_per_artist"></div></div>
            </div>
          </div>
          <div class="field">
            <label for="max_new_artists">Max new artists</label>
            <p class="desc">Cap on how many new artists to add to Lidarr in a single run.</p>
            <div class="compare">
              <div><small>Current</small><input name="max_new_artists" id="max_new_artists" type="number" min="1" /></div>
              <div><small>Env default</small><div class="mono env-val" data-k="max_new_artists"></div></div>
            </div>
          </div>
          <div class="field">
            <label for="min_similarity">Min similarity</label>
            <p class="desc">Minimum Labs similarity score (0–1) for a candidate to be considered.</p>
            <div class="compare">
              <div><small>Current</small><input name="min_similarity" id="min_similarity" type="number" step="0.01" min="0" max="1" /></div>
              <div><small>Env default</small><div class="mono env-val" data-k="min_similarity"></div></div>
            </div>
          </div>
          <div class="field">
            <label for="similar_algorithm">Similar algorithm</label>
            <p class="desc">ListenBrainz Labs algorithm id for the similar-artists endpoint.</p>
            <div class="compare">
              <div><small>Current</small><input name="similar_algorithm" id="similar_algorithm" /></div>
              <div><small>Env default</small><div class="mono env-val" data-k="similar_algorithm"></div></div>
            </div>
          </div>
        </div>
        <div class="settings-section">
          <h3>Scheduler</h3>
          <div class="field">
            <label for="run_interval_seconds">Run interval (seconds)</label>
            <p class="desc">How long the background scheduler waits between automatic discovery runs.</p>
            <div class="compare">
              <div><small>Current</small><input name="run_interval_seconds" id="run_interval_seconds" type="number" min="60" /></div>
              <div><small>Env default</small><div class="mono env-val" data-k="run_interval_seconds"></div></div>
            </div>
          </div>
        </div>
        <button type="submit" class="btn-primary">Save settings</button>
      </form>
    </section>

    <section id="panel-loves" class="panel">
      <div class="card">
        <h2>Seed Loves</h2>
        <div class="banner-warn" id="lovesTokenBanner" hidden>
          ListenBrainz user token is not configured (<span class="mono">LISTENBRAINZ_TOKEN</span>). Loving tracks from this UI will not work until it is set.
        </div>
        <p class="desc" style="margin-top:0">
          Submit tracks as &quot;loved&quot; on ListenBrainz to influence future discovery runs.
          Loved tracks are used as seeds when seed mode includes <span class="mono">loved</span>.
        </p>
        <div class="field">
          <label for="lovesText">Tracks (one per line)</label>
          <p class="desc">Format: <span class="mono">Artist Name - Track Title</span>, or a single line with just the artist name (lookup may need both artist and title).</p>
          <textarea id="lovesText" placeholder="Artist - Song&#10;Another Artist - Another Song"></textarea>
        </div>
        <button type="button" class="btn-primary" id="btnSubmitLoves">Submit</button>
        <div style="margin-top:20px" id="lovesResultsWrap" hidden>
          <h2 style="font-size:1rem;margin-bottom:10px">Results</h2>
          <table class="data">
            <thead><tr><th>Track</th><th>Result</th><th>Recording MBID</th></tr></thead>
            <tbody id="lovesResultsBody"></tbody>
          </table>
        </div>
      </div>
    </section>
  </div>
  <div id="toast" role="status"></div>

  <script>
(function () {
  const token = new URLSearchParams(window.location.search).get('token') || '';
  async function api(path, opts) {
    opts = opts || {};
    const u = token ? path + (path.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(token) : path;
    return fetch(u, opts);
  }

  function parseIso(s) {
    if (!s || typeof s !== 'string') return null;
    var d = Date.parse(s);
    if (!isNaN(d)) return new Date(d);
    try {
      return new Date(s);
    } catch (e) { return null; }
  }

  function formatRelative(iso) {
    var t = parseIso(iso);
    if (!t) return '—';
    var sec = Math.round((Date.now() - t.getTime()) / 1000);
    if (sec < 45) return 'just now';
    if (sec < 3600) return Math.floor(sec / 60) + ' min ago';
    if (sec < 86400) return Math.floor(sec / 3600) + ' hours ago';
    if (sec < 604800) return Math.floor(sec / 86400) + ' days ago';
    return t.toLocaleDateString();
  }

  function formatCountdown(iso) {
    var t = parseIso(iso);
    if (!t) return '—';
    var ms = t.getTime() - Date.now();
    if (ms <= 0) return 'due now';
    var s = Math.floor(ms / 1000);
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    var r = Math.floor(s % 60);
    if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
    if (h > 0) return h + 'h ' + m + 'm ' + r + 's';
    return m + 'm ' + r + 's';
  }

  function statusBadge(st) {
    var s = (st || '').toLowerCase();
    if (s === 'ok') return '<span class="badge badge-ok">ok</span>';
    if (s === 'error') return '<span class="badge badge-err">error</span>';
    if (s === 'no_candidates' || s === 'no_seeds' || s === 'busy') return '<span class="badge badge-warn">' + (st || '') + '</span>';
    return '<span class="badge badge-neu">' + (st || '—') + '</span>';
  }

  var state = {
    lidarrUrl: '',
    nextRunAt: null,
    artistsAll: [],
    runArtistsCache: {},
    countdownTimer: null,
    statusTimer: null
  };

  function fillForm(form, data) {
    if (!data) return;
    Object.keys(data).forEach(function (k) {
      var el = form.elements[k];
      if (el && 'value' in el) el.value = data[k];
    });
  }

  function renderDashStatus(s) {
    var running = s && s.is_running;
    var el = document.getElementById('dashStatus');
    var modeLabel = running
      ? '<span class="pulse">Running...</span>'
      : 'Idle';
    var nextIso = s && s.next_run_at;
    state.nextRunAt = nextIso || null;
    var lidarrN = s && typeof s.lidarr_artist_count === 'number' ? s.lidarr_artist_count : '—';
    var totalN = s && typeof s.total_artists_added === 'number' ? s.total_artists_added : '—';
    if (s && s.lidarr_url) state.lidarrUrl = s.lidarr_url;
    var lbTok = s && s.listenbrainz_token_configured;
    document.getElementById('lovesTokenBanner').hidden = !!lbTok;

    el.innerHTML =
      '<div class="stat-pill"><div class="label">Discovery</div><div class="value">' + modeLabel + '</div></div>' +
      '<div class="stat-pill"><div class="label">Next run in</div><div class="value" id="countdownVal">' + formatCountdown(nextIso) + '</div></div>' +
      '<div class="stat-pill"><div class="label">Lidarr artists</div><div class="value">' + lidarrN + '</div></div>' +
      '<div class="stat-pill"><div class="label">Total added (DB)</div><div class="value">' + totalN + '</div></div>';

    var btn = document.getElementById('btnRun');
    btn.disabled = !!running;
    btn.classList.toggle('loading', !!running);
  }

  function tickCountdown() {
    var node = document.getElementById('countdownVal');
    if (node && state.nextRunAt) node.textContent = formatCountdown(state.nextRunAt);
  }

  function statBox(label, val) {
    var v = (val !== undefined && val !== null) ? val : '—';
    return '<div class="stat-box"><div class="n">' + v + '</div><div class="l">' + label + '</div></div>';
  }

  function renderLastRun(last) {
    var box = document.getElementById('lastRunStats');
    if (!last) {
      box.innerHTML = '<p class="hint">No run yet.</p>';
      return;
    }
    box.innerHTML =
      statBox('Seeds used', last.seeds_used) +
      statBox('Candidates', last.candidates) +
      statBox('Filtered', last.filtered) +
      statBox('Artists added', last.artists_added);
  }

  function toggleRunDetail(runId, row) {
    var next = row.nextElementSibling;
    if (next && next.classList.contains('expand-detail')) {
      next.remove();
      return;
    }
    var open = document.querySelectorAll('tr.expand-detail');
    open.forEach(function (r) { r.remove(); });
    var tr = document.createElement('tr');
    tr.className = 'expand-detail';
    var td = document.createElement('td');
    td.colSpan = 3;
    td.innerHTML = 'Loading…';
    tr.appendChild(td);
    row.parentNode.insertBefore(tr, row.nextSibling);

    if (state.runArtistsCache[runId]) {
      td.innerHTML = renderArtistList(state.runArtistsCache[runId]);
      return;
    }
    api('/api/runs/' + runId + '/artists').then(function (r) { return r.json(); }).then(function (j) {
      var list = (j && j.artists) || [];
      state.runArtistsCache[runId] = list;
      td.innerHTML = renderArtistList(list);
    }).catch(function () {
      td.textContent = 'Failed to load artists.';
    });
  }

  function renderArtistList(artists) {
    if (!artists.length) return '<span class="hint">No artists linked to this run.</span>';
    var ul = '<ul>';
    artists.forEach(function (a) {
      ul += '<li><strong>' + escapeHtml(a.name || '') + '</strong>' +
        (a.lidarr_id != null ? ' · Lidarr ' + a.lidarr_id : '') + '</li>';
    });
    return ul + '</ul>';
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function renderRuns(runs) {
    var tb = document.getElementById('runsBody');
    tb.innerHTML = '';
    (runs || []).forEach(function (x) {
      var tr = document.createElement('tr');
      tr.className = 'run-row';
      var id = x.id;
      tr.innerHTML =
        '<td>' + formatRelative(x.run_at) + '</td>' +
        '<td>' + statusBadge(x.status) + '</td>' +
        '<td>' + (x.artists_added != null ? x.artists_added : '—') + '</td>';
      tr.addEventListener('click', function () {
        if (id != null) toggleRunDetail(id, tr);
      });
      tb.appendChild(tr);
    });
  }

  async function loadStatus() {
    var r = await api('/api/status');
    var s = await r.json();
    renderDashStatus(s);
    renderLastRun(s.last_run);
  }

  async function loadRuns() {
    var r = await api('/api/runs?limit=25');
    var j = await r.json();
    renderRuns(j.runs);
  }

  function renderArtistsTable(rows) {
    var q = (document.getElementById('artistSearch').value || '').trim().toLowerCase();
    var filtered = rows.filter(function (a) {
      return !q || (a.name && a.name.toLowerCase().indexOf(q) >= 0);
    });
    var base = state.lidarrUrl || '';
    var tb = document.getElementById('artistsBody');
    tb.innerHTML = filtered.map(function (x) {
      var lid = x.lidarr_id != null
        ? '<a href="' + escapeHtml(base + '/artist/' + x.lidarr_id) + '" target="_blank" rel="noopener">' + x.lidarr_id + '</a>'
        : '—';
      var mb = (x.mbid && String(x.mbid).trim())
        ? '<a href="https://musicbrainz.org/artist/' + escapeHtml(x.mbid) + '" target="_blank" rel="noopener">artist</a>'
        : '—';
      return '<tr><td>' + escapeHtml(x.name || '') + '</td><td>' + formatRelative(x.added_at) + '</td><td>' + lid + '</td><td>' + mb + '</td></tr>';
    }).join('');
  }

  async function loadArtists() {
    var r = await api('/api/added?limit=500');
    var j = await r.json();
    state.artistsAll = j.added || [];
    state.artistsAll.sort(function (a, b) {
      var ta = parseIso(a.added_at); var tb = parseIso(b.added_at);
      if (!ta || !tb) return 0;
      return tb - ta;
    });
    renderArtistsTable(state.artistsAll);
  }

  async function loadConfig() {
    var r = await api('/api/config');
    var j = await r.json();
    fillForm(document.getElementById('settingsForm'), j.config || {});
    var def = j.defaults || {};
    document.querySelectorAll('.env-val').forEach(function (node) {
      var k = node.getAttribute('data-k');
      if (k && def[k] !== undefined) node.textContent = String(def[k]);
    });
  }

  function showToast(msg, ok) {
    var t = document.getElementById('toast');
    t.textContent = msg;
    t.className = ok ? 'show ok' : 'show err';
    setTimeout(function () { t.classList.remove('show'); }, 3200);
  }

  document.getElementById('mainNav').addEventListener('click', function (e) {
    var btn = e.target.closest('button[data-tab]');
    if (!btn) return;
    var tab = btn.getAttribute('data-tab');
    document.querySelectorAll('.nav button').forEach(function (b) { b.classList.toggle('active', b === btn); });
    document.querySelectorAll('.panel').forEach(function (p) { p.classList.remove('active'); });
    document.getElementById('panel-' + tab).classList.add('active');
    if (tab === 'artists') loadArtists();
  });

  document.getElementById('artistSearch').addEventListener('input', function () {
    renderArtistsTable(state.artistsAll);
  });

  document.getElementById('btnRun').addEventListener('click', async function () {
    var r = await api('/api/run', { method: 'POST' });
    var j = await r.json();
    if (r.status === 409) showToast(j.error || 'Already running', false);
    else await loadStatus();
  });

  document.getElementById('settingsForm').addEventListener('submit', async function (ev) {
    ev.preventDefault();
    var fd = new FormData(ev.target);
    var payload = {};
    fd.forEach(function (v, k) { payload[k] = v; });
    ['top_artists_count','loved_feedback_count','similar_per_artist','max_new_artists','run_interval_seconds'].forEach(function (k) {
      payload[k] = parseInt(payload[k], 10);
    });
    payload.min_similarity = parseFloat(payload.min_similarity);
    var r = await api('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    var j = await r.json();
    if (j.ok) {
      showToast('Settings saved.', true);
      await loadConfig();
    } else showToast(j.error || 'Save failed', false);
  });

  function parseLovesLines(text) {
    var recs = [];
    text.split(String.fromCharCode(10)).forEach(function (line) {
      var t = line.trim();
      if (!t) return;
      var idx = t.indexOf(' - ');
      if (idx < 0) recs.push({ artist: t, track: '' });
      else recs.push({ artist: t.slice(0, idx).trim(), track: t.slice(idx + 3).trim() });
    });
    return recs;
  }

  document.getElementById('btnSubmitLoves').addEventListener('click', async function () {
    var raw = document.getElementById('lovesText').value;
    var recordings = parseLovesLines(raw);
    if (!recordings.length) {
      showToast('Add at least one line.', false);
      return;
    }
    var r = await api('/api/listenbrainz/submit-loves', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ recordings: recordings })
    });
    var j = await r.json();
    var wrap = document.getElementById('lovesResultsWrap');
    var tbody = document.getElementById('lovesResultsBody');
    if (r.status === 400 && j.error === 'token_required') {
      showToast('ListenBrainz token required.', false);
      wrap.hidden = false;
      tbody.innerHTML = '';
      return;
    }
    wrap.hidden = false;
    tbody.innerHTML = (j.results || []).map(function (row) {
      var track = [row.artist, row.track].filter(Boolean).join(' — ') || '—';
      var ok = row.status === 'ok';
      var mbid = row.recording_mbid || '—';
      return '<tr><td>' + escapeHtml(track) + '</td><td>' +
        (ok ? '<span class="badge badge-ok">submitted</span>' : '<span class="badge badge-err">failed</span>') +
        '</td><td class="mono">' + escapeHtml(String(mbid)) + '</td></tr>';
    }).join('');
    showToast('Done: ' + (j.submitted || 0) + ' ok, ' + (j.failed || 0) + ' failed', (j.failed || 0) === 0);
  });

  state.statusTimer = setInterval(loadStatus, 5000);
  state.countdownTimer = setInterval(tickCountdown, 1000);
  loadStatus();
  loadRuns();
  loadConfig();
  setInterval(loadRuns, 15000);
})();
  </script>
</body>
</html>
"""

    @app.get("/api/config")
    def api_config_get():
        cfg = _coerce_settings(effective_settings())
        defaults = _coerce_settings(dict(DEFAULT_SETTINGS))
        return jsonify({"config": cfg, "defaults": defaults})

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

    @app.get("/api/status")
    def api_status():
        cfg = _coerce_settings(effective_settings())
        interval = int(cfg["run_interval_seconds"])
        conn = init_db()
        total_added = conn.execute("SELECT COUNT(*) FROM added_artists").fetchone()[0]
        conn.close()
        return jsonify(
            {
                "is_running": _run_lock.locked(),
                "next_run_at": _next_run_at_iso(interval),
                "last_run": _get_last_run(),
                "lidarr_artist_count": len(get_lidarr_artist_names()),
                "total_artists_added": int(total_added),
                "lidarr_url": LIDARR_URL.rstrip("/"),
                "listenbrainz_token_configured": bool(LISTENBRAINZ_TOKEN),
            }
        )

    @app.get("/api/runs/<int:run_id>/artists")
    def api_run_artists(run_id: int):
        conn = init_db()
        try:
            exists = conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not exists:
                return jsonify({"error": "not_found"}), 404
            rows = conn.execute(
                "SELECT name, mbid, added_at, lidarr_id, run_id FROM added_artists WHERE run_id = ? ORDER BY added_at ASC",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
        return jsonify(
            {
                "artists": [
                    {"name": r[0], "mbid": r[1], "added_at": r[2], "lidarr_id": r[3], "run_id": r[4]}
                    for r in rows
                ]
            }
        )

    @app.post("/api/listenbrainz/submit-loves")
    def api_listenbrainz_submit_loves():
        if not LISTENBRAINZ_TOKEN:
            return jsonify({"error": "token_required"}), 400
        body = request.get_json(silent=True) or {}
        raw_list = body.get("recordings")
        if not isinstance(raw_list, list):
            return jsonify({"error": "invalid_json", "message": "recordings must be a list"}), 400

        submitted = 0
        failed = 0
        results: list[dict] = []

        for item in raw_list:
            if not isinstance(item, dict):
                failed += 1
                results.append({"status": "failed", "error": "invalid_item"})
                continue
            artist = item.get("artist")
            track = item.get("track")
            mbid_raw = item.get("mbid")
            artist_s = artist.strip() if isinstance(artist, str) else ""
            track_s = track.strip() if isinstance(track, str) else ""
            mbid = mbid_raw.strip() if isinstance(mbid_raw, str) else ""
            base = {"artist": artist_s or None, "track": track_s or None}

            recording_mbid = mbid or None
            if not recording_mbid:
                if not artist_s or not track_s:
                    failed += 1
                    results.append({**base, "status": "failed", "error": "missing_artist_or_track"})
                    continue
                lookup = listenbrainz_get(
                    "/1/metadata/lookup",
                    params={"artist_name": artist_s, "recording_name": track_s},
                )
                recording_mbid = (lookup.get("recording_mbid") or "").strip() or None
                if not recording_mbid:
                    failed += 1
                    results.append({**base, "status": "failed", "error": "recording_not_found"})
                    continue

            r = listenbrainz_post(
                "/1/feedback/recording-feedback",
                {"recording_mbid": recording_mbid, "score": 1},
            )
            _respect_listenbrainz_rate_limit_headers(r)
            if r.status_code == 200:
                submitted += 1
                results.append(
                    {**base, "status": "ok", "recording_mbid": recording_mbid}
                )
            else:
                failed += 1
                try:
                    detail = r.json()
                except Exception:
                    detail = {"detail": r.text}
                results.append(
                    {
                        **base,
                        "status": "failed",
                        "error": "feedback_rejected",
                        "http_status": r.status_code,
                        "detail": detail,
                    }
                )

        return jsonify({"submitted": submitted, "failed": failed, "results": results})

    @app.get("/api/runs")
    def api_runs():
        try:
            limit = max(1, min(200, int(request.args.get("limit", "50"))))
        except ValueError:
            limit = 50
        conn = init_db()
        rows = conn.execute(
            "SELECT id, run_at, artists_added, status FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return jsonify(
            {
                "runs": [
                    {"id": r[0], "run_at": r[1], "artists_added": r[2], "status": r[3]}
                    for r in rows
                ]
            }
        )

    @app.get("/api/added")
    def api_added():
        try:
            limit = max(1, min(500, int(request.args.get("limit", "80"))))
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
