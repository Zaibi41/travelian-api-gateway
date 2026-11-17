#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# Travelian API — one-time server setup (run by an administrator, not by CI)
#
# Usage (on the Lightsail instance, from a copy of this repo or of deploy/):
#   sudo LEGACY_API_TARGET=d-xxxxxxxxxx.execute-api.us-east-1.amazonaws.com \
#        ACME_EMAIL=you@example.com \
#        bash deploy/server-setup.sh
#
# Safe to run again: every step checks before it changes anything.
# It never prints the database password.
# ============================================================

APP_DIR="/opt/travelian-api"
SHARED_ENV="$APP_DIR/shared/.env"
SERVICE="travelian-api.service"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

DB_NAME="travelian"
DB_USER="travelian_app"

LEGACY_API_TARGET="${LEGACY_API_TARGET:?Set LEGACY_API_TARGET to the API Gateway domain name of api.travelian.pk}"
ACME_EMAIL="${ACME_EMAIL:?Set ACME_EMAIL for certificate expiry notices}"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run with sudo."
    exit 1
fi

if [[ ! "$LEGACY_API_TARGET" =~ ^[a-z0-9-]+(\.[a-z0-9-]+)*\.(amazonaws\.com|cloudfront\.net)$ ]]; then
    echo "ERROR: LEGACY_API_TARGET must be a bare hostname such as d-abc123.execute-api.us-east-1.amazonaws.com"
    exit 1
fi

if [[ ! -f "$SCRIPT_DIR/Caddyfile" ]]; then
    echo "ERROR: deploy/Caddyfile not found next to this script."
    exit 1
fi

if [[ ! -d "$APP_DIR/shared" ]]; then
    echo "ERROR: $APP_DIR/shared is missing; this is not the prepared Travelian server."
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive

# ------------------------------------------------------------
# 1. Swap (instance has ~1 GB RAM and no swap)
# ------------------------------------------------------------

if ! swapon --show | grep -q .; then
    echo "Adding 1 GB swap..."
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ------------------------------------------------------------
# 2. Packages: PostgreSQL and Caddy
# ------------------------------------------------------------

apt-get update -q
apt-get install -y -q postgresql debian-keyring debian-archive-keyring apt-transport-https curl gnupg

if ! command -v caddy >/dev/null; then
    echo "Installing Caddy..."
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q
    apt-get install -y -q caddy
fi

# ------------------------------------------------------------
# 3. Database (local only; PostgreSQL listens on localhost by default)
# ------------------------------------------------------------

systemctl enable --now postgresql

psql_admin() { sudo -u postgres psql -v ON_ERROR_STOP=1 -qtA "$@"; }

if [[ "$(psql_admin -c "SELECT 1 FROM pg_roles WHERE rolname = '$DB_USER'")" != "1" ]]; then
    echo "Creating database role $DB_USER..."
    DB_PASSWORD="$(openssl rand -hex 24)"
    psql_admin -c "CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASSWORD'"
else
    DB_PASSWORD=""
fi

if [[ "$(psql_admin -c "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'")" != "1" ]]; then
    echo "Creating database $DB_NAME..."
    psql_admin -c "CREATE DATABASE $DB_NAME OWNER $DB_USER"
fi

# ------------------------------------------------------------
# 4. Shared environment: add database settings that are missing
# ------------------------------------------------------------

touch "$SHARED_ENV"
chown deploy:deploy "$SHARED_ENV"
chmod 600 "$SHARED_ENV"

set_if_missing() {
    local key="$1" value="$2"
    if ! grep -q "^${key}=" "$SHARED_ENV"; then
        printf '%s=%s\n' "$key" "$value" >> "$SHARED_ENV"
    fi
}

set_if_missing DB_HOST 127.0.0.1
set_if_missing DB_PORT 5432
set_if_missing DB_NAME "$DB_NAME"
set_if_missing DB_USER "$DB_USER"

if [[ -n "$DB_PASSWORD" ]]; then
    if grep -q '^DB_PASSWORD=' "$SHARED_ENV"; then
        sed -i "s/^DB_PASSWORD=.*/DB_PASSWORD=$DB_PASSWORD/" "$SHARED_ENV"
    else
        printf 'DB_PASSWORD=%s\n' "$DB_PASSWORD" >> "$SHARED_ENV"
    fi
    echo "Database password written to $SHARED_ENV (not shown)."
fi
unset DB_PASSWORD

# ------------------------------------------------------------
# 5. Caddy: HTTPS on 80/443, /v1 -> gateway, rest -> legacy API
# ------------------------------------------------------------

install -m 644 "$SCRIPT_DIR/Caddyfile" /etc/caddy/Caddyfile
mkdir -p /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/travelian.conf <<EOF
[Service]
Environment=LEGACY_API_TARGET=$LEGACY_API_TARGET
Environment=ACME_EMAIL=$ACME_EMAIL
EOF
chmod 644 /etc/systemd/system/caddy.service.d/travelian.conf
systemctl daemon-reload

LEGACY_API_TARGET="$LEGACY_API_TARGET" ACME_EMAIL="$ACME_EMAIL" \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null

systemctl enable caddy
systemctl restart caddy

# ------------------------------------------------------------
# 6. Host firewall (the Lightsail firewall is separate; open 80/443 there too)
# ------------------------------------------------------------

if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
fi

# ------------------------------------------------------------
# 7. Gateway service starts at boot
# ------------------------------------------------------------

systemctl enable "$SERVICE"

echo "============================================"
echo "Server setup complete."
echo "Next: fill the rest of $SHARED_ENV, deploy, run deploy/migrate.sh."
echo "See deploy/CUTOVER.md."
echo "============================================"
