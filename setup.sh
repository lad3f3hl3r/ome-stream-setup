#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

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
  echo "  TLS mode: certbot / selfsigned / proxy"
  read -rp  "  TLS_MODE [certbot]:               "  TLS_MODE
  read -rp  "  OME API token:                    "  OME_API_TOKEN
  read -rp  "  Stream secret (OBS key prefix):   "  STREAM_SECRET
  read -rp  "  Admin page password:              "  ADMIN_PASSWORD
  read -rp  "  LLHLS port [8090, empty=proxy]:   "  LLHLS_PORT
  read -rp  "  WebRTC port [3334, empty=proxy]:  "  WEBRTC_PORT
  echo ""
  echo "  SMTP configuration:"
  read -rp  "  SMTP host:                        "  SMTP_HOST
  read -rp  "  SMTP port [587]:                  "  SMTP_PORT
  read -rp  "  SMTP username:                    "  SMTP_USER
  read -rsp "  SMTP password:                    "  SMTP_PASS; echo ""
  read -rp  "  From address:                     "  SMTP_FROM
  read -rp  "  STARTTLS? [true]:                 "  SMTP_TLS
  read -rp  "  SMTPS/SSL? [false]:               "  SMTP_SSL

  TZ="${TZ:-Europe/Vienna}"
  TLS_MODE="${TLS_MODE:-certbot}"
  SMTP_PORT="${SMTP_PORT:-587}"
  SMTP_TLS="${SMTP_TLS:-true}"
  SMTP_SSL="${SMTP_SSL:-false}"
  LLHLS_PORT="${LLHLS_PORT:-8090}"
  WEBRTC_PORT="${WEBRTC_PORT:-3334}"

  # Write .env — single-quote SMTP_PASS to handle special characters
  cat > .env <<EOF
DOMAIN=$DOMAIN
OME_HOST_IP=$OME_HOST_IP
EMAIL=$EMAIL
TZ=$TZ
TLS_MODE=$TLS_MODE
OME_API_TOKEN=$OME_API_TOKEN
STREAM_SECRET=$STREAM_SECRET
ADMIN_PASSWORD=$ADMIN_PASSWORD
LLHLS_PORT=$LLHLS_PORT
WEBRTC_PORT=$WEBRTC_PORT
SMTP_HOST=$SMTP_HOST
SMTP_PORT=$SMTP_PORT
SMTP_USER=$SMTP_USER
SMTP_PASS='$SMTP_PASS'
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
[[ -z "${OME_HOST_IP:-}"    ]] && error "OME_HOST_IP is required"
[[ -z "${OME_API_TOKEN:-}"  ]] && error "OME_API_TOKEN is required"
[[ -z "${STREAM_SECRET:-}"  ]] && error "STREAM_SECRET is required"
[[ -z "${ADMIN_PASSWORD:-}" ]] && error "ADMIN_PASSWORD is required"
[[ -z "${SMTP_HOST:-}"      ]] && error "SMTP_HOST is required"
TZ="${TZ:-Europe/Vienna}"
TLS_MODE="${TLS_MODE:-certbot}"
# Only default ports when not in proxy mode (proxy mode wants empty = no port suffix)
if [[ "$TLS_MODE" != "proxy" ]]; then
  LLHLS_PORT="${LLHLS_PORT:-8090}"
  WEBRTC_PORT="${WEBRTC_PORT:-3334}"
fi

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
  info "Docker already installed ($(docker --version | cut -d' ' -f3 | tr -d ','))"
fi

# ── Install tools ─────────────────────────────────────────────────────────────
apt-get install -y -qq openssl
if [[ "$TLS_MODE" == "certbot" ]]; then
  command -v certbot &>/dev/null || apt-get install -y -qq certbot
fi

# ── Create directory structure ────────────────────────────────────────────────
info "Creating directory structure at $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"/{config,auth,nginx/html,nginx/certbot-webroot}

cp -r config/. "$INSTALL_DIR/config/"
cp -r auth/.   "$INSTALL_DIR/auth/"
cp docker-compose.yml "$INSTALL_DIR/"

# ── Generate nginx.conf ───────────────────────────────────────────────────────
info "Generating nginx.conf (TLS_MODE=$TLS_MODE)"
TEMPLATE="nginx/nginx.conf.template"
[[ "$TLS_MODE" == "proxy" ]] && TEMPLATE="nginx/nginx-proxy.conf.template"
sed "s|@@DOMAIN@@|$DOMAIN|g" "$TEMPLATE" > "$INSTALL_DIR/nginx/nginx.conf"

# ── Generate Server.xml ───────────────────────────────────────────────────────
info "Generating Server.xml"
sed \
  -e "s|@@DOMAIN@@|$DOMAIN|g" \
  -e "s|@@OME_API_TOKEN@@|$OME_API_TOKEN|g" \
  "$INSTALL_DIR/config/Server.xml" > "$INSTALL_DIR/config/Server.xml.tmp"
mv "$INSTALL_DIR/config/Server.xml.tmp" "$INSTALL_DIR/config/Server.xml"

# ── Copy HTML files ───────────────────────────────────────────────────────────
info "Copying viewer page"
for f in index.html login.html admin.html; do
  sed "s|@@DOMAIN@@|$DOMAIN|g" "nginx/html/$f" > "$INSTALL_DIR/nginx/html/$f"
done

# Generate config.js
LLHLS_JS=$([[ -n "${LLHLS_PORT}" ]] && echo "${LLHLS_PORT}" || echo "null")
WEBRTC_JS=$([[ -n "${WEBRTC_PORT}" ]] && echo "${WEBRTC_PORT}" || echo "null")
cat > "$INSTALL_DIR/nginx/html/config.js" <<EOF
const STREAM_CONFIG = {
  gracePeriodMs  : 60 * 60 * 1000,
  pollIntervalMs : 5000,
  streamSecret   : '$STREAM_SECRET',
  streamSeparator: '~',
  llhlsPort  : $LLHLS_JS,
  webrtcPort : $WEBRTC_JS,
};
EOF

# ── Admin htpasswd ────────────────────────────────────────────────────────────
info "Generating admin credentials"
ADMIN_HASH=$(openssl passwd -apr1 "$ADMIN_PASSWORD")
printf 'admin:%s\n' "$ADMIN_HASH" > "$INSTALL_DIR/nginx/admin_htpasswd"

# ── TLS certificate ───────────────────────────────────────────────────────────
CERT_DIR="/etc/letsencrypt/live/$DOMAIN"

if [[ "$TLS_MODE" == "certbot" ]]; then
  if [[ ! -f "$CERT_DIR/fullchain.pem" ]]; then
    info "Obtaining Let's Encrypt certificate"
    warn "Ensure $DOMAIN resolves to this server and port 80 is reachable"
    read -rp "  Press Enter to request (Ctrl+C to abort)..."
    certbot certonly --standalone --non-interactive --agree-tos \
      --email "${EMAIL:-admin@$DOMAIN}" -d "$DOMAIN"
  else
    info "Certificate already exists"
  fi
  # Renewal hook
  mkdir -p /etc/letsencrypt/renewal-hooks/deploy
  cat > /etc/letsencrypt/renewal-hooks/deploy/restart-ome.sh <<'HOOK'
#!/usr/bin/env bash
cd /opt/ome && docker compose restart nginx ome
HOOK
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/restart-ome.sh

elif [[ "$TLS_MODE" == "selfsigned" ]]; then
  if [[ ! -f "$CERT_DIR/fullchain.pem" ]]; then
    info "Generating self-signed certificate"
    mkdir -p "$CERT_DIR"
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
      -keyout "$CERT_DIR/privkey.pem" \
      -out "$CERT_DIR/fullchain.pem" \
      -subj "/CN=$DOMAIN" \
      -addext "subjectAltName=DNS:$DOMAIN" 2>/dev/null
  else
    info "Certificate already exists"
  fi

elif [[ "$TLS_MODE" == "proxy" ]]; then
  info "TLS_MODE=proxy — nginx serves HTTP only, reverse proxy handles HTTPS"
fi

# ── docker-compose.yml: remove TLS-dependent volumes if proxy mode ────────────
if [[ "$TLS_MODE" == "proxy" ]]; then
  sed -i '/letsencrypt/d' "$INSTALL_DIR/docker-compose.yml"
fi

# ── Start services ────────────────────────────────────────────────────────────
info "Starting containers"
cd "$INSTALL_DIR"
docker compose pull ome nginx 2>&1 | grep -E 'Pull|Status|already' || true
docker compose build auth 2>&1 | tail -3
docker compose up -d

echo ""
echo -e "${GREEN}  Setup complete!${NC}"
echo ""
echo "  Viewer page : http${TLS_MODE:+s}://$DOMAIN"
echo "  Admin page  : http${TLS_MODE:+s}://$DOMAIN/admin.html  (user: admin)"
echo ""
echo "  OBS:"
echo "    Server    : rtmp://$OME_HOST_IP/live"
echo "    Stream key: ${STREAM_SECRET}~YourStreamName"
echo ""
echo "  Manage: cd $INSTALL_DIR && docker compose [logs|restart|down]"
echo ""
