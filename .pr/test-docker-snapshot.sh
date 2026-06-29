#!/usr/bin/env bash
# Test Docker snapshot + restore manually
# Usage: ./test-docker-snapshot.sh <sandbox_id>

set -euo pipefail

SANDBOX_ID="${1:-}"
if [ -z "$SANDBOX_ID" ]; then
    echo "Usage: $0 <sandbox_id>"
    echo "Example: $0 oh-agent-server-6s83O2GoRM3ybdZtNV0h2D"
    exit 1
fi

echo "=== Step 1: Snapshot the container ==="
SNAPSHOT_NAME="oh-snapshot-${SANDBOX_ID}-$(date +%s)"
SNAPSHOT_NAME="${SNAPSHOT_NAME,,}"  # lowercase for docker
echo "Creating snapshot: $SNAPSHOT_NAME"
docker commit "$SANDBOX_ID" "$SNAPSHOT_NAME"

echo ""
echo "=== Step 2: Verify snapshot image exists ==="
docker image inspect "$SNAPSHOT_NAME" --format='Image {{.Id}} created' || { echo "FAIL: Snapshot not found!"; exit 1; }

echo ""
echo "=== Step 3: Restore container from snapshot ==="
docker run -d --name "${SANDBOX_ID}-restored" "$SNAPSHOT_NAME"
echo "Restored container started"

echo ""
echo "=== Step 4: Verify restored container is running ==="
docker ps --filter "name=${SANDBOX_ID}-restored" --format 'Container {{.Names}} is {{.Status}}'

echo ""
echo "=== Step 5: Cleanup ==="
docker rm -f "${SANDBOX_ID}-restored"
docker rmi "$SNAPSHOT_NAME"
echo "Cleanup complete"

echo ""
echo "=== SUCCESS: Snapshot + restore works! ==="
