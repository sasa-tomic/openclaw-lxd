#!/bin/bash
# Hourly librarian pass:
# - add safe wikilinks + conflict scan (link_and_conflict_scan.py)
# - scan last hour messenger logs for follow-ups (read-only)
# - quick LLM maintenance via opencode (every 4 hours)
#
# Output is mostly via notes + journald; keep quiet when nothing interesting.

set -euo pipefail

HB_DIR="/projects/automations/heartbeat"
OBSIDIAN_DIR="/projects/automations/obsidian"

# 1) Links + conflicts (fast, always run)
LINKER="$OBSIDIAN_DIR/link_and_conflict_scan.py"
if [ -x "$LINKER" ]; then
  "$LINKER" >/dev/null 2>&1 || true
fi

# 2) Chat follow-ups (last 60m)
MINUTES=60 "$HB_DIR/chat-followups-last-hour.sh" >/dev/null 2>&1 || true

# 3) Quick LLM maintenance (every 4 hours to avoid rate limits)
HOUR=$(date +%H)
if [[ "$HOUR" =~ ^(00|04|08|12|16|20)$ ]]; then
  PICKLE_MAINT="$OBSIDIAN_DIR/pickle_obsidian_maintenance.py"
  if [ -x "$PICKLE_MAINT" ]; then
    python3 "$PICKLE_MAINT" \
      --mode apply \
      --rule-id quick_scan \
      --timeout 180 \
      --no-telegram \
      >/dev/null 2>&1 || true
  fi
fi
