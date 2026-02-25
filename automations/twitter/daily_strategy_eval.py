#!/usr/bin/env python3
"""Daily Twitter strategy evaluation and recommendations.

Gathers objective metrics:
- Follower count
- Engagement stats (replies, posts)
- Follower growth rate
- Engagement reply rate

Uses LLM to analyze and recommend strategic improvements.

Runs daily at 7 AM UTC (after morning research, before first engagement).
"""

from __future__ import annotations

import json
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from lib.llm_utils import call_llm, extract_json
from twitter_utils import (
    get_follower_count as get_user_follower_count,
    get_tweet_stats,
    load_project_context,
    utc_now,
    send_error_alert,
)
from db import (
    get_conn,
    count_engagements,
    count_posts_today,
    get_recent_evals,
    get_last_eval,
    get_search_term_stats,
    get_engagements_for_perf_check,
    insert_eval,
    update_search_term_perf,
    update_engagement_perf,
)


def get_follower_count() -> int | None:
    """Get our own follower count via shared CDP function."""
    return get_user_follower_count("DecentCloud_org")


def gather_metrics(conn) -> dict:
    """Gather current Twitter metrics from the database."""
    now = datetime.now(timezone.utc)

    # Last 24h and 7d engagement counts
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    engagements_24h = count_engagements(conn, since=day_ago)
    engagements_7d = count_engagements(conn, since=week_ago)

    # Post counts — count_posts_today only covers since midnight
    # For 24h and 7d, query engagements table isn't right; use a direct approach
    # count_posts_today covers today; for 7d we query directly
    posts_24h = _count_posts_since(conn, since=day_ago)
    posts_7d = _count_posts_since(conn, since=week_ago)

    follower_count = get_follower_count()

    # Follower growth (compare to last eval)
    last_eval = get_last_eval(conn)
    prev_followers = None
    prev_eval_date = None
    if last_eval:
        prev_followers = last_eval.get("follower_count")
        prev_eval_date = str(last_eval.get("eval_date", ""))

    follower_growth = None
    if follower_count and prev_followers:
        follower_growth = follower_count - prev_followers

    # Check performance of recent replies (2-24h old, not yet perf-checked)
    reply_performances = _gather_reply_performances(conn)

    return {
        "date": now.date().isoformat(),
        "timestamp": utc_now(),
        "followerCount": follower_count,
        "followerGrowth": follower_growth,
        "prevEvalDate": prev_eval_date,
        "engagements24h": engagements_24h,
        "engagements7d": engagements_7d,
        "posts24h": posts_24h,
        "posts7d": posts_7d,
        "replyPerformances": reply_performances,
    }


def _count_posts_since(conn, since: datetime) -> int:
    """Count posts since a given datetime."""
    import psycopg2.extras
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM posts WHERE posted_at >= %s",
            (since,),
        )
        row = cur.fetchone()
        return row[0] if row else 0


def _gather_reply_performances(conn) -> list[dict]:
    """Fetch performance stats for replies that are 2-24h old.

    Also persists the performance data into engagements and search_term_stats tables.
    """
    candidates = get_engagements_for_perf_check(conn)
    reply_performances = []

    for eng in candidates:
        reply_id = eng.get("our_reply_id")
        if not reply_id:
            continue

        stats = get_tweet_stats(reply_id)
        if not stats:
            continue

        likes = stats.get("likes", 0)
        rts = stats.get("retweets", 0)
        replies = stats.get("replies", 0)
        got_reply_back = replies > 0

        # Persist perf back to engagements row
        update_engagement_perf(
            conn,
            tweet_id=eng["tweet_id"],
            likes=likes,
            rts=rts,
            replies=replies,
            got_reply_back=got_reply_back,
        )

        # Persist perf into search_term_stats if the engagement had a term
        term = eng.get("search_term")
        if term:
            zero_perf = 1 if likes == 0 and replies == 0 else 0
            update_search_term_perf(
                conn,
                term=term,
                likes=likes,
                reply_backs=1 if got_reply_back else 0,
                zero_perf=zero_perf,
            )

        reply_performances.append({
            "replyId": reply_id,
            "author": eng.get("target_username"),
            "replyText": (eng.get("our_reply_text") or "")[:100],
            "likes": likes,
            "retweets": rts,
            "replies": replies,
            "searchTerm": term,
        })

    print(f"  Perf-checked {len(reply_performances)} reply(ies)", flush=True)
    return reply_performances


def evaluate_with_llm(metrics: dict, history: list) -> str | None:
    """Use LLM to evaluate metrics and recommend improvements."""

    recent_history = history[-7:] if len(history) > 7 else history
    project_context = load_project_context()

    prompt = f"""Evaluate @DecentCloud_org Twitter strategy and recommend improvements.

# Project & Strategy Context
{project_context}

# Operational Parameters
- 5 engagement runs/day at 13:00, 17:00, 20:00, 23:00, 02:00 UTC (US peak)
- 8 replies per run = ~40 engagements/day target
- Follower filter: 100–5,000 (hard skip <100 and >5,000)
- 2 original posts/day (10:30, 16:30 UTC)
- Phase 2 unlock criteria: 1-3k followers AND replies consistently getting >5 likes

**Current Metrics (today):**
{json.dumps(metrics, indent=2)}

**Reply Performance (last 24h — replies with tracked IDs):**
{json.dumps(metrics.get("replyPerformances", []), indent=2) if metrics.get("replyPerformances") else "  No tracked replies yet (ourReplyId not captured for these)"}

**Last 7 days history:**
{json.dumps(recent_history, indent=2)}

**Evaluation Criteria:**
1. Follower growth rate (good: 5-10/day for cold start)
2. Engagement volume (target: 40/day = 8/run × 5 runs)
3. Post consistency (target: 2/day, no 0-post days)
4. Quality signals (are we reaching p2p/marketplace builders, DevOps with support pain?)
5. Phase progression (are we close to Phase 2 unlock criteria? unlock = 1-3k followers AND replies consistently >5 likes)
6. Messaging alignment (are we hitting marketplace trust angle or still just cost?)
7. Audience quality (engaging with p2p/marketplace builders vs generic DevOps?)
8. Reply quality (from replyPerformances: which reply angles get likes/RTs? which get nothing?)

**Your Task:**
Analyze objectively. Are we on track? What's working? What's not?

Recommend **specific, actionable improvements** if metrics are off. Do NOT recommend:
- Changing timing (already optimized)
- Changing follower filter (100-5k is correct per strategy)
- Adding product promotion (still Phase 1)
- Using hashtags

Focus recommendations on: search term quality, reply quality/targeting, consistency gaps, post content, marketplace trust messaging.

If replyPerformances data is available: identify which reply angles/search terms produce likes (>2) vs zero engagement. Recommend doubling down on performing angles and dropping underperforming ones.

Output format:
## Status: [On Track / Needs Improvement / Critical]

## What's Working:
- ...

## What's Not:
- ...

## Recommendations:
1. ...
2. ...

Keep it under 500 words."""

    try:
        success, result = call_llm(prompt, max_retries=3, timeout=180)
        if not success:
            print(f"LLM eval failed: {result}", file=sys.stderr)
            return None
        return result.strip()
    except Exception as e:
        print(f"LLM eval failed: {e}", file=sys.stderr)
        return None


def send_report(metrics: dict, evaluation: str) -> None:
    """Send daily report via Telegram."""

    # Build reply performance summary for report
    reply_perf = metrics.get("replyPerformances", [])
    if reply_perf:
        top = sorted(reply_perf, key=lambda x: x.get("likes", 0) + x.get("retweets", 0), reverse=True)[:3]
        perf_lines = []
        for r in top:
            perf_lines.append(
                f"  - @{r['author']}: {r['likes']}L/{r['retweets']}RT | {r['replyText'][:60]}...",
            )
        perf_summary = "\n**Top replies (last 24h):**\n" + "\n".join(perf_lines)
    else:
        perf_summary = ""

    report = f"""📊 Twitter Strategy Daily Evaluation

**Metrics:**
- Followers: {metrics.get("followerCount") or "N/A"}
- Growth (since last eval): {metrics.get("followerGrowth") or "N/A"}
- Engagements (24h): {metrics["engagements24h"]}
- Posts (24h): {metrics["posts24h"]}
- Engagements (7d): {metrics["engagements7d"]}
- Posts (7d): {metrics["posts7d"]}
- Replies tracked (24h): {len(reply_perf)}{perf_summary}

{evaluation}
"""

    subprocess.run(
        [
            "/home/openclaw/.npm-global/bin/openclaw",
            "message",
            "send",
            "--channel",
            "telegram",
            "--target",
            "5996479639",
            "--message",
            report,
        ],
        timeout=30,
    )


def main() -> int:
    print("=== TWITTER STRATEGY DAILY EVALUATION ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    try:
        with get_conn() as conn:
            # Gather metrics (also persists reply perf into DB)
            print("Gathering metrics...", flush=True)
            metrics = gather_metrics(conn)
            print(f"Metrics: {json.dumps(metrics, indent=2)}", flush=True)

            # Load eval history for LLM context
            history = get_recent_evals(conn, limit=7)

            # Evaluate with LLM
            print("Evaluating with LLM...", flush=True)
            evaluation = evaluate_with_llm(metrics, history)

            if not evaluation:
                msg = (
                    "LLM evaluation skipped: all retry attempts failed "
                    "(likely HTTP 429 rate limit). Metrics were gathered; "
                    "no report sent. Will retry tomorrow."
                )
                print(msg, file=sys.stderr, flush=True)
                send_error_alert(f"Daily strategy eval: {msg}")
                return 0

            print(f"Evaluation:\n{evaluation[:200]}...", flush=True)

            # Persist evaluation to DB
            now = datetime.now(timezone.utc)
            day_ago = now - timedelta(days=1)
            week_ago = now - timedelta(days=7)
            insert_eval(
                conn,
                eval_date=now.date(),
                follower_count=metrics.get("followerCount"),
                follower_growth=metrics.get("followerGrowth"),
                engagements_24h=metrics["engagements24h"],
                engagements_7d=metrics["engagements7d"],
                posts_24h=metrics["posts24h"],
                posts_7d=metrics["posts7d"],
                evaluation=evaluation,
                raw_metrics=metrics,
            )
            print("Evaluation saved to DB", flush=True)

            # Send report
            print("Sending report...", flush=True)
            send_report(metrics, evaluation)

        print("Complete", flush=True)
        return 0

    except Exception as e:
        send_error_alert(f"Unexpected error: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
