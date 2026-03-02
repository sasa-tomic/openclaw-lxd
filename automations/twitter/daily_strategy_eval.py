#!/usr/bin/env python3
"""Daily Twitter strategy evaluation and recommendations.

Gathers objective metrics:
- Follower count
- Engagement stats (replies, posts)
- Follower growth rate
- Engagement reply rate

Uses LLM to analyze and recommend strategic improvements.

Runs daily at 7 AM UTC.
"""

from __future__ import annotations

import json
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from lib.llm_utils import call_llm
from twitter_utils import (
    get_follower_count as get_user_follower_count,
    get_tweet_stats,
    load_project_context,
    utc_now,
    send_error_alert,
)
from db import (
    get_conn,
    get_recent_evals,
    get_last_eval,
    get_engagements_for_perf_refresh,
    get_engagement_counts_breakdown,
    get_post_counts_breakdown,
    get_reply_performance_snapshot,
    get_posts_for_stats_update,
    insert_eval,
    normalize_eval_metrics,
    update_search_term_perf,
    update_engagement_perf,
    update_post_stats,
)

ENGAGEMENT_RUNS_PER_DAY = 48  # deployment: :07 and :38 every hour
ENGAGEMENT_ACTION_CAP_PER_RUN = 8
ORIGINAL_POSTS_TARGET_PER_DAY = 5  # deployment: 07:30, 10:30, 13:30, 17:30, 21:30 UTC


def get_follower_count() -> int | None:
    """Get our own follower count via shared CDP function."""
    return get_user_follower_count("DecentCloud_org")


def gather_metrics(conn) -> dict:
    """Gather current Twitter metrics from the database."""
    now = datetime.now(timezone.utc)

    # Last 24h and 7d engagement counts
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    engagements_24h = get_engagement_counts_breakdown(conn, since=day_ago)
    engagements_7d = get_engagement_counts_breakdown(conn, since=week_ago)
    posts_24h = get_post_counts_breakdown(conn, since=day_ago)
    posts_7d = get_post_counts_breakdown(conn, since=week_ago)

    follower_count = get_follower_count()

    # Follower growth (compare to last eval)
    last_eval = get_last_eval(conn)
    prev_followers = None
    prev_eval_date = None
    if last_eval:
        prev_followers = last_eval.get("follower_count")
        prev_eval_date = str(last_eval.get("eval_date", ""))

    follower_growth = None
    if follower_count is not None and prev_followers is not None:
        follower_growth = follower_count - prev_followers

    # Refresh stats for recent original posts so get_top_posts() has real numbers
    post_stats_refreshed = _refresh_post_stats(conn)

    # Refresh and then read a true 24h snapshot for reporting/evaluation
    reply_perf_refresh = _refresh_reply_performances(conn)
    reply_performances_24h = get_reply_performance_snapshot(conn, since=day_ago)

    return {
        "date": now.date().isoformat(),
        "timestamp": utc_now(),
        "operationalTargets": {
            "engagementRunsPerDay": ENGAGEMENT_RUNS_PER_DAY,
            "engagementActionCapPerRun": ENGAGEMENT_ACTION_CAP_PER_RUN,
            "engagementActionCapPerDay": ENGAGEMENT_RUNS_PER_DAY * ENGAGEMENT_ACTION_CAP_PER_RUN,
            "originalPostsPerDay": ORIGINAL_POSTS_TARGET_PER_DAY,
        },
        "followerCount": follower_count,
        "followerGrowth": follower_growth,
        "prevEvalDate": prev_eval_date,
        "engagements24hTotal": engagements_24h["total"],
        "replies24h": engagements_24h["non_like"],
        "likes24h": engagements_24h["like_only"],
        "engagements7dTotal": engagements_7d["total"],
        "replies7d": engagements_7d["non_like"],
        "likes7d": engagements_7d["like_only"],
        "posts24hTotal": posts_24h["total"],
        "originalPosts24h": posts_24h["original_posts"],
        "threadRoots24h": posts_24h["thread_roots"],
        "threadReplies24h": posts_24h["thread_replies"],
        "posts7dTotal": posts_7d["total"],
        "originalPosts7d": posts_7d["original_posts"],
        "threadRoots7d": posts_7d["thread_roots"],
        "threadReplies7d": posts_7d["thread_replies"],
        "postStatsRefreshed": post_stats_refreshed,
        "replyPerfRefresh": reply_perf_refresh,
        "replyPerformances24h": reply_performances_24h,
    }


def _refresh_reply_performances(conn) -> dict:
    """Fetch and refresh performance stats for recent replies (up to 7 days old).

    Re-checks on every daily run so stats stay current throughout a reply's
    lifecycle, not just as a one-shot snapshot in the first 24h.

    search_term_perf is only updated on the FIRST check to avoid double-counting.
    """
    candidates = get_engagements_for_perf_refresh(conn)
    checked = 0
    first_checks = 0

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
        is_first_check = eng.get("perf_checked_at") is None

        # Always refresh engagement row stats
        update_engagement_perf(
            conn,
            tweet_id=eng["tweet_id"],
            likes=likes,
            rts=rts,
            replies=replies,
            got_reply_back=got_reply_back,
        )

        # Only update search_term_perf on first check — it accumulates, so
        # re-counting on every daily run would inflate the totals.
        if is_first_check:
            first_checks += 1
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

        checked += 1

    rechecked = checked - first_checks
    print(f"  Reply perf refreshed: {first_checks} first-check, {rechecked} re-checked", flush=True)
    return {
        "candidates": len(candidates),
        "checked": checked,
        "firstChecks": first_checks,
        "rechecks": rechecked,
    }


def _refresh_post_stats(conn) -> int:
    """Fetch current likes/rts for recent original posts and write them to DB.

    Runs on every daily_strategy_eval so get_top_posts() returns accurate data
    for the LLM candidate ranking prompt.
    """
    posts = get_posts_for_stats_update(conn)
    updated = 0
    for post in posts:
        stats = get_tweet_stats(post["tweet_id"])
        if not stats:
            continue
        update_post_stats(
            conn,
            tweet_id=post["tweet_id"],
            likes=stats.get("likes", 0),
            rts=stats.get("retweets", 0),
        )
        updated += 1

    print(f"  Post stats refreshed: {updated}/{len(posts)}", flush=True)
    return updated


def _history_for_prompt(history: list[dict]) -> list[dict]:
    """Convert eval_history rows into a compact, canonical metrics history."""
    rows = history[-7:] if len(history) > 7 else history
    snapshot: list[dict] = []
    for row in rows:
        normalized = row.get("raw_metrics_normalized")
        if not isinstance(normalized, dict):
            normalized = normalize_eval_metrics(row.get("raw_metrics"), fallback_row=row)
        snapshot.append(
            {
                "evalDate": row.get("eval_date"),
                "followerCount": normalized.get("followerCount"),
                "followerGrowth": normalized.get("followerGrowth"),
                "engagements24hTotal": normalized.get("engagements24hTotal"),
                "replies24h": normalized.get("replies24h"),
                "likes24h": normalized.get("likes24h"),
                "originalPosts24h": normalized.get("originalPosts24h"),
                "posts24hTotal": normalized.get("posts24hTotal"),
                "engagements7dTotal": normalized.get("engagements7dTotal"),
                "originalPosts7d": normalized.get("originalPosts7d"),
                "repliesTracked24h": len(normalized.get("replyPerformances24h") or []),
            }
        )
    return snapshot


def evaluate_with_llm(metrics: dict, history: list) -> str | None:
    """Use LLM to evaluate metrics and recommend improvements."""

    recent_history = _history_for_prompt(history)
    project_context = load_project_context()

    prompt = f"""Evaluate @DecentCloud_org Twitter strategy and recommend improvements.

# Project & Strategy Context
{project_context}

# Operational Parameters
- Engagement flow runs/day: {metrics["operationalTargets"]["engagementRunsPerDay"]}
- Engagement action cap/run: {metrics["operationalTargets"]["engagementActionCapPerRun"]}
- Engagement action cap/day (upper bound): {metrics["operationalTargets"]["engagementActionCapPerDay"]}
- Follower filter: 100–5,000 (hard skip <100 and >5,000)
- Original post target/day: {metrics["operationalTargets"]["originalPostsPerDay"]}
- Phase 2 unlock criteria: 1-3k followers AND replies consistently getting >5 likes

**Current Metrics (today):**
{json.dumps(metrics, indent=2)}

**Reply Performance Snapshot (true last 24h — replies/quotes with tracked IDs):**
{json.dumps(metrics.get("replyPerformances24h", []), indent=2) if metrics.get("replyPerformances24h") else "  No tracked reply/quote rows in the 24h window."}

**Last 7 days history:**
{json.dumps(recent_history, indent=2)}

**Evaluation Criteria:**
1. Follower growth rate (good: 5-10/day for cold start)
2. Engagement volume (use replies24h and likes24h; don't assume all engagement rows are replies)
3. Post consistency (target originalPosts24h ~= {ORIGINAL_POSTS_TARGET_PER_DAY}; don't confuse thread replies with original posts)
4. Quality signals (are we reaching p2p/marketplace builders, DevOps with support pain?)
5. Phase progression (are we close to Phase 2 unlock criteria? unlock = 1-3k followers AND replies consistently >5 likes)
6. Messaging alignment (are we hitting marketplace trust angle or still just cost?)
7. Audience quality (engaging with p2p/marketplace builders vs generic DevOps?)
8. Reply quality (from replyPerformances24h: which reply angles get likes/RTs? which get nothing?)

**Your Task:**
Analyze objectively. Are we on track? What's working? What's not?

Recommend **specific, actionable improvements** if metrics are off. Do NOT recommend:
- Changing timing (already optimized)
- Changing follower filter (100-5k is correct per strategy)
- Adding product promotion (still Phase 1)
- Using hashtags

Focus recommendations on: search term quality, reply quality/targeting, consistency gaps, post content, marketplace trust messaging.

If replyPerformances24h data is available: identify which reply angles/search terms produce likes (>2) vs zero engagement. Recommend doubling down on performing angles and dropping underperforming ones.

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
    reply_perf = metrics.get("replyPerformances24h", [])
    if reply_perf:
        top = sorted(reply_perf, key=lambda x: x.get("likes", 0) + x.get("retweets", 0), reverse=True)[:3]
        perf_lines = []
        for r in top:
            perf_lines.append(
                f"  - @{r['author']}: {r['likes']}L/{r['retweets']}RT | {r['replyText'][:60]}...",
            )
        perf_summary = "\n**Top tracked replies/quotes (24h):**\n" + "\n".join(perf_lines)
    else:
        perf_summary = ""

    report = f"""📊 Twitter Strategy Daily Evaluation

**Metrics:**
- Followers: {metrics.get("followerCount") or "N/A"}
- Growth (since last eval): {metrics.get("followerGrowth") or "N/A"}
- Engagements total (24h): {metrics["engagements24hTotal"]}
- Replies/quotes (24h): {metrics["replies24h"]}
- Likes-only (24h): {metrics["likes24h"]}
- Original posts (24h): {metrics["originalPosts24h"]}
- Posts total (24h): {metrics["posts24hTotal"]}
- Engagements total (7d): {metrics["engagements7dTotal"]}
- Original posts (7d): {metrics["originalPosts7d"]}
- Replies tracked snapshot (24h): {len(reply_perf)}{perf_summary}

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
            insert_eval(
                conn,
                eval_date=now.date(),
                follower_count=metrics.get("followerCount"),
                follower_growth=metrics.get("followerGrowth"),
                engagements_24h=metrics["engagements24hTotal"],
                engagements_7d=metrics["engagements7dTotal"],
                posts_24h=metrics["posts24hTotal"],
                posts_7d=metrics["posts7dTotal"],
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
