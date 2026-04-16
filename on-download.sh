#!/usr/bin/env bash
# =============================================================================
# on-download.sh — Lidarr Custom Script
#
# Tells Navidrome to rescan its library immediately after Lidarr imports
# a new album, so music appears in Symfonium without waiting for the hourly scan.
#
# Setup in Lidarr:
#   Settings → Connect → + → Custom Script
#   Name: Navidrome Rescan
#   Path: /config/scripts/on-download.sh
#   Triggers: ✓ On Import  ✓ On Upgrade
# =============================================================================

# Navidrome connection (uses Docker internal network)
NAVIDROME_URL="${NAVIDROME_URL:-http://navidrome:4533}"
NAVIDROME_USER="${NAVIDROME_USER:-admin}"
NAVIDROME_PASS="${NAVIDROME_PASS:-}"   # set in Lidarr's env or hardcode here

# Debounce overlapping rescans when Lidarr fires many imports at once (flock + short cooldown)
NAVIDROME_SCAN_LOCK="${NAVIDROME_SCAN_LOCK:-/tmp/navidrome_scan.lock}"
NAVIDROME_SCAN_DEBOUNCE_SLEEP="${NAVIDROME_SCAN_DEBOUNCE_SLEEP:-3}"

# Lidarr passes event type via environment
EVENT_TYPE="${lidarr_eventtype:-}"

# Only act on Import and Upgrade events
if [[ "$EVENT_TYPE" != "Download" && "$EVENT_TYPE" != "Test" ]]; then
    exit 0
fi

if [[ "$EVENT_TYPE" == "Test" ]]; then
    echo "on-download.sh test successful"
    exit 0
fi

ARTIST="${lidarr_artist_name:-unknown artist}"
ALBUM="${lidarr_album_title:-unknown album}"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Triggering Navidrome rescan after import: $ARTIST — $ALBUM"

if [[ -z "${NAVIDROME_PASS}" ]]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Warning: NAVIDROME_PASS is empty; refusing to call Navidrome without credentials." >&2
    exit 1
fi

exec {navidrome_scan_lock_fd}>"${NAVIDROME_SCAN_LOCK}"
if ! flock -n "$navidrome_scan_lock_fd"; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Debounced: another Navidrome rescan is in progress or cooling down; skipping this hook."
    exit 0
fi

# Navidrome exposes a scan endpoint via its Subsonic API
SCAN_URL="$NAVIDROME_URL/rest/startScan.view"

response=$(curl -sf --max-time 10 \
    "$SCAN_URL" \
    -d "u=$NAVIDROME_USER" \
    -d "p=$NAVIDROME_PASS" \
    -d "v=1.16.1" \
    -d "c=lidarr-hook" \
    -d "f=json" \
    2>&1)

if echo "$response" | grep -q '"status":"ok"'; then
    echo "Navidrome scan triggered successfully."
else
    # Non-fatal — the hourly scan will catch it anyway
    echo "Warning: could not trigger Navidrome scan. Response: $response"
fi

# Hold the lock briefly so burst imports coalesce into one scan trigger
sleep "${NAVIDROME_SCAN_DEBOUNCE_SLEEP}"

exit 0
