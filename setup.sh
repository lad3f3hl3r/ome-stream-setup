#!/usr/bin/env bash
set -euo pipefail

# ── OvenMediaEngine live stream server setup ─────────────────────────────────
# Tested on Ubuntu 24.04. Run as root or with sudo.

INSTALL_DIR="/opt/ome"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

echo ""
echo "  OvenMediaEngine Live Stream Server Setup"
echo "  ─────────────────────────────────────────"
echo ""

# ── Load .env or prompt ───────────────────────────────────────────────────────
if [[ -f .env ]]; then
  info "Loading .env"
  set -a; source .env; set +a
else
  warn ".env not found — prompting for values"
  read -rp  "  Domain (e.g. stream.example.com): "  DOMAIN
  read -rp  "  Server public IP:                 "  OME_HOST_IP
  read -rp  "  Email (Let's Encrypt):            "  EMAIL
  read -rp  "  Timezone [Europe/Vienna]:         "  TZ
  read -rp  "  OME API token:                    "  OME_API_TOKEN
  read -rp  "  Stream secret (OBS key prefix):   "  STREAM_SECRET
  read -rp  "  Admin page password:              "  ADMIN_PASSWORD
  echo ""
  echo "  SMTP configuration:"
  read -rp  "  SMTP host:                        "  SMTP_HOST
  read -rp  "  SMTP port [587]:                  "  SMTP_PORT
  read -rp  "  SMTP username:                    "  SMTP_USER
  read -rsp "  SMTP password:                    "  SMTP_PASS; echo ""
  read -rp  "  From address:                     "  SMTP_FROM
  read -rp  "  Use STARTTLS? (true/false) [true]:"  SMTP_TLS
  read -rp  "  Use SSL/SMTPS? (true/false) [false]:"SMTP_SSL

  SMTP_PORT="${SMTP_PORT:-587}"
  SMTP_TLS="${SMTP_TLS:-true}"
  SMTP_SSL="${SMTP_SSL:-false}"
  TZ="${TZ:-Europe/Vienna}"

  cat > .env <<EOF
DOMAIN=$DOMAIN
OME_HOST_IP=$OME_HOST_IP
EMAIL=$EMAIL
TZ=$TZ
OME_API_TOKEN=$OME_API_TOKEN
STREAM_SECRET=$STREAM_SECRET
ADMIN_PASSWORD=$ADMIN_PASSWORD
SMTP_HOST=$SMTP_HOST
SMTP_PORT=$SMTP_PORT
SMTP_USER=$SMTP_USER
SMTP_PASS=$SMTP_PASS
SMTP_FROM=$SMTP_FROM
SMTP_TLS=$SMTP_TLS
SMTP_SSL=$SMTP_SSL
MAGIC_LINK_EXPIRE_MINUTES=15
SESSION_EXPIRE_DAYS=7
EOF
  info ".env created"
fi

# ── Validate ──────────────────────────────────────────────────────────────────
[[ -z "${DOMAIN:-}"          ]] && error "DOMAIN is required"
[[ -z "${OME_HOST_IP:-}"     ]] && error "OME_HOST_IP is required"
[[ -z "${EMAIL:-}"           ]] && error "EMAIL is required"
[[ -z "${OME_API_TOKEN:-}"   ]] && error "OME_API_TOKEN is required"
[[ -z "${STREAM_SECRET:-}"   ]] && error "STREAM_SECRET is required"
[[ -z "${ADMIN_PASSWORD:-}"  ]] && error "ADMIN_PASSWORD is required"
[[ -z "${SMTP_HOST:-}"       ]] && error "SMTP_HOST is required"
TZ="${TZ:-Europe/Vienna}"

# ── Install Docker ────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  info "Installing Docker"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
    https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  info "Docker installed"
else
  info "Docker already installed"
fi

# ── Install certbot + openssl ─────────────────────────────────────────────────
if ! command -v certbot &>/dev/null; then
  info "Installing certbot"
  apt-get install -y -qq certbot
fi
if ! command -v openssl &>/dev/null; then
  apt-get install -y -qq openssl
fi

# ── Create directory structure ────────────────────────────────────────────────
info "Creating directory structure at $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"/{config,auth,nginx/html,nginx/certbot-webroot}

# Copy repo files
cp -r config/.  "$INSTALL_DIR/config/"
cp -r auth/.    "$INSTALL_DIR/auth/"
cp docker-compose.yml "$INSTALL_DIR/"

# ── Generate nginx.conf from template ────────────────────────────────────────
info "Generating nginx.conf"
TOKEN_B64=$(printf '%s' "$OME_API_TOKEN" | base64 | tr -d '\n')
sed \
  -e "s|@@DOMAIN@@|$DOMAIN|g" \
  -e "s|@@OME_API_TOKEN_B64@@|$TOKEN_B64|g" \
  nginx/nginx.conf.template > "$INSTALL_DIR/nginx/nginx.conf"

# ── Generate Server.xml from template ────────────────────────────────────────
info "Generating Server.xml"
sed \
  -e "s|@@DOMAIN@@|$DOMAIN|g" \
  -e "s|@@OME_API_TOKEN@@|$OME_API_TOKEN|g" \
  "$INSTALL_DIR/config/Server.xml" > "$INSTALL_DIR/config/Server.xml.tmp"
mv "$INSTALL_DIR/config/Server.xml.tmp" "$INSTALL_DIR/config/Server.xml"

# ── Copy HTML files ───────────────────────────────────────────────────────────
info "Copying viewer page"
cp nginx/html/index.html  "$INSTALL_DIR/nginx/html/"
cp nginx/html/login.html  "$INSTALL_DIR/nginx/html/"
cp nginx/html/admin.html  "$INSTALL_DIR/nginx/html/"

# Generate config.js
cat > "$INSTALL_DIR/nginx/html/config.js" <<EOF
const STREAM_CONFIG = {
  gracePeriodMs  : 60 * 60 * 1000,
  pollIntervalMs : 5000,
  streamSecret   : '$STREAM_SECRET',
  streamSeparator: '~',
};
EOF

# ── Admin htpasswd ────────────────────────────────────────────────────────────
info "Generating admin credentials"
ADMIN_HASH=$(openssl passwd -apr1 "$ADMIN_PASSWORD")
printf 'admin:%s\n' "$ADMIN_HASH" > "$INSTALL_DIR/nginx/admin_htpasswd"

# ── Obtain TLS certificate ────────────────────────────────────────────────────
if [[ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
  info "Obtaining TLS certificate for $DOMAIN"
  warn "Ensure $DOMAIN points to this server ($OME_HOST_IP) before continuing"
  read -rp "  Press Enter to request certificate (Ctrl+C to abort)..."
  certbot certonly --standalone --non-interactive --agree-tos \
    --email "$EMAIL" -d "$DOMAIN"
  info "Certificate obtained"
else
  info "TLS certificate already exists"
fi

# ── Certbot renewal hook ──────────────────────────────────────────────────────
HOOK_DIR="/etc/letsencrypt/renewal-hooks/deploy"
mkdir -p "$HOOK_DIR"
cat > "$HOOK_DIR/restart-ome.sh" <<'HOOK'
#!/usr/bin/env bash
cd /opt/ome && docker compose restart nginx ome
HOOK
chmod +x "$HOOK_DIR/restart-ome.sh"
info "Certbot renewal hook installed"

# ── Start services ────────────────────────────────────────────────────────────
info "Starting containers"
cd "$INSTALL_DIR"
docker compose pull ome nginx
docker compose build auth
docker compose up -d

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}  Setup complete!${NC}"
echo ""
echo "  Viewer page : https://$DOMAIN"
echo "  Admin page  : https://$DOMAIN/admin.html  (user: admin)"
echo ""
echo "  OBS settings:"
echo "    Server    : rtmp://$OME_HOST_IP/live"
echo "    Stream key: ${STREAM_SECRET}~YourStreamName"
echo ""
echo "  Next steps:"
echo "    1. Visit the admin page and add viewer email addresses"
echo "    2. Or generate invite links to share"
echo ""
echo "  Manage: cd $INSTALL_DIR && docker compose [logs|restart|down]"
echo ""
