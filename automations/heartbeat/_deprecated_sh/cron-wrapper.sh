#!/bin/bash
# Cron Wrapper Script
# Runs heartbeat automation scripts and sends results to Telegram
# Usage: ./cron-wrapper.sh <script_name>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="$1"
TEMP_RESULTS="/tmp/${SCRIPT_NAME}-results.txt"

echo "=== CRON WRAPPER: $SCRIPT_NAME ==="

# Run the script
if "$SCRIPT_DIR/${SCRIPT_NAME}.sh"; then
    echo "Script completed successfully"
else
    echo "Script failed with exit code $?"
fi

# Check if there are results to send
if [ -f "$TEMP_RESULTS" ]; then
    CONTENT=$(cat "$TEMP_RESULTS")

    # Parse the content type
    if [[ "$CONTENT" =~ ^OBSERVATIONS:(.*)$ ]]; then
        MESSAGE="📝 **Obsidian Note Review**
${BASH_REMATCH[1]}"
    elif [[ "$CONTENT" =~ ^IMPORTANT_EMAILS:(.*)$ ]]; then
        MESSAGE="📧 **Important Emails**
${BASH_REMATCH[1]}"
    elif [[ "$CONTENT" =~ ^MAINTENANCE:(.*)$ ]]; then
        MESSAGE="🗂️ **Obsidian Maintenance**
${BASH_REMATCH[1]}"
    elif [[ "$CONTENT" =~ ^TWITTER_MORNING:(.*)$ ]]; then
        MESSAGE="🐦 **Twitter Morning Research**
${BASH_REMATCH[1]}

Ready to post? Approve with A/B/C or edit."
    elif [[ "$CONTENT" =~ ^TWITTER_ENGAGEMENT:(.*)$ ]]; then
        MESSAGE="💬 **Twitter Engagement Check**
${BASH_REMATCH[1]}"
    else
        MESSAGE="**$SCRIPT_NAME**
$CONTENT"
    fi

    # Send to Telegram via OpenClaw message tool
    # Telegram hard limit is 4096 chars; keep a buffer and split if needed.
    TELEGRAM_TARGET="${TELEGRAM_TARGET:-5996479639}"

    MAX_CHUNK=3500
    export MESSAGE MAX_CHUNK
    if [ "${#MESSAGE}" -le "$MAX_CHUNK" ]; then
        openclaw message send --channel telegram --target "$TELEGRAM_TARGET" --message "$MESSAGE"
    else
        # Split into chunks with a small header so long reports don't fail the whole job.
        # Emit NUL-separated chunks and send each one.
        while IFS= read -r -d '' CHUNK; do
            openclaw message send --channel telegram --target "$TELEGRAM_TARGET" --message "$CHUNK"
            sleep 1
        done < <(python3 - << 'PY'
import os
msg = os.environ["MESSAGE"]
max_chunk = int(os.environ.get("MAX_CHUNK", "3500"))
chunks = [msg[i:i+max_chunk] for i in range(0, len(msg), max_chunk)]
for idx, chunk in enumerate(chunks, 1):
    prefix = f"(part {idx}/{len(chunks)})\n"
    out = prefix + chunk
    os.write(1, out.encode("utf-8") + b"\0")
PY
)
    fi

    # Clean up
    rm -f "$TEMP_RESULTS"
else
    echo "No results to send"
fi

echo "Cron wrapper complete."
