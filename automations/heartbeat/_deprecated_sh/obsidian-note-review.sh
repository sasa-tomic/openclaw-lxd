#!/bin/bash
# Obsidian Note Review Script
# Runs every 30-60 minutes to review recently modified notes
# Usage: ./obsidian-note-review.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTES_DIR="/projects/Notes"
MEMORY_DIR="/home/openclaw/clawd/memory"
STATE_FILE="$MEMORY_DIR/obsidian-note-review-state.json"

mkdir -p "$MEMORY_DIR"

# Ensure state file exists and is valid JSON (it may have been corrupted by older versions)
if [ ! -f "$STATE_FILE" ] || ! jq -e . "$STATE_FILE" >/dev/null 2>&1; then
    echo '{"lastCheck": null}' > "$STATE_FILE"
fi
rm -f "${STATE_FILE}.tmp" 2>/dev/null || true

# Find recently modified notes (last 60 minutes)
echo "=== OBSIDIAN NOTE REVIEW ==="

CHANGED_FILES=$("$SCRIPT_DIR/../obsidian/note-watcher.sh" 60)

if [ -z "$CHANGED_FILES" ]; then
    echo "No changes in last 60 minutes."

    # Update state
    jq --arg date "$(date -Iseconds)" '.lastCheck = $date' "$STATE_FILE" > "${STATE_FILE}.tmp"
    mv "${STATE_FILE}.tmp" "$STATE_FILE"

    exit 0
fi

echo "Found changes in:"
echo "$CHANGED_FILES"
echo ""

# Track observations
OBSERVATIONS=""

# Process each changed file
while IFS= read -r file; do
    if [ -z "$file" ]; then
        continue
    fi

    echo "---"
    echo "Reviewing: $file"

    # Skip chat logs (message-watcher handles Signal/WhatsApp; Telegram logs are noisy too)
    if [[ "$file" == *Signal/* ]] || [[ "$file" == *WhatsApp/* ]] || [[ "$file" == *Telegram/* ]]; then
        echo "Chat log - skipping (handled elsewhere)"
        continue
    fi

    # Read the file
    if [ -f "$NOTES_DIR/$file" ]; then
        content=$(cat "$NOTES_DIR/$file")

        # Check for implicit TODOs
        if grep -qi "TODO\|FIXME\|REMEMBER\|don't forget" <<< "$content"; then
            OBSERVATIONS="$OBSERVATIONS
📝 $file: Contains TODO markers that might need extraction"
        fi

        # Check for time-sensitive content
        if grep -qiE "by\s+(tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|deadline|due date|meeting|call" <<< "$content"; then
            OBSERVATIONS="$OBSERVATIONS
⏰ $file: May contain time-sensitive items"
        fi

        # Check for confusion markers
        if grep -qiE "\?\?|\.\.\.|\[todo\]|\[check\]|unclear" <<< "$content"; then
            OBSERVATIONS="$OBSERVATIONS
❓ $file: May need clarification"
        fi

        # Check for project connections
        if grep -qiE "voki|decent cloud|axiom" <<< "$content"; then
            OBSERVATIONS="$OBSERVATIONS
🔗 $file: Relates to active project"
        fi
    fi
done <<< "$CHANGED_FILES"

# Update state
jq --arg date "$(date -Iseconds)" '.lastCheck = $date' "$STATE_FILE" > "${STATE_FILE}.tmp"
mv "${STATE_FILE}.tmp" "$STATE_FILE"

# Send observations to Telegram if any
if [ -n "$OBSERVATIONS" ]; then
    echo ""
    echo "Observations to report:"
    echo "$OBSERVATIONS"

    echo "OBSERVATIONS:$OBSERVATIONS" > /tmp/obsidian-note-review-results.txt
else
    echo "No significant observations."
fi
