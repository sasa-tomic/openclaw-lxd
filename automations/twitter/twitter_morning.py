#!/usr/bin/env python3
"""Twitter Morning Research Script

Runs once daily to find and draft tweet opportunities.

Output:
- kv_state key `twitter:morning_research` — cache read by post_original_content.py
- /tmp/twitter-morning-results.txt       — human-readable summary for review
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from urllib.request import urlopen
from urllib.parse import quote
from pathlib import Path

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent.parent))

from db import get_conn, kv_get_json, kv_set_json

MEMORY_DIR = Path("/home/openclaw/clawd/memory")
RESULTS_FILE = Path("/tmp/twitter-morning-results.txt")
NOTES_DIR = Path("/projects/Notes")
TEASER_FILE = NOTES_DIR / "Pickle/twitter-teasers.md"
DEDUPE_SCRIPT = Path("/projects/automations/twitter/dedupe_recent_posts.py")

KV_MORNING_STATE = "twitter:morning_state"
KV_MORNING_RESEARCH = "twitter:morning_research"

HN_QUERIES = [
    "kubernetes clusters hetzner",
    "open-source local-first",
    "cloud outage locked out",
    "egress pricing",
    "decentralized cloud storage",
    "open source observability datadog alternative",
]

DEFAULT_STATE = {
    "lastResearchRun": None,
    "lastPost": None,
    "postsToday": 0,
    "pendingApproval": [],
    "engaged": [],
    "pending": [],
    "recentPosts": [],
}


def fetch_hn(query: str, min_points: int = 0) -> list[dict]:
    """Fetch Hacker News stories for a query."""
    encoded = quote(query)
    url = f"https://hn.algolia.com/api/v1/search?query={encoded}&tags=story&hitsPerPage=30"

    try:
        with urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"Failed to fetch HN for '{query}': {e}")
        return []

    results = []
    for hit in data.get("hits", []):
        title = hit.get("title", "")
        url = hit.get("url", "")
        points = hit.get("points", 0)

        if not title or not url:
            continue
        if points < min_points:
            continue

        results.append({"title": title, "url": url, "points": points})

    return results


def is_duplicate(url: str, title: str) -> bool:
    """Check if URL/title was already posted."""
    if not DEDUPE_SCRIPT.exists():
        return False

    try:
        result = subprocess.run(
            [sys.executable, str(DEDUPE_SCRIPT), "--url", url, "--title", title],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def main():
    print("=== DECENT CLOUD TWITTER - MORNING RESEARCH ===")

    with get_conn() as conn:
        state = kv_get_json(conn, KV_MORNING_STATE, DEFAULT_STATE)
        if not isinstance(state, dict):
            state = dict(DEFAULT_STATE)
    force = os.environ.get("FORCE", "0") == "1"

    # Check if we should run
    last_run = state.get("lastResearchRun")
    if last_run and not force:
        try:
            last_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            hours_since = (
                datetime.now(last_dt.tzinfo) - last_dt
            ).total_seconds() / 3600
            print(f"Hours since last run: {hours_since:.1f}")

            if hours_since < 20:
                print("Skipping (run <20 hours ago). Re-run with FORCE=1")
                return 0
        except (ValueError, TypeError) as e:
            logger.debug(f"Could not parse timestamp '{last_run}': {e}")

    print("Running morning research...")

    tweet_options = []
    hn_stories = []
    dev_activity = None

    # 1. Hacker News search
    print("\nSearching Hacker News...")
    candidates = []

    for query in HN_QUERIES:
        hits = fetch_hn(query, min_points=50)
        for hit in hits:
            if not is_duplicate(hit["url"], hit["title"]):
                candidates.append(hit)

    # If no candidates with 50+ points, try without threshold
    if not candidates:
        print("No candidates with 50+ points, retrying without threshold...")
        for query in HN_QUERIES:
            hits = fetch_hn(query, min_points=0)
            for hit in hits:
                if not is_duplicate(hit["url"], hit["title"]):
                    candidates.append(hit)

    # Sort by points, dedupe by URL, take top 3
    seen_urls = set()
    for hit in sorted(candidates, key=lambda x: -x["points"]):
        if hit["url"] not in seen_urls and len(tweet_options) < 3:
            seen_urls.add(hit["url"])
            opt = {
                "type": "VALUE DROP",
                "title": hit["title"],
                "url": hit["url"],
                "points": hit["points"],
            }
            tweet_options.append(opt)
            hn_stories.append({"title": hit["title"], "points": hit["points"]})

    # 2. Check Decent Cloud repo
    print("\nChecking Decent Cloud repo...")
    dc_repo = Path("/projects/decent-cloud")
    if dc_repo.exists():
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "--since=24 hours ago"],
                cwd=dc_repo,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                commits = [l for l in result.stdout.strip().split("\n") if l]
                if commits:
                    opt = {
                        "type": "DEV UPDATE",
                        "commit_count": len(commits),
                        "latest": commits[0] if commits else "",
                    }
                    tweet_options.append(opt)
                    dev_activity = (
                        f"{len(commits)} commits in last 24h (latest: {commits[0]})"
                    )
        except Exception as e:
            print(f"Could not check git log: {e}")

    # 3. Check teaser bank
    print("\nChecking teaser bank...")
    if TEASER_FILE.exists():
        try:
            content = TEASER_FILE.read_text()
            # Find first unused teaser (before ## Posted)
            if "## Posted" in content:
                unused_section = content.split("## Posted")[0]
            else:
                unused_section = content

            for line in unused_section.split("\n"):
                if line.strip().startswith("- ") and not line.strip().startswith(
                    "- [x]"
                ):
                    tweet_options.append(
                        {
                            "type": "TEASER",
                            "content": line.strip()[2:],
                        }
                    )
                    break
        except Exception as e:
            print(f"Could not read teaser file: {e}")

    # Update state
    state["lastResearchRun"] = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        kv_set_json(conn, KV_MORNING_STATE, state)

    # Write research cache for post_original_content.py to consume
    research_cache = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hnStories": hn_stories,
        "devActivity": dev_activity,
    }
    with get_conn() as conn:
        kv_set_json(conn, KV_MORNING_RESEARCH, research_cache)
    print(
        f"  Wrote research cache (DB): {len(hn_stories)} HN stories, dev={dev_activity is not None}"
    )

    # Report results
    if tweet_options:
        print("\n=== TWEET OPTIONS READY ===")
        print("\u26a0\ufe0f  PHASE 1 RULES: NO links, NO hashtags, NO product mentions")

        output_parts = []
        for i, opt in enumerate(tweet_options, 1):
            if opt["type"] == "VALUE DROP":
                output_parts.append(f"""
[OPTION {i} - VALUE DROP]
**Title:** {opt["title"]}
**Source URL (for context, DO NOT post):** {opt["url"]}
**Points:** {opt["points"]}

**Suggested approach:** Write a short opinionated take inspired by this story. NO links, NO hashtags. Founder voice.

---
""")
            elif opt["type"] == "DEV UPDATE":
                output_parts.append(f"""
[OPTION {i} - DEV UPDATE]
**Recent activity:** {opt["commit_count"]} commits in last 24 hours
**Latest:** {opt["latest"]}

**Suggested draft:**
shipped {opt["commit_count"]} commits today. the best marketing is building something people actually need.

---
""")
            elif opt["type"] == "TEASER":
                output_parts.append(f"""
[OPTION {i} - TEASER]
**From bank:** {opt["content"]}

(Adapt as needed)

---
""")

        output = "\n".join(output_parts)
        print(output)
        RESULTS_FILE.write_text("TWITTER_MORNING:" + output)
    else:
        print("\nNo tweet options found today.")

    print("\nMorning research complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
