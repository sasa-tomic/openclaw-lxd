#!/bin/bash
# Obsidian Notes Maintenance Script
# Runs daily to keep the vault organized
# Usage: ./obsidian-maintenance.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTES_DIR="/projects/Notes"
MEMORY_DIR="/home/openclaw/clawd/memory"
STATE_FILE="$MEMORY_DIR/obsidian-maintenance-state.json"
ORG_DIR="$NOTES_DIR/.organization"
CHANGELOG="$ORG_DIR/changelog.md"

echo "=== OBSIDIAN NOTES MAINTENANCE ==="
DATE=$(date -Iseconds)
# Default to quiet output (cron delivery should be short). Set VERBOSE=1 for debugging.
VERBOSE="${VERBOSE:-0}"
[ "$VERBOSE" -eq 1 ] && echo "Date: $DATE"

# Ensure directories exist
mkdir -p "$ORG_DIR"

# Ensure state file exists
if [ ! -f "$STATE_FILE" ]; then
    echo '{"lastMaintenance": null}' > "$STATE_FILE"
fi

# Ensure changelog exists
if [ ! -f "$CHANGELOG" ]; then
    echo "# Obsidian Vault Changelog" > "$CHANGELOG"
    echo "" >> "$CHANGELOG"
    echo "## $(date -I)" >> "$CHANGELOG"
    echo "- Created changelog" >> "$CHANGELOG"
fi

CHANGES_MADE=""

# 1. Check cleanup queue from README.md
if [ -f "$ORG_DIR/README.md" ]; then
    # Extract *pending* cleanup items from README.
    # Only treat markdown task items as actionable queue entries: "- [ ] ..."
    CLEANUP_ITEMS=$(awk '
      /^## Cleanup Queue/{flag=1; next}
      flag && /^## / {exit}
      flag && /Queue cleared/ {exit}
      flag && /^- \[ \]/ {print}
    ' "$ORG_DIR/README.md")

    if [ -n "$CLEANUP_ITEMS" ]; then
        CLEANUP_COUNT=$(printf "%s\n" "$CLEANUP_ITEMS" | sed '/^$/d' | wc -l)
        [ "$VERBOSE" -eq 1 ] && {
            echo ""
            echo "Cleanup queue items: $CLEANUP_COUNT"
            echo "(showing up to 10)"
            printf "%s\n" "$CLEANUP_ITEMS" | head -10
        }

        # For now, just log what we found; actual cleanup should be explicit.
        CHANGES_MADE="$CHANGES_MADE
- Reviewed cleanup queue in .organization/README.md ($CLEANUP_COUNT items)"
    else
        [ "$VERBOSE" -eq 1 ] && echo "No cleanup items in queue."
    fi
fi

# 2. Check for new files at root level that need organizing
# Ignore known intentional root files.
ROOT_FILES=$(find "$NOTES_DIR" -maxdepth 1 -type f -name "*.md" \
  ! -name "TODO.md" \
  ! -name "🏠 Home.md" \
  | sort)

if [ -n "$ROOT_FILES" ]; then
    ROOT_COUNT=$(printf "%s\n" "$ROOT_FILES" | sed '/^$/d' | wc -l)
    [ "$VERBOSE" -eq 1 ] && {
        echo ""
        echo "Root-level .md files (excluding TODO/Home): $ROOT_COUNT"
        printf "%s\n" "$ROOT_FILES" | head -20
    }
    CHANGES_MADE="$CHANGES_MADE
- Found root-level files that may need organization ($ROOT_COUNT files)"
else
    [ "$VERBOSE" -eq 1 ] && echo "No root-level files found (excluding TODO/Home)."
fi

# 3. Add wikilinks + scan for conflicts (safe additions + report)
LINKER="/projects/automations/obsidian/link_and_conflict_scan.py"
if [ -x "$LINKER" ]; then
    LINK_JSON=$("$LINKER" 2>/dev/null || true)
    LINK_COUNT=$(echo "$LINK_JSON" | jq -r '.linked | length' 2>/dev/null || echo 0)
    CONFLICT_COUNT=$(echo "$LINK_JSON" | jq -r '.conflicts | length' 2>/dev/null || echo 0)

    if [ "$LINK_COUNT" != "0" ]; then
        CHANGES_MADE="$CHANGES_MADE
- Added wikilinks in $LINK_COUNT recently modified note(s)"
    fi

    if [ "$CONFLICT_COUNT" != "0" ]; then
        CHANGES_MADE="$CHANGES_MADE
- ⚠️ Possible conflicts found ($CONFLICT_COUNT) — review recommended"
        # Write details to a temp file for inspection
        echo "$LINK_JSON" > /tmp/obsidian-linker-report.json
    fi
fi

# 4. Check WhatsApp sync health
[ "$VERBOSE" -eq 1 ] && {
  echo ""
  echo "Checking WhatsApp sync health..."
}
if command -v wacli &> /dev/null; then
    if WACLI_OUTPUT=$(wacli doctor 2>&1); then
        if echo "$WACLI_OUTPUT" | grep -q "AUTHENTICATED.*true"; then
            echo "✅ WhatsApp sync: Authenticated"

            # Check if service is running
            if systemctl --user is-active wacli-sync &> /dev/null; then
                echo "✅ wacli-sync service: Running"
            else
                echo "⚠️ wacli-sync service: Not running"
                CHANGES_MADE="$CHANGES_MADE
- ⚠️ wacli-sync service not running (needs attention)"
            fi
        else
            echo "❌ WhatsApp sync: NOT AUTHENTICATED"
            CHANGES_MADE="$CHANGES_MADE
- ❌ WhatsApp sync not authenticated (needs QR re-link)"
        fi
    else
        echo "⚠️ Could not run wacli doctor: $WACLI_OUTPUT"
    fi
else
    echo "⚠️ wacli not found"
fi

# 4. Weekly tasks (check if it's Friday)
DAY_OF_WEEK=$(date +%u)
if [ "$DAY_OF_WEEK" -eq 5 ]; then
    [ "$VERBOSE" -eq 1 ] && {
      echo ""
      echo "Friday weekly tasks:"
    }

    # Check TODO.md for stale items
    TODO_FILE="$NOTES_DIR/TODO.md"
    if [ -f "$TODO_FILE" ]; then
        # grep -c returns exit code 1 when there are 0 matches (while still printing "0"),
        # so don't append a second "0" via `|| echo 0` (it breaks arithmetic).
        TODO_COUNT=$(grep -c "^- \[" "$TODO_FILE" || true)
        DONE_COUNT=$(grep -c "^- \[x\]" "$TODO_FILE" || true)
        PENDING_COUNT=$((TODO_COUNT - DONE_COUNT))
        [ "$VERBOSE" -eq 1 ] && echo "TODO.md: $PENDING_COUNT pending items"

        if [ "$PENDING_COUNT" -gt 20 ]; then
            CHANGES_MADE="$CHANGES_MADE
- TODO.md has $PENDING_COUNT pending items (consider review)"
        fi
    fi

    # Check Daily/ for items worth extracting
    DAILY_DIR="$NOTES_DIR/Daily"
    if [ -d "$DAILY_DIR" ]; then
        # Check last 7 days of daily notes
        RECENT_DAILIES=$(find "$DAILY_DIR" -name "*.md" -mtime -7 | wc -l)
        [ "$VERBOSE" -eq 1 ] && echo "Daily notes (last 7 days): $RECENT_DAILIES"

        if [ "$RECENT_DAILIES" -gt 0 ]; then
            CHANGES_MADE="$CHANGES_MADE
- $RECENT_DAILIES daily notes from last 7 days (may contain items for extraction)"
        fi
    fi
fi

# Update state
jq --arg date "$DATE" '.lastMaintenance = $date' "$STATE_FILE" > "${STATE_FILE}.tmp"
mv "${STATE_FILE}.tmp" "$STATE_FILE"

# Log changes if any
if [ -n "$CHANGES_MADE" ]; then
    echo ""
    echo "Changes/observations logged:"
    echo "$CHANGES_MADE"

    # Append to changelog
    echo "" >> "$CHANGELOG"
    echo "## $(date -I)" >> "$CHANGELOG"
    echo "$CHANGES_MADE" >> "$CHANGELOG"

    # Write to file for cron handler
    echo "MAINTENANCE:$CHANGES_MADE" > /tmp/obsidian-maintenance-results.txt
else
    echo "No changes or observations to report."
fi

echo ""
echo "Maintenance complete."
