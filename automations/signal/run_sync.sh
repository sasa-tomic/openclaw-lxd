#!/bin/bash
# Signal to Obsidian Live Sync Service
# Continuously receives messages and syncs to Obsidian

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIGNAL_CLI="${SIGNAL_CLI:-/home/openclaw/homebrew/bin/signal-cli}"
PHONE="${SIGNAL_PHONE:-+41798471964}"

log() {
    echo "[$(date -Iseconds)] $*" >&2
}

log "Starting Signal sync for $PHONE"
log "Script dir: $SCRIPT_DIR"

while true; do
    log "Polling for messages..."
    
    # Receive with 60s timeout, output JSON, pipe to processor
    if ! "$SIGNAL_CLI" -o json -u "$PHONE" receive -t 60 2>&1 | \
         python3 "$SCRIPT_DIR/sync_to_obsidian.py"; then
        log "WARN: Sync cycle failed, will retry"
    fi
    
    # Brief pause between polls
    sleep 5
done
