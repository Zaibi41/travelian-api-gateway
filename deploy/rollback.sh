
#!/usr/bin/env bash
set -Eeuo pipefail

# Travelian API Gateway — Manual Production Rollback
# Usage: bash deploy/rollback.sh <release-ID>

APP_DIR="/opt/travelian-api"
RELEASES_DIR="$APP_DIR/releases"
CURRENT="$APP_DIR/current"
SERVICE="travelian-api.service"
# The gateway's local health check; HEALTH_URL overrides it if the service listens elsewhere.
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
LOCK_FILE="$APP_DIR/shared/deployment.lock"

TARGET="${1:?Usage: rollback.sh RELEASE_ID}"

# 1. Validate target
if [[ ! "$TARGET" =~ ^[a-f0-9]{40}(-[0-9]+-[0-9]+)?$ ]]; then
    echo "ERROR: Invalid release ID."
    exit 1
fi

TARGET_DIR="$RELEASES_DIR/$TARGET"

if [[ ! -d "$TARGET_DIR" ]]; then
    echo "ERROR: Target release does not exist."
    exit 1
fi

if [[ ! -f "$TARGET_DIR/.deployment-success" || ! -x "$TARGET_DIR/.venv/bin/python" ]]; then
    echo "ERROR: Target release has not completed a healthy deployment."
    exit 1
fi

# 2. Prevent concurrent deployment and rollback
exec 9>"$LOCK_FILE"

if ! flock -n 9; then
    echo "ERROR: Another deployment or rollback is running."
    exit 1
fi

# 3. Identify currently active release
if [[ ! -L "$CURRENT" ]]; then
    echo "ERROR: No active release symlink exists."
    exit 1
fi

PREVIOUS="$(readlink -f "$CURRENT")"

if [[ ! -d "$PREVIOUS" ]]; then
    echo "ERROR: Current release is missing."
    exit 1
fi


# 4. Health verification
check_health() {
    local response

    response="$(curl \
        --fail \
        --silent \
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

if [[ "$PREVIOUS" == "$TARGET_DIR" ]]; then
    if wait_for_health; then
        echo "Target release is already active and healthy."
        exit 0
    fi
    echo "ERROR: Active target release is unhealthy."
    exit 1
fi

# 5. Atomic symlink switch
activate_release() {
    local target="$1"
    local next_link="$APP_DIR/current.next"

    rm -f -- "$next_link" || return 1
    ln -s -- "$target" "$next_link" || return 1
    mv -Tf -- "$next_link" "$CURRENT"
}

# 6. Activate requested release
echo "Rolling back to: $TARGET"

activate_release "$TARGET_DIR"

if sudo -n systemctl restart "$SERVICE" && wait_for_health; then
    echo "Rollback successful."
    echo "Active release: $TARGET"
    exit 0
fi

# 7. Restore original release if rollback failed
echo "ERROR: Target release failed health verification."
echo "Restoring original release..."

activate_release "$PREVIOUS"

if sudo -n systemctl restart "$SERVICE" && wait_for_health; then
    echo "Original release restored successfully."
else
    echo "CRITICAL: Original release could not be restored to a healthy state."
fi

exit 1
