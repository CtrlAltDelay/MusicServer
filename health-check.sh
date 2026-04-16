#!/usr/bin/env bash
# =============================================================================
# health-check.sh — Quick status check for all music server services
#
# Usage:
#   bash health-check.sh
#
# Shows:
#   - Docker container status
#   - HTTP reachability of each service
#   - Lidarr queue and disk space
#   - Stale Soulseek incomplete downloads (> 48h)
#   - Discovery bridge last run info
#   - Tailscale connectivity
# =============================================================================

set -uo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
fail() { echo -e "  ${RED}✗${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }
hdr()  { echo -e "\n${BOLD}${CYAN}$*${NC}"; }

# ── Load .env for API keys ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    set -a; source "$SCRIPT_DIR/.env"; set +a
fi

LIDARR_API_KEY="${LIDARR_API_KEY:-}"
LIDARR_URL="http://localhost:8686"
NAVIDROME_URL="http://localhost:4533"
SLSKD_URL="http://localhost:5030"
DISCOVERY_DB="/opt/music/discovery/discovery.db"

# ── Helper: HTTP check ────────────────────────────────────────────────────────
http_ok() {
    local url="$1"
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null || echo "000")
    [[ "$code" =~ ^[23] ]]
}

# slskd often returns 401 without auth — treat as reachable
http_ok_slskd() {
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$SLSKD_URL" 2>/dev/null || echo "000")
    [[ "$code" =~ ^(2|3|401)$ ]]
}

# ── 1. Docker containers ──────────────────────────────────────────────────────
hdr "Docker Containers"
containers=("slskd" "soularr" "lidarr" "navidrome" "music-discovery")
all_running=true
for name in "${containers[@]}"; do
    status=$(docker inspect --format '{{.State.Status}}' "$name" 2>/dev/null || echo "not found")
    uptime=$(docker inspect --format '{{.State.StartedAt}}' "$name" 2>/dev/null || echo "")
    if [[ "$status" == "running" ]]; then
        ok "$name — running"
    else
        fail "$name — $status"
        all_running=false
    fi
done

# ── 2. HTTP reachability ──────────────────────────────────────────────────────
hdr "Service Reachability"

if http_ok_slskd; then
    ok "slskd → $SLSKD_URL"
else
    fail "slskd → $SLSKD_URL (not responding)"
fi

declare -A services=(
    ["Lidarr"]="$LIDARR_URL"
    ["Navidrome"]="$NAVIDROME_URL"
)
for name in "${!services[@]}"; do
    url="${services[$name]}"
    if http_ok "$url"; then
        ok "$name → $url"
    else
        fail "$name → $url (not responding)"
    fi
done

# ── 3. Lidarr status ──────────────────────────────────────────────────────────
hdr "Lidarr"
if [[ -n "$LIDARR_API_KEY" ]]; then
    # Artist count
    artists=$(curl -sf --max-time 5 \
        -H "X-Api-Key: $LIDARR_API_KEY" \
        "$LIDARR_URL/api/v1/artist" 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d))" 2>/dev/null || echo "?")
    ok "Artists monitored: $artists"

    # Queue (active downloads)
    queue=$(curl -sf --max-time 5 \
        -H "X-Api-Key: $LIDARR_API_KEY" \
        "$LIDARR_URL/api/v1/queue" 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('totalRecords',0))" 2>/dev/null || echo "?")
    ok "Downloads in queue: $queue"

    # Wanted / missing
    wanted=$(curl -sf --max-time 5 \
        -H "X-Api-Key: $LIDARR_API_KEY" \
        "$LIDARR_URL/api/v1/wanted/missing?pageSize=1" 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('totalRecords',0))" 2>/dev/null || echo "?")
    warn "Missing albums: $wanted"

    # Health checks
    health=$(curl -sf --max-time 5 \
        -H "X-Api-Key: $LIDARR_API_KEY" \
        "$LIDARR_URL/api/v1/health" 2>/dev/null \
        | python3 -c "
import sys, json
issues = json.load(sys.stdin)
if not issues:
    print('No issues')
else:
    for i in issues:
        print(f\"[{i['type']}] {i['message']}\")
" 2>/dev/null || echo "could not fetch")
    if [[ "$health" == "No issues" ]]; then
        ok "Health: $health"
    else
        while IFS= read -r line; do warn "Health: $line"; done <<< "$health"
    fi
else
    warn "LIDARR_API_KEY not set in .env — skipping Lidarr API checks"
fi

# ── 4. Disk space ─────────────────────────────────────────────────────────────
hdr "Disk Space"
for path in /data/music /data/slskd; do
    if [[ -d "$path" ]]; then
        read -r used avail pct <<< "$(df -h "$path" | tail -1 | awk '{print $3, $4, $5}')"
        if [[ "${pct%%%}" -gt 90 ]]; then
            fail "$path — used: $used, free: $avail ($pct used)"
        elif [[ "${pct%%%}" -gt 75 ]]; then
            warn "$path — used: $used, free: $avail ($pct used)"
        else
            ok "$path — used: $used, free: $avail ($pct used)"
        fi
    else
        warn "$path — directory not found"
    fi
done

# ── 5. Stale Downloads (slskd incomplete staging) ───────────────────────────
hdr "Stale Downloads"
SLSKD_INCOMPLETE="/data/slskd/.incomplete"
STALE_MINUTES=$((48 * 60))
if [[ ! -d "$SLSKD_INCOMPLETE" ]]; then
    warn "$SLSKD_INCOMPLETE — directory not found (skipping)"
else
    mapfile -t _stale_paths < <(
        find "$SLSKD_INCOMPLETE" -mindepth 1 \( -type f -o -type d \) -mmin +"$STALE_MINUTES" 2>/dev/null
    )
    stale_n="${#_stale_paths[@]}"
    if [[ "$stale_n" -eq 0 ]]; then
        ok "No entries under $SLSKD_INCOMPLETE older than 48 hours"
    else
        warn "$stale_n path(s) under $SLSKD_INCOMPLETE older than 48 hours (mtime)"
        for p in "${_stale_paths[@]:0:15}"; do
            echo -e "  ${YELLOW}!${NC} $p"
        done
        if [[ "$stale_n" -gt 15 ]]; then
            echo -e "  ${YELLOW}!${NC} … and $((stale_n - 15)) more"
        fi
    fi
    echo ""
    echo "  To preview the same set:"
    echo "    find \"$SLSKD_INCOMPLETE\" -mindepth 1 \\( -type f -o -type d \\) -mmin +$STALE_MINUTES -print"
    echo ""
    echo "  After reviewing, you can prune with find -delete (see commented examples at end of health-check.sh)."
fi

# Stale slskd incomplete cleanup (uncomment after verifying paths reported above):
# find "/data/slskd/.incomplete" -mindepth 1 -depth \( -type f -o -type d \) -mmin $((48 * 60)) -delete

# ── 6. Discovery bridge ───────────────────────────────────────────────────────
hdr "Discovery Bridge"
if [[ -f "$DISCOVERY_DB" ]]; then
    last_run=$(sqlite3 "$DISCOVERY_DB" \
        "SELECT run_at, artists_added, status FROM runs ORDER BY id DESC LIMIT 1;" \
        2>/dev/null || echo "")
    total_added=$(sqlite3 "$DISCOVERY_DB" \
        "SELECT COUNT(*) FROM added_artists;" 2>/dev/null || echo "?")

    if [[ -n "$last_run" ]]; then
        IFS='|' read -r run_at added status <<< "$last_run"
        ok "Last run: $run_at (status=$status, added=$added)"
        ok "Total artists ever added by bridge: $total_added"
    else
        warn "No runs recorded yet."
    fi

    echo ""
    echo "  Recently discovered artists:"
    sqlite3 "$DISCOVERY_DB" \
        "SELECT '    ' || name || ' (added ' || substr(added_at,1,10) || ')'
         FROM added_artists ORDER BY added_at DESC LIMIT 5;" 2>/dev/null \
        || echo "    (none yet)"
else
    warn "Discovery DB not found at $DISCOVERY_DB"
    warn "Has the bridge run at least once? Check: docker logs music-discovery"
fi

# ── 7. Tailscale ─────────────────────────────────────────────────────────────
hdr "Tailscale"
if command -v tailscale &>/dev/null; then
    ts_status=$(tailscale status --json 2>/dev/null \
        | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    self = d.get('Self', {})
    ip = self.get('TailscaleIPs', ['?'])[0]
    online = self.get('Online', False)
    hostname = self.get('HostName', '?')
    print(f'{hostname} — {ip} — online={online}')
except:
    print('could not parse')
" 2>/dev/null || echo "not running")
    if echo "$ts_status" | grep -q "online=True"; then
        ok "$ts_status"
        ts_ip=$(tailscale ip -4 2>/dev/null || echo "unknown")
        echo ""
        echo "  Symfonium connection URL:"
        echo -e "  ${BOLD}http://$ts_ip:4533${NC}"
    else
        fail "Tailscale not connected. Run: tailscale up"
    fi
else
    warn "Tailscale not installed. Run setup.sh first."
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}─────────────────────────────────────────${NC}"
if $all_running; then
    echo -e "${GREEN}${BOLD} All containers running.${NC}"
else
    echo -e "${RED}${BOLD} Some containers are not running.${NC}"
    echo "  Run: docker compose up -d"
fi
echo ""
