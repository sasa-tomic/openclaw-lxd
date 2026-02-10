#!/bin/bash
# Hourly librarian pass:
# - add safe wikilinks + conflict scan (link_and_conflict_scan.py)
# - scan last hour messenger logs for follow-ups (read-only)
#
# Output is mostly via notes + journald; keep quiet when nothing interesting.

set -euo pipefail

HB_DIR="/projects/automations/heartbeat"

# 1) Links + conflicts
LINKER="/projects/automations/obsidian/link_and_conflict_scan.py"
if [ -x "$LINKER" ]; then
  "$LINKER" >/dev/null 2>&1 || true
fi

# 2) Chat follow-ups (last 60m)
MINUTES=60 "$HB_DIR/chat-followups-last-hour.sh" >/dev/null 2>&1 || true
