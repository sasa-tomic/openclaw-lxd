#!/bin/bash
# Twitter Morning Research Script
# Runs once daily to find and draft tweet opportunities
# Usage: ./twitter-morning.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEMORY_DIR="/home/openclaw/clawd/memory"
NOTES_DIR="/projects/Notes"
STATE_FILE="$MEMORY_DIR/twitter-state.json"
TEASER_FILE="$NOTES_DIR/Pickle/twitter-teasers.md"
TWITTER_WORKFLOW="$HOME/clawd/docs/TwitterWorkflow.md"

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

# Get last research run time
LAST_RUN=$(jq -r '.lastResearchRun' "$STATE_FILE" 2>/dev/null || echo "null")
CURRENT_TIME=$(date +%s)

# Duplicate detection helpers (skip links/titles we've already posted recently)
# We treat any match in recentPosts[].link as a hard duplicate.
# We also do a cheap case-insensitive substring match against recentPosts[].text.

normalize_url() {
    # Strip query/fragment + trailing slash for stable matching.
    # (HN sometimes includes tracking params or inconsistent trailing slashes.)
    echo "$1" | sed -E 's/[?#].*$//; s#/$##'
}

ALREADY_POSTED_LINKS=$(jq -r '.recentPosts[]? | .link? // empty' "$STATE_FILE" 2>/dev/null || true)
ALREADY_POSTED_LINKS_NORM=$(echo "$ALREADY_POSTED_LINKS" | sed -E 's/[?#].*$//; s#/$##')
ALREADY_POSTED_TEXT=$(jq -r '.recentPosts[]? | .text? // empty' "$STATE_FILE" 2>/dev/null || true)

already_posted_link() {
    local url="$1"
    if [ -z "$url" ]; then return 1; fi
    local norm
    norm=$(normalize_url "$url")
    echo "$ALREADY_POSTED_LINKS_NORM" | grep -Fqx "$norm"
}

already_posted_titleish() {
    local title="$1"
    if [ -z "$title" ]; then return 1; fi
    # Lowercase compare; substring match (cheap + good enough)
    local t
    t=$(echo "$title" | tr '[:upper:]' '[:lower:]')
    echo "$ALREADY_POSTED_TEXT" | tr '[:upper:]' '[:lower:]' | grep -Fq "$t"
}

already_posted_urlish() {
    local url="$1"
    if [ -z "$url" ]; then return 1; fi

    # If we didn't store the full URL in state, try matching a stable fragment
    # (repo name, slug) against recent post text.
    local frag
    frag=$(echo "$url" | sed -E 's#https?://##' | tr '[:upper:]' '[:lower:]')
    # Keep last 2 path segments when possible (e.g. github.com/org/repo)
    frag=$(echo "$frag" | awk -F/ '{if (NF>=3) print $(NF-1) "/" $NF; else print $NF}')

    if [ -n "$frag" ]; then
        echo "$ALREADY_POSTED_TEXT" | tr '[:upper:]' '[:lower:]' | grep -Fq "$frag" && return 0
        # Also try just the last segment (e.g. "kaytu")
        echo "$ALREADY_POSTED_TEXT" | tr '[:upper:]' '[:lower:]' | grep -Fq "$(echo "$frag" | awk -F/ '{print $NF}')" && return 0
    fi

    return 1
}

echo "=== DECENT CLOUD TWITTER - MORNING RESEARCH ==="
echo "Last research run: $LAST_RUN"

# Check if we should run (20+ hours since last run)
# Set FORCE=1 to run regardless (manual trigger).
if [ "${FORCE:-0}" != "1" ] && [ "$LAST_RUN" != "null" ]; then
    HOURS_SINCE=$(( (CURRENT_TIME - $(date -d "$LAST_RUN" +%s 2>/dev/null || echo "0")) / 3600 ))
    echo "Hours since last run: $HOURS_SINCE"

    if [ "$HOURS_SINCE" -lt 20 ]; then
        echo "Skipping (run <20 hours ago). Re-run with FORCE=1"
        exit 0
    fi
fi

echo "Running morning research..."

TWEET_OPTIONS=""
OPTION_COUNT=0

# 1. Check Hacker News for cloud/infra/pricing stories
# HARD REQUIREMENT: do not surface items we've already posted.
# Also: guarantee at least one *new* (not-yet-posted) entry if HN has any.

echo ""
echo "Searching Hacker News for cloud/pricing stories..."

# Query set tuned for Decent Cloud's voice: infra portability, open source, outages, pricing/egress.
HN_QUERIES=(
  "kubernetes clusters hetzner"
  "open-source local-first"
  "cloud outage locked out"
  "egress pricing"
  "decentralized cloud storage"
  "open source observability datadog alternative"
)

MAX_OPTIONS_FROM_HN=3
MIN_POINTS=50

CANDIDATES_TSV=$(mktemp)
# columns: points<TAB>title<TAB>url

for Q in "${HN_QUERIES[@]}"; do
    HN_URL="https://hn.algolia.com/api/v1/search?query=$(/usr/bin/python3 - <<PY
import urllib.parse
print(urllib.parse.quote("$Q"))
PY
)&tags=story&hitsPerPage=30"

    if ! HN_DATA=$(curl -s "$HN_URL" 2>&1); then
        echo "Failed to fetch HN data for query: $Q"
        continue
    fi

    HN_HIT_COUNT=$(echo "$HN_DATA" | jq -r '.hits | length')
    if [ -z "$HN_HIT_COUNT" ] || [ "$HN_HIT_COUNT" = "null" ]; then
        continue
    fi

    for ((i=0; i<HN_HIT_COUNT; i++)); do
        TITLE=$(echo "$HN_DATA" | jq -r ".hits[$i].title // empty")
        URL=$(echo "$HN_DATA"   | jq -r ".hits[$i].url // empty")
        POINTS=$(echo "$HN_DATA"| jq -r ".hits[$i].points // 0")

        # Skip items with no outbound URL (Ask HN/job posts/etc.)
        if [ -z "$TITLE" ] || [ -z "$URL" ]; then
            continue
        fi

        # Prefer items with some traction, but don't be too strict.
        if [ "$POINTS" -lt "$MIN_POINTS" ]; then
            continue
        fi

        # Conservative dedupe (canonical link + keyword match)
        if /usr/bin/python3 /projects/automations/twitter/dedupe_recent_posts.py --url "$URL" --title "$TITLE" >/tmp/twitter-dedupe-last.txt 2>&1; then
            echo "Skipping duplicate: $TITLE ($URL)"
            sed -n '1,1p' /tmp/twitter-dedupe-last.txt | sed 's/^/  /'
            continue
        fi

        printf "%s\t%s\t%s\n" "$POINTS" "$TITLE" "$URL" >> "$CANDIDATES_TSV"
    done

done

# If the MIN_POINTS filter yielded nothing, retry without the points threshold.
if [ ! -s "$CANDIDATES_TSV" ]; then
    echo "No candidates met MIN_POINTS=$MIN_POINTS; retrying without points threshold..."
    MIN_POINTS=0
    for Q in "${HN_QUERIES[@]}"; do
        HN_URL="https://hn.algolia.com/api/v1/search?query=$(/usr/bin/python3 - <<PY
import urllib.parse
print(urllib.parse.quote("$Q"))
PY
)&tags=story&hitsPerPage=30"

        if ! HN_DATA=$(curl -s "$HN_URL" 2>&1); then
            continue
        fi

        HN_HIT_COUNT=$(echo "$HN_DATA" | jq -r '.hits | length')
        if [ -z "$HN_HIT_COUNT" ] || [ "$HN_HIT_COUNT" = "null" ]; then
            continue
        fi

        for ((i=0; i<HN_HIT_COUNT; i++)); do
            TITLE=$(echo "$HN_DATA" | jq -r ".hits[$i].title // empty")
            URL=$(echo "$HN_DATA"   | jq -r ".hits[$i].url // empty")
            POINTS=$(echo "$HN_DATA"| jq -r ".hits[$i].points // 0")
            if [ -z "$TITLE" ] || [ -z "$URL" ]; then
                continue
            fi
            if /usr/bin/python3 /projects/automations/twitter/dedupe_recent_posts.py --url "$URL" --title "$TITLE" >/tmp/twitter-dedupe-last.txt 2>&1; then
                continue
            fi
            printf "%s\t%s\t%s\n" "$POINTS" "$TITLE" "$URL" >> "$CANDIDATES_TSV"
        done
    done
fi

# Sort by points desc, keep top N
if [ -s "$CANDIDATES_TSV" ]; then
    mapfile -t TOP_LINES < <(sort -nr "$CANDIDATES_TSV" | awk '!seen[$3]++' | head -n "$MAX_OPTIONS_FROM_HN")
else
    TOP_LINES=()
fi

for LINE in "${TOP_LINES[@]}"; do
    POINTS=$(echo "$LINE" | cut -f1)
    TITLE=$(echo "$LINE" | cut -f2)
    URL=$(echo "$LINE" | cut -f3)

    OPTION_COUNT=$((OPTION_COUNT + 1))
    TWEET_OPTIONS="$TWEET_OPTIONS

[OPTION $OPTION_COUNT - VALUE DROP]
**Title:** $TITLE
**URL:** $URL
**Points:** $POINTS

**Suggested draft:**
$TITLE

$URL

---
"
done

rm -f "$CANDIDATES_TSV"

# 2. Check Decent Cloud repo for recent activity
echo ""
echo "Checking Decent Cloud repo for recent activity..."
if [ -d "/projects/decent-cloud" ]; then
    cd /projects/decent-cloud

    # Get commits from last 24 hours
    if RECENT_COMMITS=$(git log --oneline --since="24 hours ago" 2>&1); then
        # git log prints nothing when there are no commits; avoid wc -l counting an empty line.
        if [ -n "${RECENT_COMMITS//[[:space:]]/}" ]; then
            COMMIT_COUNT=$(printf "%s\n" "$RECENT_COMMITS" | sed '/^$/d' | wc -l)
            echo "Found $COMMIT_COUNT commits in last 24 hours:"
            echo "$RECENT_COMMITS" | head -5

            # Draft a teaser tweet
            LATEST=$(printf "%s\n" "$RECENT_COMMITS" | sed '/^$/d' | head -1)
            OPTION_COUNT=$((OPTION_COUNT + 1))
            TWEET_OPTIONS="$TWEET_OPTIONS

[OPTION $OPTION_COUNT - DEV UPDATE]
**Recent activity:** $COMMIT_COUNT commits in last 24 hours
**Latest:** $LATEST

**Suggested draft:**
Ship, ship, ship 🚢

Another $COMMIT_COUNT commits shipped in the last 24 hours. Building the future of peer-to-peer cloud.

#DecentCloud #P2P #CloudComputing

---
"
        else
            echo "No commits in last 24 hours"
        fi
    else
        echo "Could not check git log"
    fi
fi

# 3. Check teaser bank
echo ""
echo "Checking teaser bank..."
if [ -f "$TEASER_FILE" ]; then
    # Get unused teasers (before "## Posted" section)
    UNUSED_TEASER=$(awk '/## Posted/{exit} /^- / && !/^$/ {print; exit}' "$TEASER_FILE")

    if [ -n "$UNUSED_TEASER" ]; then
        OPTION_COUNT=$((OPTION_COUNT + 1))
        TWEET_OPTIONS="$TWEET_OPTIONS

[OPTION $OPTION_COUNT - TEASER]
**From bank:** $UNUSED_TEASER

(Adapt as needed)

---
"
    else
        echo "No unused teasers found"
    fi
else
    echo "Teaser file not found: $TEASER_FILE"
fi

# Update state
jq --arg date "$(date -Iseconds)" '.lastResearchRun = $date' "$STATE_FILE" > "${STATE_FILE}.tmp"
mv "${STATE_FILE}.tmp" "$STATE_FILE"

# Report results
if [ "$OPTION_COUNT" -gt 0 ]; then
    echo ""
    echo "=== TWEET OPTIONS READY ==="
    echo "$TWEET_OPTIONS"

    # Write to file for cron handler
    echo "TWITTER_MORNING:$TWEET_OPTIONS" > /tmp/twitter-morning-results.txt
else
    echo ""
    echo "No tweet options found today."
fi

echo ""
echo "Morning research complete."
