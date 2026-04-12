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

exit 0
