#!/usr/bin/env python3
"""Post original Twitter content for @DecentCloud_org.

Strategy: /projects/Notes/Pickle/Twitter/decent-cloud-twitter-plan.md

Runs daily to ensure consistent original content posting (1-2 tweets/day).

Phase 1 mode: founder voice takes only — no links, no product mentions, no hashtags.

Sources:
1. Morning research results (HN stories, curated links)
2. Dev updates (repo activity)
3. Insights/learnings (from memory, notes)

This complements engagement replies with original value-adding content.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/projects/automations/twitter")
from lib.llm_utils import call_llm_simple as call_llm, extract_json
from twitter_utils import (
    humanize,
    load_project_context,
    post_tweet,
    send_error_alert,
    utc_now,
    get_latest_own_tweet_id,
)
from db import (
    get_conn,
    count_posts_today,
    get_recent_posts,
    get_recent_engagements,
    insert_post,
)

# Phase 1 hard rules: zero product mentions in original posts
PRODUCT_MENTION_PATTERNS = [
    r"\bwe\s+(just|recently|today|now)?\s*(shipped|launched|built|released|deployed|published)\b",
    r"\bwe\s+just\b",
    r"\bdecent\s*cloud\b",
    r"\bour\s+(platform|product|marketplace|service|tool|app)\b",
    r"\bcheck\s+out\b",
    r"\bsign\s+up\b",
    r"\bwaitlist\b",
    r"\bearly\s+access\b",
    r"\bwe\s+shipped\b",
    r"\bwe\s+launched\b",
    r"\bfull\s+visibility\s+into\b",
    r"\bdeployment\s+recipe\b",
]


def has_product_mention(text: str) -> bool:
    for pattern in PRODUCT_MENTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


MORNING_RESEARCH_CACHE = Path("/tmp/twitter-morning-research.json")
QUEUE_PATH = Path("/home/openclaw/clawd/memory/twitter-content-queue.json")


def load_queue() -> list[dict]:
    """Load the content queue from disk."""
    if QUEUE_PATH.exists():
        try:
            return json.loads(QUEUE_PATH.read_text())
        except Exception:
            return []
    return []


def save_queue(queue: list[dict]) -> None:
    """Atomically save the content queue to disk."""
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(QUEUE_PATH)


def score_tweet(text: str) -> float:
    """Score a tweet draft with simple quality heuristics. Returns 0.0-1.0."""
    score = 0.5  # baseline
    # Specificity signals (numbers, comparisons)
    if re.search(r"\d", text):
        score += 0.15
    # Shorter is punchier
    if len(text) < 180:
        score += 0.1
    elif len(text) > 250:
        score -= 0.1
    # Controversial/opinion markers
    if any(
        w in text.lower()
        for w in [
            "nobody",
            "everyone",
            "myth",
            "scam",
            "lie",
            "truth",
            "wrong",
            "actually",
        ]
    ):
        score += 0.1
    # Questions score lower (less viral)
    if "?" in text:
        score -= 0.1
    return min(1.0, max(0.0, score))


def load_morning_research() -> dict | None:
    """Load cached morning research results if available."""
    if not MORNING_RESEARCH_CACHE.exists():
        return None

    try:
        return json.loads(MORNING_RESEARCH_CACHE.read_text())
    except Exception:
        return None


def get_recent_commits(n: int = 5) -> list[str]:
    """Fetch recent commits from decent-cloud repo."""
    try:
        r = subprocess.run(
            ["git", "-C", "/projects/decent-cloud", "log", f"--oneline", f"-{n}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip().split("\n")
    except Exception:
        pass
    return []


def _get_engagement_themes(recent_engagements: list[dict], n: int = 10) -> list[str]:
    """Extract recurring themes from recent engagements to inform content direction."""
    recent = recent_engagements[-n:]
    terms = [e.get("search_term", "") for e in recent if e.get("search_term")]
    return list(dict.fromkeys(terms))  # deduplicated, order-preserving


def draft_batch(conn) -> list[dict]:
    """Use LLM to draft a batch of 3-5 tweets. Returns list of queue entries."""

    research = load_morning_research()
    recent_posts_db = get_recent_posts(conn, days=14, limit=12)
    recent_engagements_db = get_recent_engagements(conn, hours=168, limit=15)

    # Extended history: last 12 posts for better dedup and thematic continuity
    recent_posts = [p.get("text", "")[:200] for p in recent_posts_db]
    recent_commits = get_recent_commits(6)
    engagement_themes = _get_engagement_themes(recent_engagements_db, n=15)

    project_context = load_project_context()

    # Build morning research context (HN trending topics + dev activity)
    research_text = ""
    if research:
        parts = []
        if research.get("hnStories"):
            for s in research["hnStories"][:3]:
                parts.append(f'- HN trending: "{s["title"]}" ({s["points"]} pts)')
        if research.get("devActivity"):
            parts.append(f"- Dev activity: {research['devActivity']}")
        if parts:
            research_text = "\n".join(parts)

    # Build dev commits context (fallback if morning research didn't run)
    commits_text = ""
    if not research_text and recent_commits:
        commits_text = "\n".join(f"- {c}" for c in recent_commits[:4])

    prompt = f"""Generate 4 short original takes for a technical Twitter account.

# Project & Strategy Context
{project_context}

# Your Task
Draft 4 tweets. Each must be a DIFFERENT angle. Strictly follow the phase rules above.

## Recent posts — AVOID repeating these angles (last 12):
{json.dumps(recent_posts, indent=2)}

## Audience engagement signal — active topics recently:
{json.dumps(engagement_themes, indent=2) if engagement_themes else "  (no data yet)"}
Use as inspiration for fresh angles only — don't repeat same framing.

{f"## Trending today (inspiration only — NO links in tweets):{chr(10)}{research_text}" if research_text else ""}
{f"## Recent dev activity (for inspiration):{chr(10)}{commits_text}" if commits_text else ""}

## Voice & Style (see STRATEGY.md for full reference with examples)
- Observational, not imperative — describe what happens, don't instruct the reader
- Short sentences, each doing one job — but enough detail to be genuinely useful
- Specific details over generic takes: numbers, timeframes, concrete mechanics
- Peer voice — like explaining something to a knowledgeable friend, not a blog post
- NOT: "Make sure you read Stripe's reserve policy" / "Switch to an authenticator app"
- YES: "Stripe can hold 10% of your revenue for 6 months. Rapid growth often triggers it."

## Constraints
- 1-2 sentences max per tweet
- Under 260 characters each
- Mostly lowercase — start lowercase unless it's a proper noun (AWS, GCP, etc.)
- Each tweet distinctly different in topic and angle

Output as JSON array: ["tweet1", "tweet2", "tweet3", "tweet4"]
Output ONLY the JSON array. No explanation, no markdown wrapping."""

    try:
        raw = call_llm(prompt, timeout=120)
        if not raw:
            print("LLM returned nothing for batch draft", file=sys.stderr)
            return []

        # Try to extract JSON array from the response
        raw = raw.strip()
        # Strip markdown fences
        if raw.startswith("```json"):
            raw = raw[7:]
        if raw.startswith("```"):
            raw = raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

        # Try direct parse as array
        tweets = None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                tweets = parsed
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: try extract_json (finds objects), then look for array
        if tweets is None:
            # Try to find a JSON array in the text
            bracket_depth = 0
            start = None
            for i, ch in enumerate(raw):
                if ch == "[":
                    if bracket_depth == 0:
                        start = i
                    bracket_depth += 1
                elif ch == "]":
                    bracket_depth -= 1
                    if bracket_depth == 0 and start is not None:
                        candidate = raw[start : i + 1]
                        try:
                            parsed = json.loads(candidate)
                            if isinstance(parsed, list):
                                tweets = parsed
                                break
                        except (json.JSONDecodeError, ValueError):
                            start = None
                            continue

        # Fallback: parse as numbered/line-separated plain text
        if not tweets or not isinstance(tweets, list):
            lines = raw.split("\n")
            text_tweets = []
            for line in lines:
                line = line.strip()
                # Strip numbering like "1.", "1)", "1:", "- "
                line = re.sub(r"^[\d]+[.):\-]\s*", "", line).strip()
                line = re.sub(r"^[-•]\s*", "", line).strip()
                # Strip surrounding quotes
                line = line.strip('"').strip("'").strip("`")
                if line and 20 <= len(line) <= 280:
                    text_tweets.append(line)
            if text_tweets:
                tweets = text_tweets
                print(
                    f"Parsed {len(tweets)} tweets from plain text response", flush=True
                )
            else:
                print(f"Failed to parse batch draft: {raw[:200]}", file=sys.stderr)
                return []

        now = datetime.now(timezone.utc).isoformat()
        entries = []
        for text in tweets:
            if not isinstance(text, str):
                continue
            text = text.strip().strip('"').strip("'")
            # Strip LLM categorization prefixes like "(Observation/DR)", "(Technical insight)", etc.
            text = re.sub(r"^\([^)]{3,30}\)\s*", "", text).strip()
            if not text or len(text) > 280 or len(text) < 20:
                print(f"Batch draft rejected: length={len(text)}", file=sys.stderr)
                continue
            if has_product_mention(text):
                print(
                    f"Batch draft rejected: Phase 1 violation: {text[:80]}",
                    file=sys.stderr,
                )
                continue
            entries.append(
                {
                    "text": text,
                    "draftedAt": now,
                    "score": score_tweet(text),
                    "posted": False,
                }
            )

        print(
            f"Batch drafted {len(entries)} valid tweets from {len(tweets)} candidates",
            flush=True,
        )
        return entries

    except Exception as e:
        print(f"LLM batch draft failed: {e}", file=sys.stderr)
        return []


def draft_single(conn) -> dict | None:
    """Draft a single tweet using the proven single-tweet prompt. Fallback for batch failures."""
    recent_posts_db = get_recent_posts(conn, days=14, limit=12)
    recent_engagements_db = get_recent_engagements(conn, hours=168, limit=10)

    recent_posts = [p.get("text", "")[:200] for p in recent_posts_db]
    engagement_themes = _get_engagement_themes(recent_engagements_db, n=10)
    research = load_morning_research()

    project_context = load_project_context()

    research_text = ""
    if research:
        parts = []
        if research.get("hnStories"):
            for s in research["hnStories"][:2]:
                parts.append(f'- HN trending: "{s["title"]}" ({s["points"]} pts)')
        if research.get("devActivity"):
            parts.append(f"- Dev activity: {research['devActivity']}")
        if parts:
            research_text = "\n".join(parts)

    prompt = f"""You are drafting a short original take for a technical Twitter account.

# Project & Strategy Context
{project_context}

# Your Task
Draft ONE tweet. Strictly follow the phase rules above.

## Recent posts — avoid repeating these angles (last 12):
{json.dumps(recent_posts, indent=2)}

## Audience engagement signal — active topics recently:
{json.dumps(engagement_themes, indent=2) if engagement_themes else "  (no data yet)"}
Use as inspiration for fresh angles, but don't repeat the same framing.

{f"## Trending today (inspiration only — NO links in tweets):{chr(10)}{research_text}" if research_text else ""}

## Voice & Style (see STRATEGY.md for full reference with examples)
- Observational, not imperative — describe what happens, don't instruct the reader
- Short sentences, each doing one job — but enough detail to be genuinely useful
- Specific details: numbers, timeframes, concrete mechanics over generic takes
- Peer voice — like explaining something to a knowledgeable friend, not a blog post

## Constraints
- 1-2 sentences max
- Under 260 characters
- Mostly lowercase — start lowercase unless it's a proper noun (AWS, GCP, etc.)

Output ONLY the tweet text. No quotes, no JSON, no markdown. Start with lowercase.

Tweet:"""

    try:
        text = call_llm(prompt, timeout=120)
        if not text:
            return None

        text = text.strip().strip('"').strip("'").strip("`")
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        # Strip categorization prefixes
        text = re.sub(r"^\([^)]{3,30}\)\s*", "", text).strip()
        # If multi-line, take the best substantive line (LLM thinking before answer)
        if "\n" in text:
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            # Find the best candidate line (longest line that looks like a tweet)
            candidates = [
                l
                for l in lines
                if 20 <= len(l) <= 280
                and not l.startswith("*")
                and not l.startswith("#")
            ]
            if candidates:
                text = max(candidates, key=len)
            elif lines:
                text = lines[-1]

        if not text or len(text) > 280 or len(text) < 20:
            return None
        if has_product_mention(text):
            return None

        return {
            "text": text,
            "draftedAt": datetime.now(timezone.utc).isoformat(),
            "score": score_tweet(text),
            "posted": False,
        }
    except Exception as e:
        print(f"Single draft failed: {e}", file=sys.stderr)
        return None


def main() -> int:
    print("=== TWITTER ORIGINAL CONTENT POSTER ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    try:
        with get_conn() as conn:
            # Check if we've already posted enough today
            count = count_posts_today(conn)
            print(f"Posts today: {count}/3", flush=True)

            if count >= 3:
                print("Already posted 2 original tweets today, skipping")
                return 0

            # Load queue and clean up old posted entries (>7 days)
            queue = load_queue()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            queue = [
                entry
                for entry in queue
                if not (entry.get("posted") and entry.get("postedAt", "") < cutoff)
            ]
            unposted = [entry for entry in queue if not entry.get("posted")]
            print(f"Queue: {len(queue)} total, {len(unposted)} unposted", flush=True)

            # If <2 unposted entries, draft a batch
            if len(unposted) < 2:
                print("Queue low, drafting batch...", flush=True)
                new_entries = draft_batch(conn)
                if new_entries:
                    queue.extend(new_entries)
                    save_queue(queue)
                    unposted = [entry for entry in queue if not entry.get("posted")]
                    print(f"Queue after batch: {len(unposted)} unposted", flush=True)
                else:
                    # Fallback: draft a single tweet (more reliable with GLM-5)
                    print("Batch failed, falling back to single draft...", flush=True)
                    single = draft_single(conn)
                    if single:
                        queue.append(single)
                        save_queue(queue)
                        unposted = [entry for entry in queue if not entry.get("posted")]
                        print(
                            f"Queue after single draft: {len(unposted)} unposted",
                            flush=True,
                        )
                    else:
                        print("Single draft also failed", flush=True)

            if not unposted:
                send_error_alert("Content queue empty and all drafting failed")
                print("No tweets available in queue")
                return 1

            # Pick the highest-scored unposted entry
            best = max(unposted, key=lambda e: e.get("score", 0))
            draft = best["text"]
            print(
                f"Selected (score={best.get('score', 0):.2f}): {draft[:80]}...", flush=True
            )

            # Humanize (mandatory per strategy doc)
            try:
                draft = humanize(draft)
                print(f"Humanized: {draft[:80]}...", flush=True)
            except Exception as e:
                print(f"Humanize error: {e}, proceeding with original draft", flush=True)

            # Post
            print("Posting via browser CDP...", flush=True)
            if not post_tweet(draft):
                send_error_alert(f"Failed to post via CDP\n\nDraft was:\n{draft}")
                print("Failed to post")
                return 1

            print("Posted successfully", flush=True)

            # Mark as posted in queue
            best["posted"] = True
            best["postedAt"] = datetime.now(timezone.utc).isoformat()
            save_queue(queue)

            # Get the tweet ID from our profile for tracking
            from twitter_utils import jitter_sleep
            jitter_sleep(6, 12)
            tweet_id = get_latest_own_tweet_id("DecentCloud_org")

            # Insert into posts table
            insert_post(
                conn,
                tweet_id=tweet_id or f"unknown-{utc_now()}",
                type="post",
                text=draft,
            )
            print(f"Logged post to DB (tweet_id={tweet_id})", flush=True)

        return 0

    except Exception as e:
        send_error_alert(f"Unexpected error: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
