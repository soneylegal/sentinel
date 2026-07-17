#!/bin/sh
# ─────────────────────────────────────────────────────────
# Sentinel - Entrypoint Script
# ─────────────────────────────────────────────────────────
# Automatically detects the GID of /var/run/docker.sock
# and ensures the sentinel user belongs to that group so
# the daemon can communicate with Docker without running
# as root permanently.
# ─────────────────────────────────────────────────────────

set -e

SOCKET="/var/run/docker.sock"

if [ -S "$SOCKET" ]; then
    DOCKER_GID=$(stat -c '%g' "$SOCKET")
    echo "Detected Docker socket GID: $DOCKER_GID"

    # Get or create a group with the socket's GID
    GROUP_NAME=$(getent group "$DOCKER_GID" | cut -d: -f1 || true)

    if [ -z "$GROUP_NAME" ]; then
        groupadd -g "$DOCKER_GID" docker_host
        GROUP_NAME="docker_host"
    fi

    # Add sentinel user to the group (using gpasswd, available in slim)
    gpasswd -a sentinel "$GROUP_NAME" > /dev/null 2>&1 || true

    echo "User 'sentinel' added to group '$GROUP_NAME' (GID=$DOCKER_GID)"
else
    echo "WARNING: Docker socket not found at $SOCKET"
fi

# Drop privileges back to sentinel user and exec the main process
exec gosu sentinel python -m src.main "$@"
