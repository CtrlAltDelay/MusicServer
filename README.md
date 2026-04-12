# Music Media Server

Self-hosted music stack with automatic discovery. Scrobbles your listening
habits to ListenBrainz, uses them to find new artists, and automatically downloads
their music — so opening Symfonium on your phone always has something new.

---

## Architecture

```
 ┌─────────────────────────────────────────────────────────────────┐
 │  Android (Symfonium)                                            │
 │    ↕ streams via Tailscale                                      │
 └──────────────────────┬──────────────────────────────────────────┘
                        │
 ┌──────────────────────▼──────────────────────────────────────────┐
 │  Proxmox LXC (Debian/Ubuntu)                                    │
 │                                                                 │
 │  Navidrome :4533  ←── /data/music (read-only)                  │
 │       ↕ scrobbles                                               │
 │  ListenBrainz  ←────────────────────────────────────┐          │
 │       ↑                                              │          │
 │  music-discovery (nightly)  ───────► Lidarr :8686   │          │
 │       finds new artists             ↓               │          │
 │                               rdt-client :6500      │          │
 │                               ↓                     │          │
 │                         Real-Debrid                 │          │
 │                               ↓                     │          │
 │                         /data/downloads             │          │
 │                               ↓                     │          │
 │                    Lidarr imports → /data/music ────┘          │
 └─────────────────────────────────────────────────────────────────┘
```

| Container | Purpose | Port |
|---|---|---|
| `rdtclient` | Real-Debrid proxy (fake qBittorrent API) | 6500 |
| `lidarr` | Music collection manager | 8686 |
| `navidrome` | Streaming server (Subsonic/OpenSubsonic API) | 4533 |
| `music-discovery` | ListenBrainz → Lidarr discovery bridge | — |

---

## Quick Start

### Step 1 — Run the setup script (as root on your LXC)

```bash
git clone <your-repo> /opt/music-server
cd /opt/music-server
bash setup.sh
```

This installs Docker, creates the `mediaserver` user, creates all host
directories, installs Tailscale, and scaffolds your `.env`.

### Step 2 — Fill in your credentials

```bash
nano /opt/music-server/.env
```

You need to fill in:
- `LISTENBRAINZ_USERNAME` — your ListenBrainz username ([listenbrainz.org](https://listenbrainz.org))
- `LISTENBRAINZ_TOKEN` — optional for the discovery bridge (higher API limits); set it for Navidrome scrobbling ([settings](https://listenbrainz.org/settings/))
- `NAVIDROME_ADMIN_PASS` — pick a strong password
- `LIDARR_API_KEY` — you'll get this in Step 4 after Lidarr starts

### Step 3 — Connect Tailscale

```bash
tailscale up
# Follow the printed URL in a browser to authorize the device
tailscale ip -4   # note this IP for Symfonium later
```

Install Tailscale on your Android phone and sign in to the same account.

### Step 4 — Start the stack (without discovery first)

```bash
cd /opt/music-server
docker compose up -d rdtclient lidarr navidrome
```

### Step 5 — Configure rdt-client

1. Open `http://YOUR-LAN-IP:6500`
2. Create login credentials on first visit
3. Settings → Real-Debrid API Key → paste your key from https://real-debrid.com/apitoken
4. Download path: `/data/downloads`
5. Mapped path: `/data/downloads`
6. Save

### Step 6 — Configure Lidarr

1. Open `http://YOUR-LAN-IP:8686`
2. **Add download client:**
   Settings → Download Clients → `+` → qBittorrent
   - Host: `rdtclient`
   - Port: `6500`
   - Username / Password: your rdt-client credentials
   - Category: `lidarr`
   - Test → Save
3. **Add root folder:**
   Settings → Media Management → Root Folders → `/music`
4. **Copy your API key:**
   Settings → General → Security → API Key → paste into `.env` as `LIDARR_API_KEY`
5. **Register the on-download hook:**
   Settings → Connect → `+` → Custom Script
   - Name: `Navidrome Rescan`
   - Path: `/config/scripts/on-download.sh`
   - Triggers: ✓ On Import, ✓ On Upgrade
   - Test → Save
6. **Add your first artists** to seed your library (20–30 is a good start)

### Step 7 — Restart to apply the API key, then start discovery

```bash
docker compose up -d
```

### Step 8 — Configure Symfonium (Android)

1. Install [Symfonium](https://play.google.com/store/apps/details?id=app.symfonium) (~$5)
2. Add media provider → OpenSubsonic
   - Server URL: `http://YOUR-TAILSCALE-IP:4533`
   - Username: `admin`
   - Password: your `NAVIDROME_ADMIN_PASS`
3. Settings → Scrobbling → ListenBrainz → connect with your ListenBrainz user token (same idea as in Navidrome; token from [ListenBrainz settings](https://listenbrainz.org/settings/))
4. Settings → Cache → enable offline sync for your favourite artists

---

## Discovery Bridge

The `music-discovery` container runs nightly and works like this:

```
GET /1/stats/user/{user}/artists (ListenBrainz, half-yearly stats)
        ↓
Labs similar-artists API for each seed that has an artist MBID
        ↓
Score candidates: sum normalized similarity across all seeds
        ↓
Filter: remove artists already in Lidarr or previously added
        ↓
Top 5 candidates → added to Lidarr, tagged "discovered"
        ↓
Lidarr grabs their latest album via rdt-client → /data/music
        ↓
on-download.sh fires → Navidrome rescans immediately
        ↓
New music appears in Symfonium
```

### Tuning knobs (in `docker-compose.yml`)

| Variable | Default | Effect |
|---|---|---|
| `TOP_ARTISTS_COUNT` | 20 | More seeds = broader recommendations |
| `SIMILAR_PER_ARTIST` | 10 | Candidates per seed artist |
| `MAX_NEW_ARTISTS` | 5 | Max additions per nightly run |
| `MIN_SIMILARITY` | 0.25 | Raise for stricter matching (0–1) |
| `RUN_INTERVAL_SECONDS` | 86400 | How often to run (86400 = daily) |

Optional environment variables for `music-discovery` (set in `docker-compose.yml` or override):

| Variable | Default | Effect |
|---|---|---|
| `LISTENBRAINZ_STATS_RANGE` | `half_yearly` | ListenBrainz stats window (`week`, `month`, `year`, `all_time`, …) |
| `LISTENBRAINZ_SIMILAR_ALGORITHM` | (session-based preset) | Labs [similar-artists](https://labs.api.listenbrainz.org/similar-artists) algorithm name |

### Useful commands

```bash
# Watch discovery run live
docker logs -f music-discovery

# Force an immediate run
docker restart music-discovery

# See everything the bridge has ever added
sqlite3 /opt/music/discovery/discovery.db \
  "SELECT name, substr(added_at,1,10) as date FROM added_artists ORDER BY added_at DESC;"

# Run health check
bash /opt/music-server/health-check.sh
```

### Cold-start tip

ListenBrainz recommendations need **listening history** and **computed user statistics**. After you start scrobbling, stats may take up to about a day to appear; until then the stats API can return empty (`204`). You can import past listens (e.g. from Last.fm) via [ListenBrainz import tools](https://listenbrainz.org/import/) to seed your profile faster.

Seed artists **without** a MusicBrainz ID in your top-artists stats are skipped for similarity lookup (the Labs API requires an artist MBID).

---

## File Layout

```
/opt/music-server/          ← project files (this repo)
  docker-compose.yml
  .env                      ← your secrets (not in git)
  .env.example
  setup.sh
  health-check.sh
  on-download.sh
  music-discovery-bridge/
    discovery.py
    Dockerfile
    requirements.txt

/opt/music/                 ← persistent container configs
  rdtclient/
  lidarr/
  navidrome/
  discovery/                ← discovery.db + discovery.log

/data/
  music/                    ← your music library
  downloads/                ← staging area (auto-cleared by Lidarr)
```

---

## Useful Tips

**The `discovered` tag in Lidarr** marks every artist added by the bridge.
Filter by it under Artists to see what the system has auto-added vs. what you
added yourself.

**Monitoring mode is `latest`** by default — only the newest album is
downloaded per artist. To get full discographies, open Lidarr, find the artist,
and change their monitoring from "Latest Album" to "All Albums".

**Want stricter recommendations?** Raise `MIN_SIMILARITY` to `0.5` or higher.
This filters out artists that are only loosely related to your taste.

**Disk filling up?** Check `/data/downloads` first — Lidarr should clear it
after import, but failed downloads can linger. Also consider setting
`MAX_NEW_ARTISTS` lower.
