#!/bin/bash
# Email Check Script
# Runs every 10-30 minutes to check for important emails
# Usage: ./email-check.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEMORY_DIR="/home/openclaw/clawd/memory"
STATE_FILE="$MEMORY_DIR/email-check-state.json"
HIMALAYA="/home/openclaw/.local/bin/himalaya"

# Ensure state file exists
if [ ! -f "$STATE_FILE" ]; then
    echo '{"lastEmailId": null, "lastCheck": null}' > "$STATE_FILE"
fi

# Get last checked email ID
LAST_EMAIL_ID=$(jq -r '.lastEmailId' "$STATE_FILE" 2>/dev/null || echo "null")

echo "=== EMAIL CHECK ==="
echo "Last checked ID: $LAST_EMAIL_ID"

# List recent emails (last 20)
if ! EMAILS=$($HIMALAYA envelope list -s 20 2>&1); then
    echo "Error listing emails: $EMAILS"
    exit 1
fi

# Process emails
IMPORTANT_EMAILS=""
CURRENT_EMAIL_ID=""

while IFS= read -r line; do
    # Parse email line (format: ID | Date | From | Subject)
    if [[ "$line" =~ ^([0-9]+)[[:space:]]*\|[[:space:]]*([^|]+)[[:space:]]*\|[[:space:]]*([^|]+)[[:space:]]*\|[[:space:]]*(.*)$ ]]; then
        EMAIL_ID="${BASH_REMATCH[1]}"
        EMAIL_DATE="${BASH_REMATCH[2]}"
        EMAIL_FROM="${BASH_REMATCH[3]}"
        EMAIL_SUBJECT="${BASH_REMATCH[4]}"

        # Update current ID
        if [ -z "$CURRENT_EMAIL_ID" ]; then
            CURRENT_EMAIL_ID="$EMAIL_ID"
        fi

        # Skip if we've seen this email
        if [ "$EMAIL_ID" = "$LAST_EMAIL_ID" ]; then
            break
        fi

        # Skip marketing/newsletters (simple heuristics)
        if [[ "$EMAIL_FROM" =~ (noreply|newsletter|notifications|update|news|promo|marketing|offers|deals) ]] \
           || [[ "$EMAIL_SUBJECT" =~ (Your|weekly|digest|update|newsletter|promo|offer|deal|%|€|\$) ]]; then
            echo "Skipping: $EMAIL_FROM - $EMAIL_SUBJECT (marketing)"
            continue
        fi

        # Flag as important
        echo "📧 Important: $EMAIL_FROM - $EMAIL_SUBJECT"

        # Try to read the email for more context
        if BODY=$($HIMALAYA message read "$EMAIL_ID" 2>/dev/null | head -20); then
            IMPORTANT_EMAILS="$IMPORTANT_EMAILS

**From:** $EMAIL_FROM
**Subject:** $EMAIL_SUBJECT
**ID:** $EMAIL_ID

$BODY

---
"
        else
            IMPORTANT_EMAILS="$IMPORTANT_EMAILS

**From:** $EMAIL_FROM
**Subject:** $EMAIL_SUBJECT
**ID:** $EMAIL_ID

(Could not read email body)

---
"
        fi
    fi
done <<< "$EMAILS"

# Update state
if [ -n "$CURRENT_EMAIL_ID" ]; then
    jq --arg id "$CURRENT_EMAIL_ID" --arg date "$(date -Iseconds)" \
       '.lastEmailId = $id | .lastCheck = $date' "$STATE_FILE" > "${STATE_FILE}.tmp"
    mv "${STATE_FILE}.tmp" "$STATE_FILE"
fi

# Report important emails
if [ -n "$IMPORTANT_EMAILS" ]; then
    echo ""
    echo "IMPORTANT EMAILS FOUND:"
    echo "$IMPORTANT_EMAILS"

    # Write to file for cron handler
    echo "IMPORTANT_EMAILS:$IMPORTANT_EMAILS" > /tmp/email-check-results.txt
else
    echo "No important emails since last check."
fi
