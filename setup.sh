#!/usr/bin/env bash
# =============================================================================
# setup.sh — Bootstrap script for music server on Debian/Ubuntu LXC
#
# Run as root on a fresh Proxmox LXC container:
#   bash setup.sh
#
# What this does:
#   1. Updates the system
#   2. Installs Docker + Docker Compose
#   3. Creates a non-root service user
#   4. Creates all required host directories with correct permissions
#   5. Installs and configures Tailscale
#   6. Clones/copies the project files into place
#   7. Prints next steps
# =============================================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Must run as root ──────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || error "Run this script as root: sudo bash setup.sh"

# ── Config ────────────────────────────────────────────────────────────────────
SERVICE_USER="mediaserver"
SERVICE_UID=1000
SERVICE_GID=1000
PROJECT_DIR="/opt/music-server"

# ── 1. System update ──────────────────────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    curl wget git sqlite3 ca-certificates \
    gnupg lsb-release apt-transport-https

# ── 2. Install Docker ─────────────────────────────────────────────────────────
if command -v docker &>/dev/null; then
    info "Docker already installed: $(docker --version)"
else
    info "Installing Docker..."
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
        $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
    info "Docker installed: $(docker --version)"
fi

# ── 3. Create service user ────────────────────────────────────────────────────
if id "$SERVICE_USER" &>/dev/null; then
    info "User '$SERVICE_USER' already exists."
else
    info "Creating service user '$SERVICE_USER' (uid=$SERVICE_UID)..."
    groupadd -g "$SERVICE_GID" "$SERVICE_USER" 2>/dev/null || true
    useradd -u "$SERVICE_UID" -g "$SERVICE_GID" -m -s /bin/bash "$SERVICE_USER"
    usermod -aG docker "$SERVICE_USER"
    info "User created. Add a password if you want SSH access: passwd $SERVICE_USER"
fi

# ── 4. Create directory structure ─────────────────────────────────────────────
info "Creating directory structure..."

dirs=(
    /opt/music/slskd
    /opt/music/soularr
    /opt/music/lidarr
    /opt/music/navidrome
    /opt/music/discovery
    /data/music
    /data/slskd
    "$PROJECT_DIR"
)

for d in "${dirs[@]}"; do
    mkdir -p "$d"
done

chown -R "$SERVICE_UID:$SERVICE_GID" /opt/music /data "$PROJECT_DIR"
chmod -R 755 /opt/music /data

info "Directories created:"
for d in "${dirs[@]}"; do
    echo "    $d"
done

# ── 5. Install Tailscale ──────────────────────────────────────────────────────
if command -v tailscale &>/dev/null; then
    info "Tailscale already installed: $(tailscale version | head -1)"
else
    info "Installing Tailscale..."
    curl -fsSL https://tailscale.com/install.sh | sh
    systemctl enable --now tailscaled
    info "Tailscale installed."
fi

# ── 6. Copy project files ─────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info "Copying project files to $PROJECT_DIR..."
cp -r "$SCRIPT_DIR"/. "$PROJECT_DIR"/
chmod +x "$PROJECT_DIR/on-download.sh"
chown -R "$SERVICE_UID:$SERVICE_GID" "$PROJECT_DIR"

# ── 7. Set up .env if not present ─────────────────────────────────────────────
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    warn ".env file created from template. You MUST edit it before starting:"
    warn "    nano $PROJECT_DIR/.env"
else
    info ".env already exists, skipping."
fi

# ── 8. Docker log rotation (prevents disk fill) ───────────────────────────────
info "Configuring Docker log rotation..."
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "20m",
    "max-file": "3"
  }
}
EOF
systemctl restart docker

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN} Setup complete!${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Edit your environment file:"
echo "     nano $PROJECT_DIR/.env"
echo ""
echo "  2. Connect to Tailscale (do this once, opens a browser link):"
echo "     tailscale up"
echo ""
echo "  3. Start all services:"
echo "     cd $PROJECT_DIR && docker compose up -d"
echo ""
echo "  4. Check status:"
echo "     cd $PROJECT_DIR && bash health-check.sh"
echo ""
echo "  Service URLs (replace with your Tailscale IP for remote access):"
echo "     slskd       → http://$(hostname -I | awk '{print $1}'):5030"
echo "     Lidarr      → http://$(hostname -I | awk '{print $1}'):8686"
echo "     Navidrome   → http://$(hostname -I | awk '{print $1}'):4533"
echo ""
