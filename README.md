# Music Media Server

Self-hosted music stack with automatic discovery. Scrobbles your listening
habits to ListenBrainz, uses them to find new artists, and automatically downloads
their music — so opening Symfonium on your phone always has something new.

Downloads use **[Soularr](https://github.com/mrusse/soularr)** + **[slskd](https://github.com/slskd/slskd)** (Soulseek): Soularr polls Lidarr for wanted releases, searches Soulseek, and tells Lidarr to import completed downloads.

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
 │       finds new artists             ↑               │          │
 │                               soularr (polls)       │          │
 │                                     ↓               │          │
 │                               slskd :5030           │          │
 │                                     ↓               │          │
 │                               Soulseek P2P          │          │
 │                                     ↓               │          │
 │                         /data/slskd (staging)       │          │
 │                                     ↓               │          │
 │                    Lidarr imports → /data/music ────┘          │
 └─────────────────────────────────────────────────────────────────┘
```

| Container | Purpose | Port |
|---|---|---|
| `slskd` | Soulseek daemon + web API | 5030 (5031, 50300 for P2P) |
| `soularr` | Lidarr ↔ Soulseek bridge ([Soularr](https://github.com/mrusse/soularr)) | — |
| `lidarr` | Music collection manager | 8686 |
| `navidrome` | Streaming server (Subsonic/OpenSubsonic API) | 4533 |
| `music-discovery` | ListenBrainz → Lidarr discovery bridge + optional web UI | `${DISCOVERY_HOST_GUI_PORT:-8765}` (default 8765) |

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
- `SOULSEEK_USERNAME` / `SOULSEEK_PASSWORD` — pick any username/password; a Soulseek account is created automatically on first connect (no prior registration needed)
- `LISTENBRAINZ_USERNAME` — your ListenBrainz username ([listenbrainz.org](https://listenbrainz.org))
- `LISTENBRAINZ_TOKEN` — optional for the discovery bridge (higher API limits); set it for Navidrome scrobbling ([settings](https://listenbrainz.org/settings/))
- `NAVIDROME_ADMIN_PASS` — pick a strong password
- `LIDARR_API_KEY` — you'll add this in Step 6 after Lidarr starts

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
docker compose up -d slskd lidarr navidrome soularr
```

### Step 5 — Configure slskd and Soularr

1. Open **slskd** at `http://YOUR-LAN-IP:5030` (default login: `slskd` / `slskd`).
   If slskd is stuck restarting, check `docker logs slskd` — a "not writeable" error means the host directory permissions are wrong. Fix with: `sudo chown -R 1000:1000 /opt/music/slskd /data/slskd`
2. Generate a random **API key** for slskd and add it to `.env`:
   ```bash
   # Generate a random key
   openssl rand -hex 24
   # Paste the output into .env as SLSKD_API_KEY, then restart slskd
   nano /opt/music-server/.env
   docker compose up -d slskd
   ```
3. Copy the Soularr config template and edit it on the host:
   ```bash
   sudo cp /opt/music-server/soularr/config.ini.example /opt/music/soularr/config.ini
   sudo nano /opt/music/soularr/config.ini
   ```
4. Set `[Slskd] api_key` to the same key you put in `.env`. Set `[Lidarr] api_key` to your Lidarr API key (from Step 6 once Lidarr is up).
5. Restart Soularr so it picks up the config:
   ```bash
   docker restart soularr
   ```

Soularr is **not** a Lidarr “download client” — it talks to Lidarr and slskd over HTTP. You may see a Lidarr health warning about no download clients; that is **expected** and safe to ignore for this stack.

### Step 6 — Configure Lidarr

1. Open `http://YOUR-LAN-IP:8686`
2. **Do not add** qBittorrent / rdt-client — Soularr handles acquisition.
3. **Add root folder:**
   Settings → Media Management → Root Folders → `/music`
4. **Copy your API key:**
   Settings → General → Security → API Key → paste into `.env` as `LIDARR_API_KEY`, and into `/opt/music/soularr/config.ini` under `[Lidarr] api_key`, then `docker restart soularr`.
5. **Make the hook script executable on the host** (required before Lidarr can run it):
   ```bash
   chmod +x /opt/music-server/on-download.sh
   ```
6. **Register the on-download hook in Lidarr:**
   Settings → Connect → `+` → Custom Script
   - Name: `Navidrome Rescan`
   - Path: `/config/scripts/on-download.sh`
   - Triggers: ✓ On Release Import, ✓ On Upgrade
   - Test → Save
6. **Add your first artists** to seed your library (20–30 is a good start)

### Step 7 — Restart to apply the API key, then start discovery

Ensure `LIDARR_API_KEY` is filled in your `.env`, then bring up the full stack (this starts `music-discovery` for the first time):

```bash
cd /opt/music-server
docker compose up -d
```

Check that all containers are running:

```bash
docker ps
```

The discovery container runs on a 24-hour timer. To trigger an immediate run rather than waiting:

```bash
docker restart music-discovery
docker logs -f music-discovery   # watch it run live
```

> **Note:** ListenBrainz needs listening history to make recommendations. If you haven't scrobbled much yet, the first run may add nothing (`204` from the stats API is normal). Seed it faster by importing Last.fm history at [listenbrainz.org/import](https://listenbrainz.org/import/), or just add 20–30 artists to Lidarr manually first.

### Step 8 — Configure Symfonium (Android)

1. Install [Symfonium](https://play.google.com/store/apps/details?id=app.symfonium) (~$5)
2. Add media provider → OpenSubsonic
   - Server URL: `http://YOUR-TAILSCALE-IP:4533`
   - Username: `admin`
   - Password: your `NAVIDROME_ADMIN_PASS`
3. Settings → Cache → enable offline sync for your favourite artists

Scrobbling is handled by **Navidrome**, not Symfonium. To enable it:
1. Open the Navidrome web UI (`http://YOUR-TAILSCALE-IP:4533`)
2. Click your username (top right) → Personal Settings
3. Under **ListenBrainz**, paste your user token from [listenbrainz.org/settings](https://listenbrainz.org/settings/)
4. Save — Navidrome will now scrobble every play Symfonium reports to it

---

## Discovery Bridge

The `music-discovery` container runs on a timer and works like this:

```
Seed artists from ListenBrainz:
  • most listened (user stats), and/or
  • loved recordings (feedback → MusicBrainz recording metadata → artist MBIDs)
        ↓
Labs similar-artists API for each seed that has an artist MBID
        ↓
Score candidates: sum normalized similarity across all seeds
        ↓
Filter: remove artists already in Lidarr or previously added
        ↓
Top 5 candidates → added to Lidarr, tagged "discovered"
        ↓
Soularr + slskd fetch wanted releases from Soulseek → /data/slskd
        ↓
Lidarr imports → /data/music
        ↓
on-download.sh fires → Navidrome rescans immediately
        ↓
New music appears in Symfonium
```

### Web UI

After `docker compose up -d music-discovery`, open `http://YOUR-LAN-IP:${DISCOVERY_HOST_GUI_PORT:-8765}` (or whatever you set in `.env` as `DISCOVERY_HOST_GUI_PORT`). The UI shows recent runs, artists the bridge added, live log tail, and lets you edit tunables (stored in `/opt/music/discovery/settings.json` inside the container volume). Secrets stay in environment variables.

If you set `DISCOVERY_GUI_TOKEN` in `.env`, append `?token=YOUR_TOKEN` to the URL (or send header `X-Discovery-Token`) so the UI and API are not open anonymously on your LAN.

Set `DISCOVERY_GUI_PORT=0` in `docker-compose.yml` if you want headless-only behavior (no HTTP server).

### Tuning knobs (in `docker-compose.yml` or the web UI)

| Variable | Default | Effect |
|---|---|---|
| `DISCOVERY_SEED_MODE` | `loved` | `most_listened` (stats), `loved` (ListenBrainz loved tracks → artists), or `both` |
| `TOP_ARTISTS_COUNT` | 20 | More seeds = broader recommendations |
| `LOVED_FEEDBACK_COUNT` | 200 | How many loved recordings to page through when seed mode includes `loved` |
| `SIMILAR_PER_ARTIST` | 10 | Candidates per seed artist |
| `MAX_NEW_ARTISTS` | 5 | Max additions per nightly run |
| `MIN_SIMILARITY` | 0.25 | Raise for stricter matching (0–1) |
| `RUN_INTERVAL_SECONDS` | 86400 | How often to run (86400 = daily) |

Optional environment variables for `music-discovery` (set in `docker-compose.yml` or override):

| Variable | Default | Effect |
|---|---|---|
| `LISTENBRAINZ_STATS_RANGE` | `half_yearly` | ListenBrainz stats window (`week`, `month`, `year`, `all_time`, …) |
| `LISTENBRAINZ_SIMILAR_ALGORITHM` | (session-based preset) | Labs [similar-artists](https://labs.api.listenbrainz.org/similar-artists) algorithm name |

Soularr’s poll interval is `SCRIPT_INTERVAL` (seconds) in `docker-compose.yml` for the `soularr` service (default `300`).

### Useful commands

```bash
# Watch discovery run live
docker logs -f music-discovery

# Watch Soularr (wanted → Soulseek → import)
docker logs -f soularr

# Force an immediate discovery run (or use "Run discovery now" in the web UI)
docker restart music-discovery

# See everything the bridge has ever added
sqlite3 /opt/music/discovery/discovery.db \
  "SELECT name, substr(added_at,1,10) as date FROM added_artists ORDER BY added_at DESC;"

# Run health check
bash /opt/music-server/health-check.sh
```

### Cold-start tip

ListenBrainz recommendations need **listening history** and **computed user statistics**. After you start scrobbling, stats may take up to about a day to appear; until then the stats API can return empty (`204`). You can import past listens (e.g. from Last.fm) via [ListenBrainz import tools](https://listenbrainz.org/import/) to seed your profile faster.

Seed artists **without** a MusicBrainz ID are skipped for similarity lookup (the Labs API requires an artist MBID). Loved-track mode resolves MBIDs via ListenBrainz metadata when possible.

**Note:** "Loved" here means **ListenBrainz recording feedback** (the heart on ListenBrainz clients), not a separate MusicBrainz.org account API.

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
  soularr/
    config.ini.example      ← copy to /opt/music/soularr/config.ini
  discovery.py
  Dockerfile
  requirements.txt

/opt/music/                 ← persistent container configs
  slskd/
  soularr/                  ← config.ini (not in git; copy from example)
  lidarr/
  navidrome/
  discovery/                ← discovery.db + discovery.log

/data/
  music/                    ← your music library
  slskd/                    ← Soulseek download staging (Lidarr + slskd + soularr)
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

**Disk filling up?** Check `/data/slskd` first — failed or partial Soulseek
downloads can linger. Also consider lowering Soularr `number_of_albums_to_grab`
in `config.ini` or `MAX_NEW_ARTISTS` for discovery.

**Soulseek** is a peer-to-peer network; only acquire material you have the
rights to use, and ensure your host/network policy allows it.
