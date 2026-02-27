#!/usr/bin/env python3
"""Post weekly technical threads for @DecentCloud_org.

Strategy: "One deep technical thread/week. Becomes the discoverability engine."
Threads get 3-10x more impressions than single tweets.

Premium+ account: enables longer threads (6-10 tweets) and boosted reply ranking.

Flow:
1. Check if we've already posted a thread this week
2. LLM generates a 6-10 tweet thread as JSON
3. Humanize each tweet
4. Post first tweet via post_tweet()
5. Get the tweet ID from our profile
6. Chain subsequent tweets via post_reply() with jitter delays
7. Log thread to DB (posts table)

Runs weekly (Wednesday 15:00 UTC) via systemd timer.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from lib.llm_utils import call_llm_simple as call_llm, extract_json
from twitter_utils import (
    get_latest_own_tweet_id,
    humanize,
    jitter_sleep,
    load_project_context,
    post_reply,
    post_tweet,
    send_error_alert,
    utc_now,
)
from db import (
    get_conn,
    get_recent_posts,
    get_recent_engagements,
    insert_post,
    kv_get,
    kv_set,
)

OUR_USERNAME = "DecentCloud_org"

# Phase 1 hard rules
PRODUCT_MENTION_PATTERNS = [
    r"\bwe\s+(just|recently|today|now)?\s*(shipped|launched|built|released|deployed|published)\b",
    r"\bdecent\s*cloud\b",
    r"\bour\s+(platform|product|marketplace|service|tool|app)\b",
    r"\bcheck\s+out\b",
    r"\bsign\s+up\b",
    r"\bwaitlist\b",
    r"\bearly\s+access\b",
]

THREAD_TOPICS = [
    # --- P2P compute: why it keeps stalling ---
    "why p2p compute keeps failing at the same point (it's not technical)",
    "provider ghosting is structural: anonymous providers have zero skin in the game",
    "why Airbnb works and Akash doesn't (hint: accountability, not price)",
    "the real reason people don't trust p2p marketplaces",
    "there's no Yelp for cloud providers — and that's the actual problem",
    "the p2p ideology trap: decentralization became a substitute for trust infrastructure",

    # --- AWS & big cloud: rational behavior that looks irrational ---
    "why companies pick AWS even when it's 3x more expensive (it's not laziness)",
    "nobody got fired for buying AWS: how career risk drives infrastructure decisions",
    "AWS credits as lock-in: you think you chose AWS, you didn't",
    "why enterprise support fees are actually rational (you're buying escalation access)",
    "cross-AZ traffic: the hidden bill most teams find 3 months too late",
    "the managed services migration trap: it's not vendor lock-in, it's architectural dependency",

    # --- What's actually good vs bad about big cloud ---
    "what AWS genuinely does well (and what smaller providers can't replicate yet)",
    "cloud reliability theater: SLAs that sound good but pay nothing",
    "the FinOps industry exists because cloud pricing is deliberately opaque",

    # --- Why alternatives fail to break through ---
    "the compliance chicken-and-egg: why smaller providers can't break into enterprise",
    "why the race to the bottom on price kills cloud provider support",
    "the spot instance paradox: the best deal in cloud computing most teams can't use",
    "why 'just use a VPS' keeps failing (and what people get wrong about the comparison)",

    # --- Classic angles (keep for rotation) ---
    "cloud support is broken: what recourse do you actually have",
    "the hidden economics of cloud egress: why ingress is free and egress isn't",
    "the hidden economics of cloud egress: why providers make it free in, expensive out",
    "gpu compute economics: why the cloud is losing",
    "vendor lock-in is a feature, not a bug (for vendors)",
    "GPU compute: the $3/hr vs $0.30/hr gap that nobody talks about",
    "serverless pricing myths that cost teams real money",
    "why decentralized cloud needs Airbnb-style reviews (not just lower prices)",
    "the P2P compute trust gap: what Akash/Flux got wrong",
    "why the FinOps movement proves cloud pricing is deliberately opaque",
]


def has_product_mention(text: str) -> bool:
    for pattern in PRODUCT_MENTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def threads_this_week(conn) -> int:
    """Count threads posted in the current calendar week (from posts table)."""
    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    import psycopg2.extras
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM posts
            WHERE type = 'thread' AND posted_at >= %s
            """,
            (week_start,),
        )
        row = cur.fetchone()
        return row[0] if row else 0


def recent_thread_topics(conn, n: int = 4) -> list[str]:
    """Get topics of the N most recent threads (to avoid repeats), from kv_state."""
    raw = kv_get(conn, "thread:recent_topics")
    if not raw:
        return []
    try:
        topics = json.loads(raw)
        return topics[-n:] if len(topics) > n else topics
    except Exception:
        return []


def save_thread_topics(conn, topic: str) -> None:
    """Append a topic to the recent topics list in kv_state (capped at 10)."""
    raw = kv_get(conn, "thread:recent_topics")
    topics: list[str] = []
    if raw:
        try:
            topics = json.loads(raw)
        except Exception:
            topics = []
    topics.append(topic)
    if len(topics) > 10:
        topics = topics[-10:]
    kv_set(conn, "thread:recent_topics", json.dumps(topics))


def _get_engagement_themes(recent_engagements: list[dict], n: int = 15) -> list[str]:
    """Extract recent engagement themes to inform thread topic selection."""
    recent = recent_engagements[-n:]
    terms = [e.get("search_term", "") for e in recent if e.get("search_term")]
    return list(dict.fromkeys(terms))


def generate_thread(conn) -> dict | None:
    """Use LLM to generate a technical thread (6-10 tweets).

    Returns dict with 'topic' and 'tweets' keys, or None on failure.
    """
    recent_topics = recent_thread_topics(conn, n=6)
    recent_posts_db = get_recent_posts(conn, days=14, limit=10)
    recent_engagements_db = get_recent_engagements(conn, hours=168, limit=15)

    recent_posts = [p.get("text", "")[:150] for p in recent_posts_db]
    engagement_themes = _get_engagement_themes(recent_engagements_db)

    project_context = load_project_context()

    prompt = f"""Generate a technical thread for Twitter (6-10 tweets).

# Project & Strategy Context
{project_context}

# Thread Structure
- Tweet 1: Hook — POWERFUL opening: bold counter-intuitive claim, shocking stat, or question that demands attention
- Tweets 2-7: Supporting points with SPECIFIC examples, numbers, or technical details — each one a mini-revelation
- Tweet 8-10: Conclusion or call to discussion (ask a question, not a CTA)
- Each tweet must be a COMPLETE thought that also flows into the next
- Number tweets: "1/", "2/", etc. at the start
- Premium+ note: we have more reach now — make threads longer (8-10) and more controversial to maximize virality

## Suggested topics (pick one or go deeper on an engagement theme below):
{json.dumps(THREAD_TOPICS[:12], indent=2)}

## DO NOT repeat these recent topics:
{json.dumps(recent_topics, indent=2)}

## Recent single tweets (avoid overlap in angle or framing):
{json.dumps(recent_posts, indent=2)}

## Audience engagement signal — go DEEP on one of these:
{json.dumps(engagement_themes, indent=2) if engagement_themes else "  (no data yet)"}

## Voice & Style (see STRATEGY.md for full reference with examples)
- Observational, not imperative — describe what happens, don't instruct the reader
- Short sentences per tweet, each doing one job — but substantive enough to be useful
- Specific details: numbers, timeframes, mechanics — these make takes feel real, not generic
- Peer voice — knowledgeable friend explaining something, not a blog post or security advisory
- NOT: "Make sure you check egress fees before migrating" — YES: "AWS charges ~$90/TB out, $0/TB in"
- The implication of each point should emerge from the facts, not be stated as a directive

## Constraints
- Each tweet MUST be under 260 characters (HARD LIMIT — count carefully!)
- 6-10 tweets total (Premium+ account — longer threads get more reach)
- Thread should be CONTROVERSIAL enough to argue with but DEFENSIBLE
- Include specific numbers, comparisons, or technical details where possible

## Output Format (JSON only, no other text):
{{"topic": "brief topic description", "tweets": ["1/ first tweet text", "2/ second tweet", ...]}}"""

    try:
        raw = call_llm(prompt, timeout=180)
        if not raw:
            print("LLM returned nothing for thread generation", file=sys.stderr)
            return None

        json_str = extract_json(raw)
        if not json_str:
            print(
                f"Could not extract JSON from thread LLM response: {raw[:200]}",
                file=sys.stderr,
            )
            return None

        data = json.loads(json_str)
        topic = data.get("topic", "")
        tweets = data.get("tweets", [])

        if not tweets or len(tweets) < 3:
            print(f"Thread too short: {len(tweets)} tweets", file=sys.stderr)
            return None

        if len(tweets) > 10:
            tweets = tweets[:10]

        # Validate each tweet
        valid_tweets = []
        for i, tweet in enumerate(tweets):
            tweet = tweet.strip()

            if len(tweet) > 280:
                print(
                    f"Thread tweet {i + 1} too long ({len(tweet)} chars), truncating",
                    file=sys.stderr,
                )
                tweet = tweet[:277] + "..."

            if has_product_mention(tweet):
                print(
                    f"Thread tweet {i + 1} has product mention, skipping",
                    file=sys.stderr,
                )
                continue

            valid_tweets.append(tweet)

        if len(valid_tweets) < 3:
            print(
                f"Too few valid tweets after filtering: {len(valid_tweets)}",
                file=sys.stderr,
            )
            return None

        return {"topic": topic, "tweets": valid_tweets}

    except Exception as e:
        print(f"Thread generation failed: {e}", file=sys.stderr)
        return None


def post_thread(tweets: list[str]) -> tuple[bool, list[str]]:
    """Post a thread (list of tweet texts) via CDP.

    Posts first tweet, gets its ID, then chains replies.
    Returns (success, list_of_tweet_ids).
    """
    if not tweets:
        return False, []

    posted_ids: list[str] = []

    # Post first tweet
    print(f"Thread: posting tweet 1/{len(tweets)}...", flush=True)
    if not post_tweet(tweets[0]):
        print("Thread: failed to post first tweet", flush=True)
        return False, []

    # Wait for tweet to appear on profile
    jitter_sleep(8, 15)

    # Get the first tweet's ID from our profile
    first_id = get_latest_own_tweet_id(OUR_USERNAME)
    if not first_id:
        print("Thread: couldn't get first tweet ID from profile", flush=True)
        # Still posted the first tweet, just can't chain
        return False, []

    posted_ids.append(first_id)
    print(f"Thread: first tweet ID = {first_id}", flush=True)

    # Chain remaining tweets as replies
    for i, tweet_text in enumerate(tweets[1:], 2):
        parent_id = posted_ids[-1]

        # Human-like delay between thread tweets
        jitter_sleep(30, 90)

        print(
            f"Thread: posting tweet {i}/{len(tweets)} (replying to {parent_id})...",
            flush=True,
        )
        posted, reply_id = post_reply(parent_id, tweet_text)
        if not posted:
            print(f"Thread: failed at tweet {i}/{len(tweets)}", flush=True)
            # Partial thread — still some value
            break

        if reply_id:
            posted_ids.append(reply_id)
            print(f"Thread: tweet {i} ID = {reply_id}", flush=True)
        else:
            print(
                f"Thread: posted tweet {i} but couldn't read its ID, stopping chain",
                flush=True,
            )
            break

    success = len(posted_ids) == len(tweets)
    print(f"Thread: posted {len(posted_ids)}/{len(tweets)} tweets", flush=True)
    return success, posted_ids


def main() -> int:
    print("=== TWITTER WEEKLY THREAD POSTER ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    try:
        with get_conn() as conn:
            # Check if we've already posted a thread this week
            count = threads_this_week(conn)
            print(f"Threads this week: {count}/1", flush=True)

            if count >= 1:
                print("Already posted a thread this week, skipping")
                return 0

            # Generate thread content
            print("Generating thread via LLM...", flush=True)
            thread_data = generate_thread(conn)

            if not thread_data:
                send_error_alert("Failed to generate thread (LLM returned nothing usable)")
                print("Failed to generate thread")
                return 1

            topic = thread_data["topic"]
            tweets = thread_data["tweets"]
            print(f"Topic: {topic}", flush=True)
            print(f"Tweets: {len(tweets)}", flush=True)

            # Humanize each tweet
            humanized_tweets = []
            for i, tweet in enumerate(tweets):
                try:
                    h = humanize(tweet)
                    # Ensure numbering is preserved
                    if not h.startswith(f"{i + 1}/"):
                        h = f"{i + 1}/ {h.lstrip('0123456789/').strip()}"
                    humanized_tweets.append(h)
                    print(f"  Tweet {i + 1}: {h[:60]}...", flush=True)
                except Exception as e:
                    print(
                        f"  Humanize failed for tweet {i + 1}: {e}, using original",
                        flush=True,
                    )
                    humanized_tweets.append(tweet)

            # Validate lengths after humanization
            for i, tweet in enumerate(humanized_tweets):
                if len(tweet) > 280:
                    humanized_tweets[i] = tweet[:277] + "..."
                    print(
                        f"  Tweet {i + 1} truncated to 280 chars after humanization",
                        flush=True,
                    )

            # Post the thread
            print("Posting thread via CDP...", flush=True)
            success, tweet_ids = post_thread(humanized_tweets)

            if not tweet_ids:
                send_error_alert(f"Thread posting failed completely\n\nTopic: {topic}")
                print("Thread posting failed")
                return 1

            if not success:
                print(
                    f"Thread partially posted ({len(tweet_ids)}/{len(humanized_tweets)} tweets)",
                    flush=True,
                )

            thread_url = f"https://x.com/{OUR_USERNAME}/status/{tweet_ids[0]}"
            print(f"Thread posted: {thread_url}", flush=True)

            # Log first tweet as 'thread' type in posts table
            insert_post(
                conn,
                tweet_id=tweet_ids[0],
                type="thread",
                text=humanized_tweets[0],
                url=thread_url,
            )
            # Also log each subsequent tweet in the thread
            for i, tid in enumerate(tweet_ids[1:], 1):
                insert_post(
                    conn,
                    tweet_id=tid,
                    type="thread_reply",
                    text=humanized_tweets[i] if i < len(humanized_tweets) else "",
                    url=f"https://x.com/{OUR_USERNAME}/status/{tid}",
                )
            print(f"Logged {len(tweet_ids)} tweet(s) to posts table", flush=True)

            # Store topic in kv_state for dedup on next run
            save_thread_topics(conn, topic)

        return 0

    except Exception as e:
        send_error_alert(f"Thread posting error: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
