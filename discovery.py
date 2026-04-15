#!/usr/bin/env python3
"""
Music Discovery Bridge
======================
Pulls seed artists from ListenBrainz (top listened and/or loved recordings),
finds similar artists via the Labs API, and adds new ones to Lidarr.

Optional web UI (Flask) on DISCOVERY_GUI_PORT for settings, stats, logs, and manual runs.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

# ── Paths / logging ──────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DISCOVERY_DATA_DIR", "/data"))
DB_PATH = str(DATA_DIR / "discovery.db")
SETTINGS_PATH = DATA_DIR / "settings.json"
LOG_PATH = DATA_DIR / "discovery.log"

_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _log_handlers.append(logging.FileHandler(LOG_PATH))
except OSError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger("discovery")

# ── Static config (secrets / deployment) ─────────────────────────────────────
LISTENBRAINZ_USERNAME = os.environ["LISTENBRAINZ_USERNAME"]
LISTENBRAINZ_TOKEN = os.environ.get("LISTENBRAINZ_TOKEN", "").strip()
LIDARR_URL = os.environ.get("LIDARR_URL", "http://lidarr:8686")
LIDARR_API_KEY = os.environ["LIDARR_API_KEY"]
LIDARR_ROOT_PATH = os.environ.get("LIDARR_ROOT_PATH", "/music")

LISTENBRAINZ_BASE = "https://api.listenbrainz.org"
LISTENBRAINZ_LABS_BASE = "https://labs.api.listenbrainz.org"

# Optional GUI auth: if set, require matching `?token=` or `X-Discovery-Token` header
DISCOVERY_GUI_TOKEN = os.environ.get("DISCOVERY_GUI_TOKEN", "").strip()
DISCOVERY_GUI_PORT = int(os.environ.get("DISCOVERY_GUI_PORT", "8765"))

DEFAULT_SETTINGS = {
    # Seeds: most_listened (LB stats), loved (LB recording feedback loves → MB artist),
    # both (union, de-duplicated).
    "seeds_mode": os.environ.get("DISCOVERY_SEED_MODE", "most_listened"),
    "top_artists_count": int(os.environ.get("TOP_ARTISTS_COUNT", "20")),
    "loved_feedback_count": int(os.environ.get("LOVED_FEEDBACK_COUNT", "100")),
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
_last_run: dict | None = None
_last_run_lock = threading.Lock()
_scheduler_thread: threading.Thread | None = None
_stop_scheduler = threading.Event()

# Recent log lines for the GUI (tail of log messages)
_log_buffer: deque[str] = deque(maxlen=400)
_log_handler_added = False


class BufferLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _log_buffer.append(self.format(record))
        except Exception:
            pass


def _ensure_buffer_logging() -> None:
    global _log_handler_added
    if _log_handler_added:
        return
    h = BufferLogHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(h)
    _log_handler_added = True


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
        log.warning("Could not read %s: %s", SETTINGS_PATH, e)
        return {}


def save_settings_file(updates: dict) -> None:
    with _settings_lock:
        current = {**DEFAULT_SETTINGS, **load_settings_file()}
        merged = _deep_merge(current, updates)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, sort_keys=True)
        f.flush()


def effective_settings() -> dict:
    """Defaults from env, overridden by /data/settings.json."""
    with _settings_lock:
        return {**DEFAULT_SETTINGS, **load_settings_file()}


def _coerce_settings(raw: dict) -> dict:
    """Validate and coerce user-facing settings keys."""
    out: dict = {}
    mode = str(raw.get("seeds_mode", DEFAULT_SETTINGS["seeds_mode"])).lower()
    if mode not in ("most_listened", "loved", "both"):
        mode = "most_listened"
    out["seeds_mode"] = mode

    def _pos_int(key: str, default: int, upper: int = 10_000) -> int:
        try:
            v = int(raw.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(1, min(upper, v))

    def _float_01(key: str, default: float) -> float:
        try:
            v = float(raw.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(0.0, min(1.0, v))

    out["top_artists_count"] = _pos_int("top_artists_count", DEFAULT_SETTINGS["top_artists_count"], 500)
    out["loved_feedback_count"] = _pos_int(
        "loved_feedback_count", DEFAULT_SETTINGS["loved_feedback_count"], 2000
    )
    ranges = (
        "week",
        "month",
        "quarter",
        "half_yearly",
        "year",
        "this_week",
        "this_month",
        "this_year",
        "all_time",
    )
    sr = str(raw.get("listenbrainz_stats_range", DEFAULT_SETTINGS["listenbrainz_stats_range"]))
    out["listenbrainz_stats_range"] = sr if sr in ranges else DEFAULT_SETTINGS["listenbrainz_stats_range"]
    out["similar_per_artist"] = _pos_int("similar_per_artist", DEFAULT_SETTINGS["similar_per_artist"], 100)
    out["max_new_artists"] = _pos_int("max_new_artists", DEFAULT_SETTINGS["max_new_artists"], 100)
    out["min_similarity"] = _float_01("min_similarity", float(DEFAULT_SETTINGS["min_similarity"]))
    out["run_interval_seconds"] = _pos_int(
        "run_interval_seconds", DEFAULT_SETTINGS["run_interval_seconds"], 86400 * 14
    )
    alg = str(raw.get("similar_algorithm", "") or DEFAULT_SETTINGS["similar_algorithm"]).strip()
    out["similar_algorithm"] = alg[:500] if alg else DEFAULT_SETTINGS["similar_algorithm"]
    return out


# ── Database ───────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS added_artists (
            mbid        TEXT,
            name        TEXT NOT NULL,
            added_at    TEXT NOT NULL,
            lidarr_id   INTEGER,
            PRIMARY KEY (mbid, name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at      TEXT NOT NULL,
            artists_added INTEGER,
            status      TEXT,
            seed_mode   TEXT,
            seeds_used  INTEGER,
            candidates  INTEGER,
            filtered    INTEGER
        )
        """
    )
    conn.commit()
    _migrate_runs(conn)
    return conn


def _migrate_runs(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    alters = []
    if "seed_mode" not in cols:
        alters.append("ALTER TABLE runs ADD COLUMN seed_mode TEXT")
    if "seeds_used" not in cols:
        alters.append("ALTER TABLE runs ADD COLUMN seeds_used INTEGER")
    if "candidates" not in cols:
        alters.append("ALTER TABLE runs ADD COLUMN candidates INTEGER")
    if "filtered" not in cols:
        alters.append("ALTER TABLE runs ADD COLUMN filtered INTEGER")
    for stmt in alters:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()


def was_already_added(conn, name: str, mbid: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM added_artists WHERE name = ? OR (mbid != '' AND mbid = ?)",
        (name.lower(), mbid),
    ).fetchone()
    return row is not None


def record_added(conn, name: str, mbid: str, lidarr_id: int):
    conn.execute(
        "INSERT OR IGNORE INTO added_artists (mbid, name, added_at, lidarr_id) VALUES (?,?,?,?)",
        (mbid, name.lower(), datetime.now(timezone.utc).isoformat(), lidarr_id),
    )
    conn.commit()


# ── ListenBrainz ─────────────────────────────────────────────────────────────
def _lb_headers() -> dict:
    headers = {"Content-Type": "application/json; charset=UTF-8"}
    if LISTENBRAINZ_TOKEN:
        headers["Authorization"] = f"Token {LISTENBRAINZ_TOKEN}"
    return headers


def _respect_listenbrainz_rate_limit_headers(r: requests.Response) -> None:
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


def listenbrainz_get(path: str, params: dict | None = None) -> dict:
    if not path.startswith("/"):
        path = "/" + path
    url = f"{LISTENBRAINZ_BASE}{path}"

    for attempt in range(3):
        try:
            r = requests.get(url, params=params or {}, headers=_lb_headers(), timeout=20)
            if r.status_code == 429:
                reset = r.headers.get("X-RateLimit-Reset-In") or r.headers.get("Retry-After")
                try:
                    wait = float(reset) + 1 if reset else 2**attempt
                except (TypeError, ValueError):
                    wait = 2**attempt
                log.warning("ListenBrainz rate limited, sleeping %.1fs", min(wait, 120))
                time.sleep(min(wait, 120))
                continue
            if r.status_code == 204:
                log.warning("ListenBrainz returned 204 (no data for this user/range).")
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
            time.sleep(2**attempt)
    return {}


def listenbrainz_post(path: str, body: dict) -> dict:
    if not path.startswith("/"):
        path = "/" + path
    url = f"{LISTENBRAINZ_BASE}{path}"
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, headers=_lb_headers(), timeout=25)
            if r.status_code == 429:
                time.sleep(min(2 ** (attempt + 1), 60))
                continue
            r.raise_for_status()
            _respect_listenbrainz_rate_limit_headers(r)
            data = r.json()
            return data if isinstance(data, dict) else {}
        except Exception as e:
            log.warning("ListenBrainz POST failed (attempt %d): %s", attempt + 1, e)
            time.sleep(2**attempt)
    return {}


def get_top_artists(username: str, count: int, stats_range: str) -> list[dict]:
    path = f"/1/stats/user/{quote(username, safe='')}/artists"
    data = listenbrainz_get(path, params={"count": count, "range": stats_range})
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
            artists.append({"name": name, "mbid": mbid, "listen_count": lc, "source": "most_listened"})
    log.info("Fetched %d top artists for user '%s'", len(artists), username)
    return artists


def _artist_from_metadata_block(meta: dict | None) -> tuple[str, str]:
    if not meta or not isinstance(meta, dict):
        return "", ""
    art = meta.get("artist")
    if not isinstance(art, dict):
        return "", ""
    artists = art.get("artists") or []
    if isinstance(artists, list) and artists:
        a0 = artists[0]
        if isinstance(a0, dict):
            name = a0.get("name") or ""
            mbid = a0.get("artist_mbid") or a0.get("mbid") or ""
            return str(name), str(mbid)
    name = art.get("name") or ""
    return str(name), ""


def _metadata_for_recordings(recording_mbids: list[str]) -> dict[str, dict]:
    """Batch map recording_mbid -> full metadata dict from LB."""
    out: dict[str, dict] = {}
    chunk: list[str] = []
    for mbid in recording_mbids:
        if not mbid:
            continue
        chunk.append(mbid)
        if len(chunk) >= 40:
            data = listenbrainz_post("/1/metadata/recording/", {"recording_mbids": chunk, "inc": "artist"})
            if isinstance(data, dict):
                out.update(data)
            chunk = []
    if chunk:
        data = listenbrainz_post("/1/metadata/recording/", {"recording_mbids": chunk, "inc": "artist"})
        if isinstance(data, dict):
            out.update(data)
    return out


def get_loved_seed_artists(username: str, max_feedback: int) -> list[dict]:
    """
    Loved recordings in ListenBrainz (MusicBrainz-linked metadata).
    Resolves recording MBIDs via /1/metadata/recording/ when needed.
    """
    collected: list[dict] = []
    seen_recording: set[str] = set()
    offset = 0
    page = min(100, max(25, max_feedback))
    max_fetch = max(max_feedback * 5, page)

    while offset < max_fetch:
        path = f"/1/feedback/user/{quote(username, safe='')}/get-feedback"
        data = listenbrainz_get(
            path,
            params={"score": 1, "count": page, "offset": offset, "metadata": "true"},
        )
        feedback = data.get("feedback") or []
        if not feedback:
            break
        need_meta: list[str] = []
        for item in feedback:
            if not isinstance(item, dict):
                continue
            rec_mbid = (item.get("recording_mbid") or "").strip()
            rec_msid = (item.get("recording_msid") or "").strip()
            dedupe_key = rec_mbid or rec_msid
            if dedupe_key and dedupe_key in seen_recording:
                continue
            if dedupe_key:
                seen_recording.add(dedupe_key)

            tm = item.get("track_metadata")
            name, mbid = "", ""
            if isinstance(tm, dict):
                name = tm.get("artist_name") or tm.get("artist") or ""
                mbids = tm.get("artist_mbids") or tm.get("mbids") or []
                if isinstance(mbids, list) and mbids:
                    mbid = str(mbids[0])
                if not mbid:
                    mm = tm.get("mbid_mapping")
                    if isinstance(mm, dict):
                        amb = mm.get("artist_mbids")
                        if isinstance(amb, list) and amb:
                            mbid = str(amb[0])
            if name and mbid:
                collected.append({"name": name, "mbid": mbid, "listen_count": 0, "source": "loved"})
            elif rec_mbid:
                need_meta.append(rec_mbid)
            elif name:
                # No MBID — similarity API cannot use it; still collect name for logging/skipped later
                collected.append({"name": name, "mbid": "", "listen_count": 0, "source": "loved"})

        if need_meta:
            meta_by_rec = _metadata_for_recordings(need_meta)
            for rmbid in need_meta:
                block = meta_by_rec.get(rmbid)
                n, m = _artist_from_metadata_block(block)
                if n:
                    collected.append({"name": n, "mbid": m, "listen_count": 0, "source": "loved"})

        offset += len(feedback)
        if len(feedback) < page:
            break

        uniq_tmp: dict[str, dict] = {}
        for a in collected:
            k = (a.get("mbid") or "").strip().lower() or (a.get("name") or "").lower()
            if not k:
                continue
            if k not in uniq_tmp:
                uniq_tmp[k] = a
        with_mbid = sum(1 for v in uniq_tmp.values() if (v.get("mbid") or "").strip())
        if with_mbid >= max_feedback:
            break

    # De-duplicate by artist mbid or name
    uniq: dict[str, dict] = {}
    for a in collected:
        key = (a.get("mbid") or "").strip().lower() or (a.get("name") or "").lower()
        if not key:
            continue
        if key not in uniq:
            uniq[key] = a
    out = list(uniq.values())[:max_feedback]
    log.info("Built %d seed artists from loved recordings", len(out))
    return out


def merge_seed_lists(*lists: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for lst in lists:
        for a in lst:
            name = (a.get("name") or "").strip()
            if not name:
                continue
            mbid = (a.get("mbid") or "").strip()
            key = mbid.lower() if mbid else name.lower()
            prev = merged.get(key)
            if prev is None:
                merged[key] = dict(a)
            else:
                # Prefer entry with MBID
                if not (prev.get("mbid") or "").strip() and mbid:
                    merged[key] = dict(a)
                else:
                    prev["listen_count"] = max(
                        int(prev.get("listen_count") or 0),
                        int(a.get("listen_count") or 0),
                    )
                    src = prev.get("source", "")
                    nsrc = a.get("source", "")
                    if src and nsrc and src != nsrc:
                        prev["source"] = "both"
                    elif not src:
                        prev["source"] = nsrc
    return list(merged.values())


def get_similar_artists(artist_mbid: str, limit: int, algorithm: str) -> list[dict]:
    url = f"{LISTENBRAINZ_LABS_BASE}/similar-artists/json"
    params = {"artist_mbids": artist_mbid, "algorithm": algorithm}
    headers = {"Content-Type": "application/json; charset=UTF-8"}
    if LISTENBRAINZ_TOKEN:
        headers["Authorization"] = f"Token {LISTENBRAINZ_TOKEN}"

    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                reset = r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset-In")
                try:
                    wait = float(reset) + 1 if reset else 2**attempt
                except (TypeError, ValueError):
                    wait = 2**attempt
                log.warning("ListenBrainz Labs rate limited, sleeping %.1fs", min(wait, 120))
                time.sleep(min(wait, 120))
                continue
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            log.warning("ListenBrainz Labs request failed (attempt %d): %s", attempt + 1, e)
            time.sleep(2**attempt)
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


# ── Lidarr ────────────────────────────────────────────────────────────────────
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
    artists = lidarr_get("artist") or []
    return {a["artistName"].lower() for a in artists}


def get_lidarr_profiles() -> tuple[int, int]:
    quality = lidarr_get("qualityprofile") or []
    metadata = lidarr_get("metadataprofile") or []
    q_id = quality[0]["id"] if quality else 1
    m_id = metadata[0]["id"] if metadata else 1
    return q_id, m_id


def get_or_create_discovered_tag() -> int | None:
    tags = lidarr_get("tag") or []
    for tag in tags:
        if tag["label"].lower() == "discovered":
            return tag["id"]
    result = lidarr_post("tag", {"label": "discovered"})
    if result:
        log.info("Created 'discovered' tag in Lidarr (id=%d)", result["id"])
        return result["id"]
    return None


def lookup_artist_in_lidarr(artist_name: str) -> dict | None:
    results = lidarr_get("artist/lookup", term=artist_name)
    if results:
        return results[0]
    return None


def add_artist_to_lidarr(
    artist_data: dict, quality_id: int, metadata_id: int, tag_id: int | None
) -> dict | None:
    body = {
        **artist_data,
        "qualityProfileId": quality_id,
        "metadataProfileId": metadata_id,
        "rootFolderPath": LIDARR_ROOT_PATH,
        "monitored": True,
        "tags": [tag_id] if tag_id else [],
        "addOptions": {
            "monitor": "latest",
            "searchForMissingAlbums": True,
        },
    }
    return lidarr_post("artist", body)


# ── Discovery core ────────────────────────────────────────────────────────────
def build_candidate_pool(top_artists: list[dict], cfg: dict) -> dict[str, dict]:
    candidates: dict[str, dict] = {}
    min_sim = float(cfg["min_similarity"])
    per = int(cfg["similar_per_artist"])
    algorithm = str(cfg["similar_algorithm"])

    for seed in top_artists:
        seed_name = seed.get("name", "")
        seed_mbid = (seed.get("mbid") or "").strip()
        if not seed_name:
            continue
        if not seed_mbid:
            log.warning(
                "Skipping seed artist %r: no MusicBrainz ID (needed for similar-artists API)",
                seed_name,
            )
            continue
        log.info("Finding artists similar to: %s", seed_name)
        similar = get_similar_artists(seed_mbid, per, algorithm)

        for artist in similar:
            name = artist.get("name", "")
            mbid = artist.get("mbid", "")
            try:
                score = float(artist.get("match", 0))
            except (ValueError, TypeError):
                score = 0.0

            if score < min_sim or not name:
                continue

            key = name.lower()
            if key in candidates:
                candidates[key]["score"] += score
            else:
                candidates[key] = {"name": name, "mbid": mbid, "score": score}

    return candidates


def run_discovery() -> dict:
    """Single discovery pass. Thread-safe; returns a summary dict for the UI."""
    global _last_run
    cfg = _coerce_settings(effective_settings())
    started = datetime.now(timezone.utc).isoformat()

    summary = {
        "started_at": started,
        "finished_at": None,
        "seed_mode": cfg["seeds_mode"],
        "seeds_used": 0,
        "candidates": 0,
        "filtered": 0,
        "added": 0,
        "added_names": [],
        "status": "running",
        "message": "",
    }

    conn = init_db()
    try:
        mode = cfg["seeds_mode"]
        seeds: list[dict] = []
        if mode == "most_listened":
            seeds = get_top_artists(
                LISTENBRAINZ_USERNAME,
                cfg["top_artists_count"],
                cfg["listenbrainz_stats_range"],
            )
        elif mode == "loved":
            seeds = get_loved_seed_artists(LISTENBRAINZ_USERNAME, cfg["loved_feedback_count"])
        else:
            a = get_top_artists(
                LISTENBRAINZ_USERNAME,
                cfg["top_artists_count"],
                cfg["listenbrainz_stats_range"],
            )
            b = get_loved_seed_artists(LISTENBRAINZ_USERNAME, cfg["loved_feedback_count"])
            seeds = merge_seed_lists(a, b)

        summary["seeds_used"] = len(seeds)

        if not seeds:
            summary["status"] = "no_seeds"
            summary["message"] = (
                "No seed artists returned. For most_listened, statistics may still be computing; "
                "for loved, ensure you have loved tracks in ListenBrainz and optionally set LISTENBRAINZ_TOKEN."
            )
            conn.execute(
                "INSERT INTO runs (run_at, artists_added, status, seed_mode, seeds_used, candidates, filtered) "
                "VALUES (?,?,?,?,?,?,?)",
                (started, 0, "no_seeds", mode, 0, 0, 0),
            )
            conn.commit()
            return summary

        top_artist_names = {a["name"].lower() for a in seeds}
        candidates = build_candidate_pool(seeds, cfg)
        summary["candidates"] = len(candidates)

        lidarr_names = get_lidarr_artist_names()
        filtered = {
            k: v
            for k, v in candidates.items()
            if k not in lidarr_names
            and k not in top_artist_names
            and not was_already_added(conn, v["name"], v["mbid"])
        }
        summary["filtered"] = len(filtered)

        ranked = sorted(filtered.values(), key=lambda x: x["score"], reverse=True)
        to_add = ranked[: int(cfg["max_new_artists"])]

        if not to_add:
            summary["status"] = "no_candidates"
            summary["message"] = "No new artists matched filters this run."
            conn.execute(
                "INSERT INTO runs (run_at, artists_added, status, seed_mode, seeds_used, candidates, filtered) "
                "VALUES (?,?,?,?,?,?,?)",
                (started, 0, "no_candidates", mode, len(seeds), len(candidates), len(filtered)),
            )
            conn.commit()
            return summary

        quality_id, metadata_id = get_lidarr_profiles()
        tag_id = get_or_create_discovered_tag()
        added_count = 0
        added_names: list[str] = []

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
                added_names.append(name)
            else:
                log.warning("Failed to add '%s' to Lidarr.", name)
            time.sleep(1)

        summary["added"] = added_count
        summary["added_names"] = added_names
        summary["status"] = "ok"
        summary["message"] = f"Added {added_count} artist(s)."
        conn.execute(
            "INSERT INTO runs (run_at, artists_added, status, seed_mode, seeds_used, candidates, filtered) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                started,
                added_count,
                "ok",
                mode,
                len(seeds),
                len(candidates),
                len(filtered),
            ),
        )
        conn.commit()
        return summary
    except Exception as e:
        log.exception("Discovery run failed: %s", e)
        summary["status"] = "error"
        summary["message"] = str(e)
        try:
            conn.execute(
                "INSERT INTO runs (run_at, artists_added, status, seed_mode, seeds_used, candidates, filtered) "
                "VALUES (?,?,?,?,?,?,?)",
                (started, 0, "error", cfg.get("seeds_mode"), 0, 0, 0),
            )
            conn.commit()
        except Exception:
            pass
        return summary
    finally:
        conn.close()
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        with _last_run_lock:
            _last_run = dict(summary)


def _scheduler_loop() -> None:
    while not _stop_scheduler.is_set():
        cfg = _coerce_settings(effective_settings())
        interval = int(cfg["run_interval_seconds"])
        with _run_lock:
            try:
                run_discovery()
            except Exception as e:
                log.exception("Unhandled scheduler error: %s", e)
        log.info("Next run in %d seconds.", interval)
        _stop_scheduler.wait(timeout=interval)


def start_scheduler_background() -> None:
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    t = threading.Thread(target=_scheduler_loop, name="discovery-scheduler", daemon=True)
    _scheduler_thread = t
    t.start()


# ── Flask GUI ────────────────────────────────────────────────────────────────
def _gui_auth_ok(req) -> bool:
    if not DISCOVERY_GUI_TOKEN:
        return True
    q = req.args.get("token", "")
    h = req.headers.get("X-Discovery-Token", "")
    return q == DISCOVERY_GUI_TOKEN or h == DISCOVERY_GUI_TOKEN


def create_app():
    from flask import Flask, jsonify, request, Response

    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False

    @app.before_request
    def _auth():
        if request.path.startswith("/api/") or request.path in ("/", ""):
            if not _gui_auth_ok(request):
                return Response("Unauthorized", 401)

    @app.get("/api/config")
    def api_config():
        cfg = _coerce_settings(effective_settings())
        return jsonify(
            {
                "settings": cfg,
                "env": {
                    "listenbrainz_username": LISTENBRAINZ_USERNAME,
                    "listenbrainz_token_set": bool(LISTENBRAINZ_TOKEN),
                    "lidarr_url": LIDARR_URL,
                    "lidarr_api_key_set": bool(LIDARR_API_KEY),
                    "lidarr_root_path": LIDARR_ROOT_PATH,
                    "gui_auth_required": bool(DISCOVERY_GUI_TOKEN),
                },
            }
        )

    @app.post("/api/config")
    def api_config_save():
        body = request.get_json(force=True, silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "expected JSON object"}), 400
        cleaned = _coerce_settings({**effective_settings(), **body})
        save_settings_file(cleaned)
        return jsonify({"ok": True, "settings": cleaned})

    @app.get("/api/last_run")
    def api_last_run():
        with _last_run_lock:
            return jsonify(_last_run or {})

    @app.post("/api/run")
    def api_run():
        busy = not _run_lock.acquire(blocking=False)
        if busy:
            return jsonify({"error": "A run is already in progress."}), 409

        def _job():
            try:
                run_discovery()
            finally:
                _run_lock.release()

        threading.Thread(target=_job, name="discovery-manual-run", daemon=True).start()
        return jsonify({"ok": True, "started": True})

    @app.get("/api/runs")
    def api_runs():
        n = int(request.args.get("limit", "50"))
        n = max(1, min(200, n))
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id, run_at, artists_added, status, seed_mode, seeds_used, candidates, filtered "
            "FROM runs ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
        conn.close()
        keys = (
            "id",
            "run_at",
            "artists_added",
            "status",
            "seed_mode",
            "seeds_used",
            "candidates",
            "filtered",
        )
        return jsonify({"runs": [dict(zip(keys, r)) for r in rows]})

    @app.get("/api/added")
    def api_added():
        n = int(request.args.get("limit", "100"))
        n = max(1, min(500, n))
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT mbid, name, added_at, lidarr_id FROM added_artists ORDER BY added_at DESC LIMIT ?",
            (n,),
        ).fetchall()
        conn.close()
        return jsonify(
            {
                "added": [
                    {"mbid": r[0], "name": r[1], "added_at": r[2], "lidarr_id": r[3]} for r in rows
                ]
            }
        )

    @app.get("/api/log")
    def api_log():
        n = int(request.args.get("lines", "200"))
        n = max(1, min(500, n))
        lines = list(_log_buffer)[-n:]
        return jsonify({"lines": lines})

    @app.get("/")
    def index():
        return Response(_DASHBOARD_HTML, mimetype="text/html")

    return app


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Music Discovery</title>
  <style>
    :root { --bg:#0f1115; --panel:#1a1d24; --text:#e8eaed; --muted:#9aa0a6; --acc:#6c9cff; --ok:#3dd68c; --err:#ff6b6b;}
    body { font-family: system-ui, sans-serif; background: var(--bg); color: var(--text); margin:0; }
    header { padding:1rem 1.25rem; border-bottom:1px solid #2a2f3a; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:.5rem;}
    h1 { font-size:1.1rem; margin:0; font-weight:600; }
    nav { display:flex; gap:.4rem; flex-wrap:wrap; }
    nav button { background:var(--panel); color:var(--text); border:1px solid #2a2f3a; padding:.4rem .75rem; border-radius:6px; cursor:pointer;}
    nav button.active { border-color: var(--acc); color: var(--acc); }
    main { padding:1rem 1.25rem; max-width:960px; }
    section.panel { background:var(--panel); border:1px solid #2a2f3a; border-radius:8px; padding:1rem; margin-bottom:1rem;}
    label { display:block; font-size:.8rem; color:var(--muted); margin:.35rem 0 .15rem; }
    input, select { width:100%; max-width:420px; padding:.45rem .5rem; border-radius:6px; border:1px solid #2a2f3a; background:#12141a; color:var(--text);}
    .row { display:flex; gap:1rem; flex-wrap:wrap; align-items:flex-end; margin:.5rem 0;}
    .row > div { flex:1; min-width:200px;}
    button.primary { background:var(--acc); color:#0b1020; border:none; padding:.55rem 1rem; border-radius:6px; font-weight:600; cursor:pointer;}
    button.secondary { background:transparent; color:var(--acc); border:1px solid var(--acc); padding:.45rem .9rem; border-radius:6px; cursor:pointer;}
    pre.log { font-size:.72rem; background:#12141a; padding:.75rem; border-radius:6px; overflow:auto; max-height:320px; white-space:pre-wrap;}
    table { width:100%; border-collapse:collapse; font-size:.85rem;}
    th, td { text-align:left; padding:.35rem .25rem; border-bottom:1px solid #2a2f3a;}
    th { color:var(--muted); font-weight:500;}
    .stat { font-size:1.4rem; font-weight:700;}
    .muted { color:var(--muted); font-size:.85rem;}
    .ok { color: var(--ok);} .err { color: var(--err);}
  </style>
</head>
<body>
  <header>
    <h1>Music Discovery</h1>
    <nav id="tabs">
      <button type="button" data-tab="dash" class="active">Overview</button>
      <button type="button" data-tab="settings">Settings</button>
      <button type="button" data-tab="runs">Runs</button>
      <button type="button" data-tab="added">Added artists</button>
      <button type="button" data-tab="log">Log</button>
    </nav>
  </header>
  <main>
    <section id="tab-dash" class="panel">
      <p class="muted">ListenBrainz → similar artists → Lidarr. Seeds: <strong>most listened</strong> (stats),
      <strong>loved</strong> (ListenBrainz loved recordings / MusicBrainz metadata), or <strong>both</strong>.</p>
      <div class="row">
        <div><div class="stat" id="lastStatus">—</div><span class="muted">Last run status</span></div>
        <div><div class="stat" id="lastAdded">—</div><span class="muted">Artists added (last)</span></div>
        <div><div class="stat" id="lastSeeds">—</div><span class="muted">Seeds used</span></div>
      </div>
      <p id="lastMsg" class="muted"></p>
      <button type="button" class="primary" id="btnRun">Run discovery now</button>
      <span id="runNote" class="muted" style="margin-left:.75rem;"></span>
    </section>
    <section id="tab-settings" class="panel" style="display:none">
      <form id="formSettings">
        <div class="row">
          <div>
            <label for="seeds_mode">Seed source</label>
            <select id="seeds_mode" name="seeds_mode">
              <option value="most_listened">Most listened (ListenBrainz stats)</option>
              <option value="loved">Loved tracks (ListenBrainz → artists)</option>
              <option value="both">Both (merged)</option>
            </select>
          </div>
          <div>
            <label for="listenbrainz_stats_range">Stats range (most listened)</label>
            <select id="listenbrainz_stats_range" name="listenbrainz_stats_range">
              <option value="week">week</option>
              <option value="month">month</option>
              <option value="quarter">quarter</option>
              <option value="half_yearly">half_yearly</option>
              <option value="year">year</option>
              <option value="this_week">this_week</option>
              <option value="this_month">this_month</option>
              <option value="this_year">this_year</option>
              <option value="all_time">all_time</option>
            </select>
          </div>
        </div>
        <div class="row">
          <div>
            <label for="top_artists_count">Top artists count</label>
            <input type="number" id="top_artists_count" name="top_artists_count" min="1" max="500"/>
          </div>
          <div>
            <label for="loved_feedback_count">Loved recordings to scan</label>
            <input type="number" id="loved_feedback_count" name="loved_feedback_count" min="1" max="2000"/>
          </div>
        </div>
        <div class="row">
          <div>
            <label for="similar_per_artist">Similar artists per seed</label>
            <input type="number" id="similar_per_artist" name="similar_per_artist" min="1" max="100"/>
          </div>
          <div>
            <label for="max_new_artists">Max new artists per run</label>
            <input type="number" id="max_new_artists" name="max_new_artists" min="1" max="100"/>
          </div>
        </div>
        <div class="row">
          <div>
            <label for="min_similarity">Min similarity (0–1)</label>
            <input type="number" step="0.01" id="min_similarity" name="min_similarity" min="0" max="1"/>
          </div>
          <div>
            <label for="run_interval_seconds">Run interval (seconds)</label>
            <input type="number" id="run_interval_seconds" name="run_interval_seconds" min="60"/>
          </div>
        </div>
        <label for="similar_algorithm">Similar-artists algorithm id</label>
        <input id="similar_algorithm" name="similar_algorithm"/>
        <p class="muted">Secrets (ListenBrainz token, Lidarr API key) stay in Docker environment variables, not here.</p>
        <button type="submit" class="primary" style="margin-top:.75rem">Save settings</button>
        <span id="saveNote" class="muted" style="margin-left:.75rem;"></span>
      </form>
      <div style="margin-top:1rem" class="muted" id="envBox"></div>
    </section>
    <section id="tab-runs" class="panel" style="display:none">
      <table><thead><tr><th>When</th><th>Status</th><th>Added</th><th>Seeds</th><th>Cand.</th><th>Filt.</th><th>Mode</th></tr></thead><tbody id="runsBody"></tbody></table>
    </section>
    <section id="tab-added" class="panel" style="display:none">
      <table><thead><tr><th>Artist</th><th>MBID</th><th>Added</th><th>Lidarr id</th></tr></thead><tbody id="addedBody"></tbody></table>
    </section>
    <section id="tab-log" class="panel" style="display:none">
      <pre class="log" id="logBox"></pre>
      <button type="button" class="secondary" id="btnRefreshLog">Refresh</button>
    </section>
  </main>
  <script>
    const qs = () => {
      const t = new URLSearchParams(location.search).get('token');
      return t ? ('?token=' + encodeURIComponent(t)) : '';
    };
    const api = (path, opt={}) => {
      const u = path + qs();
      const h = {...(opt.headers||{})};
      const tok = new URLSearchParams(location.search).get('token');
      if (tok) h['X-Discovery-Token'] = tok;
      return fetch(u, {...opt, headers:h}).then(r => {
        if (r.status === 401) throw new Error('Unauthorized — add ?token= if DISCOVERY_GUI_TOKEN is set');
        return r;
      });
    };
    function showTab(name) {
      document.querySelectorAll('main > section').forEach(s => s.style.display = 'none');
      document.getElementById('tab-' + name).style.display = 'block';
      document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab===name));
    }
    document.getElementById('tabs').onclick = (e) => {
      const b = e.target.closest('button[data-tab]');
      if (b) { showTab(b.dataset.tab); if (b.dataset.tab==='runs') loadRuns(); if (b.dataset.tab==='added') loadAdded(); if (b.dataset.tab==='log') loadLog(); }
    };
    async function loadConfig() {
      const r = await api('/api/config');
      const j = await r.json();
      const s = j.settings;
      for (const k of Object.keys(s)) {
        const el = document.getElementById(k);
        if (el) el.value = s[k];
      }
      document.getElementById('envBox').textContent =
        'LB user: ' + j.env.listenbrainz_username +
        ' · token set: ' + j.env.listenbrainz_token_set +
        ' · Lidarr: ' + j.env.lidarr_url +
        ' · API key set: ' + j.env.lidarr_api_key_set +
        (j.env.gui_auth_required ? ' · GUI token required' : '');
    }
    async function loadLast() {
      const r = await api('/api/last_run');
      const j = await r.json();
      const st = document.getElementById('lastStatus');
      const ad = document.getElementById('lastAdded');
      const sd = document.getElementById('lastSeeds');
      const msg = document.getElementById('lastMsg');
      window.__discoveryLastRun = j;
      if (!j || !j.status) {
        st.textContent = '—'; ad.textContent = '—'; sd.textContent = '—'; msg.textContent = 'No run yet this session.';
        return;
      }
      st.textContent = j.status;
      st.className = j.status === 'ok' ? 'stat ok' : (j.status === 'error' ? 'stat err' : 'stat');
      ad.textContent = String(j.added ?? 0);
      sd.textContent = String(j.seeds_used ?? '—');
      msg.textContent = (j.message || '') + (j.added_names && j.added_names.length ? (' · ' + j.added_names.join(', ')) : '');
    }
    async function loadRuns() {
      const r = await api('/api/runs?limit=40');
      const j = await r.json();
      const tb = document.getElementById('runsBody');
      tb.innerHTML = j.runs.map(x => '<tr><td>'+x.run_at+'</td><td>'+x.status+'</td><td>'+x.artists_added+'</td><td>'+(x.seeds_used??'')+'</td><td>'+(x.candidates??'')+'</td><td>'+(x.filtered??'')+'</td><td>'+(x.seed_mode??'')+'</td></tr>').join('');
    }
    async function loadAdded() {
      const r = await api('/api/added?limit=80');
      const j = await r.json();
      document.getElementById('addedBody').innerHTML = j.added.map(x =>
        '<tr><td>'+x.name+'</td><td class="muted">'+(x.mbid||'')+'</td><td>'+x.added_at+'</td><td>'+x.lidarr_id+'</td></tr>').join('');
    }
    async function loadLog() {
      const r = await api('/api/log?lines=200');
      const j = await r.json();
      document.getElementById('logBox').textContent = j.lines.join('\n');
    }
    let __pollRunIv = null;
    document.getElementById('btnRun').onclick = async () => {
      const note = document.getElementById('runNote');
      note.textContent = 'Starting…';
      try {
        const r = await api('/api/run', {method:'POST'});
        const j = await r.json();
        if (r.status === 409) { note.textContent = j.error || 'Busy'; return; }
        note.textContent = 'Run started in background.';
        if (__pollRunIv) clearInterval(__pollRunIv);
        __pollRunIv = setInterval(async () => {
          await loadLast();
          const lr = window.__discoveryLastRun || {};
          if (lr.finished_at) {
            clearInterval(__pollRunIv);
            __pollRunIv = null;
            note.textContent = 'Finished.';
          }
        }, 1500);
        setTimeout(() => {
          if (__pollRunIv) { clearInterval(__pollRunIv); __pollRunIv = null; }
        }, 180000);
      } catch(e) { note.textContent = String(e); }
    };
    document.getElementById('formSettings').onsubmit = async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      const body = {};
      for (const [k,v] of fd.entries()) {
        if (['top_artists_count','loved_feedback_count','similar_per_artist','max_new_artists','run_interval_seconds'].includes(k))
          body[k] = parseInt(v,10);
        else if (k === 'min_similarity') body[k] = parseFloat(v);
        else body[k] = v;
      }
      const note = document.getElementById('saveNote');
      try {
        const r = await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
        const j = await r.json();
        note.textContent = j.ok ? 'Saved to /data/settings.json' : (j.error||'Error');
      } catch(e) { note.textContent = String(e); }
    };
    document.getElementById('btnRefreshLog').onclick = loadLog;
    loadConfig();
    loadLast();
    setInterval(loadLast, 8000);
  </script>
</body>
</html>
"""


def main():
    _ensure_buffer_logging()
    log.info("Music Discovery Bridge starting")
    log.info("  ListenBrainz user: %s", LISTENBRAINZ_USERNAME)
    log.info("  Lidarr URL: %s", LIDARR_URL)
    cfg = _coerce_settings(effective_settings())
    log.info("  Seed mode: %s", cfg["seeds_mode"])
    log.info("  GUI port: %d (set DISCOVERY_GUI_PORT=0 to disable)", DISCOVERY_GUI_PORT)

    start_scheduler_background()

    if DISCOVERY_GUI_PORT <= 0:
        log.info("Web GUI disabled (DISCOVERY_GUI_PORT<=0); blocking on scheduler thread.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            _stop_scheduler.set()
        return

    app = create_app()
    # threaded=True so manual /api/run does not block GETs
    app.run(host="0.0.0.0", port=DISCOVERY_GUI_PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
