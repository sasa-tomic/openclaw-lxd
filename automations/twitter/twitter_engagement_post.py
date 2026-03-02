#!/usr/bin/env python3
"""Phase 3: post prepared LLM decisions (requires browser lock)."""

from __future__ import annotations

import json
import sys
import time as _time

sys.path.insert(0, "/projects/automations")

from prefect.concurrency.sync import concurrency
from db import (
    ensure_schema,
    get_conn,
    get_pipeline_items_by_status,
    insert_engagement,
    is_engaged,
    mark_pipeline_post_failed,
    mark_pipeline_posted,
    mark_pipeline_skipped,
)
from twitter_utils import (
    auto_follow_after_engagement,
    humanize,
    jitter_sleep,
    like_tweet,
    post_quote_tweet,
    post_reply,
    send_error_alert,
    utc_now,
)


def _decode_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def main() -> int:
    print("=== TWITTER ENGAGEMENT POST ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    try:
        with get_conn() as conn:
            ensure_schema(conn)
            items = get_pipeline_items_by_status(conn, "analyzed", limit=20)
            if not items:
                print("No analyzed candidates ready for posting", flush=True)
                return 0

            posted_count = 0
            with concurrency("twitter-browser", occupy=1):
                for item in items:
                    if posted_count >= 8:
                        break

                    tweet_id = item["tweet_id"]
                    if is_engaged(conn, tweet_id):
                        mark_pipeline_skipped(conn, tweet_id, "already engaged")
                        continue

                    decision = _decode_json(item.get("decision_json"))
                    tweet_context = _decode_json(item.get("context_json"))
                    author = item.get("author") or "unknown"
                    search_term = item.get("search_term") or ""
                    url = item.get("url") or f"https://x.com/i/web/status/{tweet_id}"

                    if not decision or not decision.get("shouldEngage"):
                        mark_pipeline_skipped(conn, tweet_id, "missing/negative decision")
                        continue

                    engagement_type = decision.get("engagementType", "reply")
                    reply_text = decision.get("reply")
                    posted = False
                    our_reply_id = None
                    engagement_source = "search"

                    jitter_sleep()
                    try:
                        if engagement_type == "like":
                            posted = like_tweet(tweet_id)
                            engagement_source = "like"
                        elif engagement_type == "quote":
                            if not reply_text:
                                mark_pipeline_skipped(conn, tweet_id, "quote missing reply text")
                                continue
                            reply_text = humanize(reply_text)
                            for attempt in range(1, 4):
                                posted, our_reply_id = post_quote_tweet(tweet_id, reply_text)
                                if posted:
                                    break
                                if attempt < 3:
                                    _time.sleep(5)
                            if not posted:
                                msg = f"Failed to quote-tweet {tweet_id} (@{author}) after 3 attempts"
                                send_error_alert(msg)
                                mark_pipeline_post_failed(conn, tweet_id, msg)
                                continue
                            engagement_source = "quote"
                        else:
                            if not reply_text:
                                mark_pipeline_skipped(conn, tweet_id, "reply missing reply text")
                                continue
                            reply_text = humanize(reply_text)
                            for attempt in range(1, 4):
                                posted, our_reply_id = post_reply(tweet_id, reply_text)
                                if posted:
                                    break
                                if attempt < 3:
                                    _time.sleep(5)
                            if not posted:
                                msg = f"Failed to post reply to {tweet_id} (@{author}) after 3 attempts"
                                send_error_alert(msg)
                                mark_pipeline_post_failed(conn, tweet_id, msg)
                                continue
                    except Exception as e:
                        mark_pipeline_post_failed(conn, tweet_id, str(e))
                        continue

                    if not posted:
                        mark_pipeline_post_failed(conn, tweet_id, "posting returned false")
                        continue

                    if engagement_type != "like":
                        auto_follow_after_engagement(conn, author, tweet_id)

                    stats = tweet_context.get("stats", {})
                    insert_engagement(
                        conn,
                        tweet_id=tweet_id,
                        target_username=author,
                        our_reply_text=reply_text,
                        our_reply_id=our_reply_id,
                        source=engagement_source,
                        search_term=search_term,
                        conv_likelihood=decision.get("audienceEngagementPotential")
                        or decision.get("conversationLikelihood"),
                        profile_click_worthy=decision.get("profileClickWorthy"),
                        llm_reasoning=decision.get("reasoning"),
                        target_tweet_text=tweet_context.get("text"),
                        tweet_url=url,
                        tweet_likes=stats.get("likes"),
                        tweet_rts=stats.get("retweets"),
                        tweet_replies=stats.get("replies"),
                    )
                    mark_pipeline_posted(conn, tweet_id)
                    posted_count += 1

            print(f"Posting complete: posted={posted_count}", flush=True)
            return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
