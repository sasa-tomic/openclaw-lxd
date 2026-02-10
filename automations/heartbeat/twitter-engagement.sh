#!/bin/bash
# Twitter Engagement Script
# Runs 2-3x daily to find and engage with relevant tweets
# Usage: ./twitter-engagement.sh

set -euo pipefail

# Make sure user-installed CLIs are available under systemd
export PATH="$HOME/.local/bin:$PATH"

# Ensure bird has credentials even under systemd (no interactive shell rc files)
if [ -f "$HOME/.config/bird/secrets.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$HOME/.config/bird/secrets.env"
  set +a
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEMORY_DIR="/home/openclaw/clawd/memory"
STATE_FILE="$MEMORY_DIR/twitter-state.json"
TWITTER_SCRIPTS="$HOME/.local/bin"

echo "=== DECENT CLOUD TWITTER - ENGAGEMENT ==="

# Ensure state file exists
if [ ! -f "$STATE_FILE" ]; then
    cat > "$STATE_FILE" << 'EOF'
{
  "lastResearchRun": null,
  "lastPost": null,
  "postsToday": 0,
  "pendingApproval": [],
  "engaged": [],
  "pending": []
}
EOF
fi

# Search terms for engagement opportunities
SEARCH_TERMS=(
    "aws bill"
    "egress cost"
    "cloud expensive"
    "aws outage"
    "gpu shortage"
    "cloud computing"
    "aws pricing"
)

echo "Searching for engagement opportunities..."

# Use twitter-research.py if available
if [ -f "$TWITTER_SCRIPTS/twitter-research.py" ]; then
    echo "Using twitter-research.py..."

    # Run search with multiple terms
    if RESULTS=$("$TWITTER_SCRIPTS/twitter-research.py" --terms "${SEARCH_TERMS[@]}" --limit 5 2>&1); then
        echo "Search results:"
        echo "$RESULTS"
    else
        echo "Error running twitter-research.py: $RESULTS"
        RESULTS=""
    fi
else
    echo "twitter-research.py not found, skipping search"
    RESULTS=""
fi

# Check pending engagements
PENDING_COUNT=$(jq '.pending | length' "$STATE_FILE" 2>/dev/null || echo "0")

echo ""
echo "Pending engagements: $PENDING_COUNT"

if [ "$PENDING_COUNT" -gt 0 ]; then
    echo "Pending items:"
    jq -r '.pending[]' "$STATE_FILE" | head -3
else
    echo "No pending engagements."
fi

# Check for mentions
echo ""
echo "Checking for @DecentCloud_org mentions..."

if command -v bird &> /dev/null; then
    if MENTIONS=$(bird mentions -n 10 2>&1); then
        echo "Recent mentions:"
        echo "$MENTIONS"
    else
        echo "Could not fetch mentions: $MENTIONS"
    fi
else
    echo "bird CLI not available"
fi

# Check stats
if [ -f "$TWITTER_SCRIPTS/twitter-engagement.py" ]; then
    echo ""
    echo "Engagement stats:"
    "$TWITTER_SCRIPTS/twitter-engagement.py" stats || echo "Could not get stats"
fi

# Prepare results for reporting
ENGAGEMENT_REPORT="

**Pending engagements:** $PENDING_COUNT

"

if [ -n "$RESULTS" ]; then
    ENGAGEMENT_REPORT="$ENGAGEMENT_REPORT
**Search results found:**
$RESULTS

"
fi

# Write to file for cron handler
echo "TWITTER_ENGAGEMENT:$ENGAGEMENT_REPORT" > /tmp/twitter-engagement-results.txt

echo ""
echo "Engagement check complete."
