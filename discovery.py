#!/usr/bin/env python3
"""
Music Discovery Bridge
======================
Pulls your top artists from Last.fm, finds similar artists,
and automatically adds new ones to Lidarr for download.

Flow:
  1. Fetch your top artists from Last.fm (user.getTopArtists)
  2. For each top artist, get similar artists (artist.getSimilar)
  3. Score candidates by similarity + how many of your artists link to them
  4. Filter out artists already in Lidarr
  5. Add the top N new artists to Lidarr, tagged as "discovered"
  6. Trigger Lidarr to search for their latest album
"""

import os
import time
import json
import logging
import sqlite3
import requests
from datetime import datetime, timedelta
from pathlib import Path

# ── Logging ──────────────────────────────────────────────────────────────────
Path("/data").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/data/discovery.log"),
    ],
)
log = logging.getLogger("discovery")

# ── Config (from environment variables) ──────────────────────────────────────
LASTFM_API_KEY    = os.environ["LASTFM_API_KEY"]
LASTFM_USERNAME   = os.environ["LASTFM_USERNAME"]
LIDARR_URL        = os.environ.get("LIDARR_URL", "http://lidarr:8686")
LIDARR_API_KEY    = os.environ["LIDARR_API_KEY"]
LIDARR_ROOT_PATH  = os.environ.get("LIDARR_ROOT_PATH", "/music")

# How many of your top artists to use as seeds (more = broader recommendations)
TOP_ARTISTS_COUNT = int(os.environ.get("TOP_ARTISTS_COUNT", "20"))
# Similar artists to fetch per seed artist
SIMILAR_PER_ARTIST = int(os.environ.get("SIMILAR_PER_ARTIST", "10"))
# Maximum new artists to add to Lidarr per run
MAX_NEW_ARTISTS   = int(os.environ.get("MAX_NEW_ARTISTS", "5"))
# Minimum similarity score (0.0–1.0) for a candidate to qualify
MIN_SIMILARITY    = float(os.environ.get("MIN_SIMILARITY", "0.25"))
# How often to run (seconds). Default: every 24 hours
RUN_INTERVAL      = int(os.environ.get("RUN_INTERVAL_SECONDS", str(24 * 60 * 60)))

LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
DB_PATH = "/data/discovery.db"

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


# ── Last.fm helpers ───────────────────────────────────────────────────────────
def lastfm_get(method: str, **params) -> dict:
    payload = {
        "method": method,
        "api_key": LASTFM_API_KEY,
        "format": "json",
        **params,
    }
    for attempt in range(3):
        try:
            r = requests.get(LASTFM_BASE, params=payload, timeout=10)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                log.warning("Last.fm error %s: %s", data["error"], data.get("message"))
                return {}
            return data
        except Exception as e:
            log.warning("Last.fm request failed (attempt %d): %s", attempt + 1, e)
            time.sleep(2 ** attempt)
    return {}


def get_top_artists(username: str, count: int) -> list[dict]:
    """Return user's top artists as [{"name": ..., "mbid": ..., "playcount": ...}]"""
    data = lastfm_get(
        "user.getTopArtists",
        user=username,
        limit=count,
        period="6month",  # 6-month window keeps it relevant
    )
    artists = data.get("topartists", {}).get("artist", [])
    log.info("Fetched %d top artists for user '%s'", len(artists), username)
    return artists


def get_similar_artists(artist_name: str, limit: int) -> list[dict]:
    """Return similar artists as [{"name": ..., "mbid": ..., "match": float}]"""
    data = lastfm_get(
        "artist.getSimilar",
        artist=artist_name,
        limit=limit,
        autocorrect=1,
    )
    similar = data.get("similarartists", {}).get("artist", [])
    time.sleep(0.25)  # respect Last.fm rate limits
    return similar


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
    body = {
        **artist_data,
        "qualityProfileId": quality_id,
        "metadataProfileId": metadata_id,
        "rootFolderPath": LIDARR_ROOT_PATH,
        "monitored": True,
        "tags": [tag_id] if tag_id else [],
        "addOptions": {
            "monitor": "latest",               # only grab the newest album
            "searchForMissingAlbums": True,    # trigger download immediately
        },
    }
    return lidarr_post("artist", body)


# ── Core discovery logic ──────────────────────────────────────────────────────
def build_candidate_pool(top_artists: list[dict]) -> dict[str, dict]:
    """
    For each top artist, fetch similar artists and accumulate a score.
    Score = sum of similarity values across all seeds that recommend this artist.
    Returns: {artist_name_lower: {"name": str, "mbid": str, "score": float}}
    """
    candidates: dict[str, dict] = {}

    for seed in top_artists:
        seed_name = seed.get("name", "")
        if not seed_name:
            continue
        log.info("Finding artists similar to: %s", seed_name)
        similar = get_similar_artists(seed_name, SIMILAR_PER_ARTIST)

        for artist in similar:
            name = artist.get("name", "")
            mbid = artist.get("mbid", "")
            try:
                score = float(artist.get("match", 0))
            except (ValueError, TypeError):
                score = 0.0

            if score < MIN_SIMILARITY or not name:
                continue

            key = name.lower()
            if key in candidates:
                candidates[key]["score"] += score  # compound score
            else:
                candidates[key] = {"name": name, "mbid": mbid, "score": score}

    return candidates


def run_discovery():
    log.info("=" * 60)
    log.info("Starting discovery run")
    conn = init_db()

    # 1. Get user's top artists from Last.fm
    top_artists = get_top_artists(LASTFM_USERNAME, TOP_ARTISTS_COUNT)
    if not top_artists:
        log.warning("No top artists returned from Last.fm. Is your history empty?")
        conn.execute(
            "INSERT INTO runs (run_at, artists_added, status) VALUES (?,?,?)",
            (datetime.utcnow().isoformat(), 0, "no_top_artists"),
        )
        conn.commit()
        return

    top_artist_names = {a["name"].lower() for a in top_artists}

    # 2. Build candidate pool from similar artists
    candidates = build_candidate_pool(top_artists)
    log.info("Candidate pool size: %d artists", len(candidates))

    # 3. Remove artists already in Lidarr or already added by this service
    lidarr_names = get_lidarr_artist_names()
    log.info("Artists already in Lidarr: %d", len(lidarr_names))

    filtered = {
        k: v for k, v in candidates.items()
        if k not in lidarr_names
        and k not in top_artist_names  # don't re-add seed artists
        and not was_already_added(conn, v["name"], v["mbid"])
    }
    log.info("Candidates after filtering: %d", len(filtered))

    # 4. Sort by compound score, take top N
    ranked = sorted(filtered.values(), key=lambda x: x["score"], reverse=True)
    to_add = ranked[:MAX_NEW_ARTISTS]

    if not to_add:
        log.info("No new artists to add this run.")
        conn.execute(
            "INSERT INTO runs (run_at, artists_added, status) VALUES (?,?,?)",
            (datetime.utcnow().isoformat(), 0, "no_candidates"),
        )
        conn.commit()
        return

    # 5. Fetch Lidarr config once
    quality_id, metadata_id = get_lidarr_profiles()
    tag_id = get_or_create_discovered_tag()

    # 6. Add each artist to Lidarr
    added_count = 0
    for candidate in to_add:
        name = candidate["name"]
        score = candidate["score"]
        log.info("Adding artist: %s (score=%.3f)", name, score)

        # Look up via Lidarr's MusicBrainz search
        artist_data = lookup_artist_in_lidarr(name)
        if not artist_data:
            log.warning("Could not find '%s' in MusicBrainz via Lidarr, skipping.", name)
            continue

        result = add_artist_to_lidarr(artist_data, quality_id, metadata_id, tag_id)
        if result and result.get("id"):
            lidarr_id = result["id"]
            record_added(conn, name, candidate["mbid"], lidarr_id)
            log.info("✓ Added '%s' to Lidarr (id=%d)", name, lidarr_id)
            added_count += 1
        else:
            log.warning("Failed to add '%s' to Lidarr.", name)

        time.sleep(1)  # be gentle on Lidarr

    conn.execute(
        "INSERT INTO runs (run_at, artists_added, status) VALUES (?,?,?)",
        (datetime.utcnow().isoformat(), added_count, "ok"),
    )
    conn.commit()
    conn.close()

    log.info("Discovery run complete. Added %d new artists.", added_count)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Music Discovery Bridge starting up")
    log.info("  Last.fm user  : %s", LASTFM_USERNAME)
    log.info("  Lidarr URL    : %s", LIDARR_URL)
    log.info("  Max new/run   : %d", MAX_NEW_ARTISTS)
    log.info("  Run interval  : %ds", RUN_INTERVAL)

    while True:
        try:
            run_discovery()
        except Exception as e:
            log.exception("Unhandled error during discovery run: %s", e)
        log.info("Next run in %d seconds.", RUN_INTERVAL)
        time.sleep(RUN_INTERVAL)
