
#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# Travelian API Gateway — Production Deployment
# Usage: bash deploy/deploy.sh <release-ID>
# ============================================================

APP_DIR="/opt/travelian-api"
RELEASES_DIR="$APP_DIR/releases"
CURRENT="$APP_DIR/current"
SHARED_DIR="$APP_DIR/shared"

SERVICE="travelian-api.service"
# The gateway's local health check; HEALTH_URL overrides it if the service listens elsewhere.
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"

LOCK_FILE="$SHARED_DIR/deployment.lock"

# ------------------------------------------------------------
# 1. Validate release ID
# ------------------------------------------------------------

RELEASE_ID="${1:?Usage: deploy.sh RELEASE_ID}"

if [[ ! "$RELEASE_ID" =~ ^[a-f0-9]{40}(-[0-9]+-[0-9]+)?$ ]]; then
    echo "ERROR: Release ID must be a Git SHA, optionally followed by run ID and attempt."
    exit 1
fi

RELEASE_DIR="$RELEASES_DIR/$RELEASE_ID"

if [[ ! -f "$RELEASE_DIR/requirements.txt" ]]; then
    echo "ERROR: Release or requirements.txt not found."
    exit 1
fi

if [[ ! -f "$RELEASE_DIR/app/main.py" ]]; then
    echo "ERROR: FastAPI application entry point not found."
    exit 1
fi

# ------------------------------------------------------------
# 2. Acquire deployment lock
# ------------------------------------------------------------

if [[ ! -r "$SHARED_DIR/.env" ]]; then
    echo "ERROR: Shared production environment file is missing or unreadable."
    exit 1
fi

exec 9>"$LOCK_FILE"

if ! flock -n 9; then
    echo "ERROR: Another deployment or rollback is running."
    exit 1
fi

# ------------------------------------------------------------
# 3. Identify previous release
# ------------------------------------------------------------

if [[ -e "$CURRENT" && ! -L "$CURRENT" ]]; then
    echo "ERROR: Current path is not a symlink."
    exit 1
fi

PREVIOUS=""

if [[ -L "$CURRENT" ]]; then
    PREVIOUS="$(readlink -f "$CURRENT")"
fi

if [[ -n "$PREVIOUS" && ! -d "$PREVIOUS" ]]; then
    echo "ERROR: Current release symlink is broken."
    exit 1
fi


if [[ "$PREVIOUS" == "$RELEASE_DIR" ]]; then
    echo "ERROR: Refusing to modify an active release; rerun the workflow for a fresh release ID."
    exit 1
fi

activated=false
completed=false

# ------------------------------------------------------------
# 4. Health check
# ------------------------------------------------------------

check_health() {
    local response

    response="$(curl \
        --fail \
        --silent \
        --show-error \
        --max-time 3 \
        "$HEALTH_URL")" || return 1

    [[ "$response" == '{"status":"ok"}' ]]
}

wait_for_health() {
    local attempt

    for attempt in $(seq 1 30); do
        if check_health; then
            return 0
        fi

        sleep 2
    done

    return 1
}

# ------------------------------------------------------------
# 5. Atomic release activation
# ------------------------------------------------------------

activate_release() {
    local target="$1"
    local next_link="$APP_DIR/current.next"

    rm -f -- "$next_link" || return 1
    ln -s -- "$target" "$next_link" || return 1
    mv -Tf -- "$next_link" "$CURRENT"
}

# ------------------------------------------------------------
# 6. Automatic rollback on failure
# ------------------------------------------------------------

rollback() {
    local original_status=$?

    trap - EXIT

    if [[ "$completed" == true ]]; then
        exit "$original_status"
    fi

    if [[ "$activated" != true ]]; then
        echo "Deployment failed before activation."
        exit "$original_status"
    fi

    echo "ERROR: Deployment failed."
    echo "Attempting automatic rollback..."

    if [[ -n "$PREVIOUS" && -d "$PREVIOUS" ]]; then

        if ! activate_release "$PREVIOUS"; then
            echo "CRITICAL: Failed to restore previous symlink."
            exit 1
        fi

        if ! sudo -n systemctl restart "$SERVICE"; then
            echo "CRITICAL: Previous release failed to restart."
            exit 1
        fi

        if wait_for_health; then
            echo "Rollback successful."
            echo "Previous release is healthy."
        else
            echo "CRITICAL: Previous release is not healthy."
            exit 1
        fi

    else
        echo "No previous release exists."
        echo "Stopping failed first deployment."

        if ! sudo -n systemctl stop "$SERVICE"; then
            echo "CRITICAL: Failed to stop failed first deployment."
            exit 1
        fi

        # Remove the active link only if it still points
        # to the failed first release.
        if [[ -L "$CURRENT" ]] &&
           [[ "$(readlink -f "$CURRENT")" == "$RELEASE_DIR" ]]; then
            rm -f -- "$CURRENT"
        fi
    fi

    exit "$original_status"
}

trap rollback EXIT

# ------------------------------------------------------------
# 7. Prepare Python environment
# ------------------------------------------------------------

echo "Preparing release: $RELEASE_ID"

cd "$RELEASE_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
    python3.12 -m venv .venv
fi

.venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 required"'

if [[ ! -f requirements.lock ]]; then
    echo "ERROR: Tested dependency lock is missing."
    exit 1
fi

echo "Installing Python dependencies..."

.venv/bin/python -m pip install \
    --disable-pip-version-check \
    --no-input \
    --no-cache-dir \
    --only-binary=:all: \
    -r requirements.lock

echo "Checking dependencies..."

.venv/bin/python -m pip check

echo "Validating Python source..."

.venv/bin/python -m compileall -q app

# ------------------------------------------------------------
# 8. Activate new release
# ------------------------------------------------------------

echo "Activating release: $RELEASE_ID"

activated=true
activate_release "$RELEASE_DIR"

echo "Restarting FastAPI service..."

sudo -n systemctl restart "$SERVICE"

# ------------------------------------------------------------
# 9. Verify application health
# ------------------------------------------------------------

echo "Waiting for FastAPI health check..."

if ! wait_for_health; then
    echo "ERROR: New release failed health verification."
    exit 1
fi

# ------------------------------------------------------------
# 10. Deployment successful
# ------------------------------------------------------------

touch "$RELEASE_DIR/.deployment-success"
completed=true

echo "============================================"
echo "Deployment successful"
echo "Release: $RELEASE_ID"
echo "Health: OK"
echo "============================================"
