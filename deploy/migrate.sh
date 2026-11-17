#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# Travelian API — apply database migrations (explicit, never at startup)
# Usage, as the deploy user on the server:
#   bash /opt/travelian-api/current/deploy/migrate.sh            # upgrade to head
#   bash /opt/travelian-api/current/deploy/migrate.sh current    # show revision
#
# Uses the active release's code and the DB_* settings from the shared .env.
# Migrations are not reverted by rollback.sh; take a backup first:
#   pg_dump -h 127.0.0.1 -U travelian_app travelian > ~/travelian-$(date +%F-%H%M).sql
# ============================================================

APP_DIR="/opt/travelian-api"
CURRENT="$APP_DIR/current"
SHARED_ENV="$APP_DIR/shared/.env"
LOCK_FILE="$APP_DIR/shared/deployment.lock"

if [[ ! -L "$CURRENT" || ! -x "$CURRENT/.venv/bin/alembic" ]]; then
    echo "ERROR: No active release with alembic installed."
    exit 1
fi

if [[ ! -r "$SHARED_ENV" ]]; then
    echo "ERROR: Shared environment file is missing or unreadable."
    exit 1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "ERROR: A deployment or rollback is running."
    exit 1
fi

# Export only the database settings; values are plain (no quotes, no spaces).
while IFS='=' read -r key value; do
    export "$key=$value"
done < <(grep -E '^DB_(HOST|PORT|NAME|USER|PASSWORD)=' "$SHARED_ENV")

if [[ -z "${DB_PASSWORD:-}" ]]; then
    echo "ERROR: DB_PASSWORD is not set in the shared environment file."
    exit 1
fi

cd "$(readlink -f "$CURRENT")"

if [[ "${1:-upgrade}" == "current" ]]; then
    .venv/bin/alembic current
else
    .venv/bin/alembic upgrade head
    .venv/bin/alembic current
fi
