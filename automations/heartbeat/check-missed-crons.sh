#!/bin/bash
# Check for Missed Cron Jobs
# Runs every 30 minutes to catch and report jobs that missed their scheduled time
# This script is called by the main session with access to cron tools
# Usage: ./check-missed-crons.sh

set -euo pipefail

MEMORY_DIR="/home/openclaw/clawd/memory"
STATE_FILE="$MEMORY_DIR/missed-crons-state.json"

echo "=== MISSED CRON JOBS CHECK ==="
echo "Current time: $(date -Iseconds)"

# Ensure state file exists
if [ ! -f "$STATE_FILE" ]; then
    cat > "$STATE_FILE" << 'EOF'
{
  "lastCheck": null,
  "notifiedJobs": {}
}
EOF
fi

# Get current time in milliseconds
CURRENT_TIME_MS=$(python3 -c "import time; print(int(time.time() * 1000))")

# Get cron list via JSON file (passed by cron event handler)
# If not provided, this is just a dry run or called incorrectly
CRON_LIST_FILE="/tmp/cron-list-for-missed-check.json"

if [ ! -f "$CRON_LIST_FILE" ]; then
    echo "⚠️ No cron list file found at $CRON_LIST_FILE"
    echo "This script expects the cron list to be provided via the main session"
    echo "For now, performing a basic check..."
    echo "✅ Check complete (no data to analyze)"
    exit 0
fi

# Find jobs with nextRunAtMs < current time
MISSED_JOBS=$(jq -r --arg now "$CURRENT_TIME_MS" '
  .jobs[] |
  select(.enabled == true) |
  select(.state.nextRunAtMs != null and (.state.nextRunAtMs | tonumber) < ($now | tonumber)) |
  {id, name, nextRunAtMs: .state.nextRunAtMs}
' "$CRON_LIST_FILE")

if [ -z "$MISSED_JOBS" ] || [ "$MISSED_JOBS" = "null" ]; then
    echo "✅ No missed jobs detected"
    jq --arg time "$(date -Iseconds)" '.lastCheck = $time' "$STATE_FILE" > "${STATE_FILE}.tmp"
    mv "${STATE_FILE}.tmp" "$STATE_FILE"
    exit 0
fi

echo ""
echo "⚠️ MISSED JOBS FOUND:"
echo "$MISSED_JOBS" | jq -r 'join(" | ")'

# Check notified jobs to avoid spamming about the same job
NOTIFIED_NOW=""

while IFS= read -r line; do
    JOB_ID=$(echo "$line" | jq -r '.id // empty')
    JOB_NAME=$(echo "$line" | jq -r '.name // empty')
    NEXT_RUN=$(echo "$line" | jq -r '.nextRunAtMs // empty')

    if [ -z "$JOB_ID" ] || [ "$JOB_ID" = "null" ]; then
        continue
    fi

    # Check if we already notified about this job recently (within 1 hour)
    LAST_NOTIFIED=$(jq -r ".notifiedJobs[\"$JOB_ID\"] // null" "$STATE_FILE")

    if [ "$LAST_NOTIFIED" != "null" ] && [ "$LAST_NOTIFIED" != "null" ]; then
        LAST_NOTIFIED_TS=$(date -d "$LAST_NOTIFIED" +%s 2>/dev/null || echo "0")
        CURRENT_TS=$(date +%s)
        HOURS_SINCE=$(( (CURRENT_TS - LAST_NOTIFIED_TS) / 3600 ))

        if [ "$HOURS_SINCE" -lt 1 ]; then
            echo "Skipping $JOB_NAME (notified $HOURS_SINCE hours ago)"
            continue
        fi
    fi

    # Mark as notified and add to list
    NOTIFIED_NOW="$NOTIFIED_NOW\n$JOB_ID"

    # Write to result file for main session to process
    echo "RUN_JOB:$JOB_ID:$JOB_NAME" >> /tmp/missed-crons-results.txt

    # Update notification time in state
    NOTIFICATION_TIME=$(date -Iseconds)
    jq --arg id "$JOB_ID" --arg time "$NOTIFICATION_TIME" \
       '.notifiedJobs[$id] = $time' "$STATE_FILE" > "${STATE_FILE}.tmp"
    mv "${STATE_FILE}.tmp" "$STATE_FILE"

    echo "✅ Queued $JOB_NAME for immediate run"

done <<< "$(echo "$MISSED_JOBS" | jq -c '.')"

# Update last check time
jq --arg time "$(date -Iseconds)" '.lastCheck = $time' "$STATE_FILE" > "${STATE_FILE}.tmp"
mv "${STATE_FILE}.tmp" "$STATE_FILE"

echo ""
echo "Missed job check complete."

