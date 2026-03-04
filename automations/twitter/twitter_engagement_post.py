#!/usr/bin/env python3
"""Phase 3: post prepared LLM decisions (requires browser lock)."""

from __future__ import annotations

import json
import re
import sys
import time as _time
from difflib import SequenceMatcher

sys.path.insert(0, "/projects/automations")

from prefect.concurrency.sync import concurrency
from db import (
    ensure_schema,
    get_conn,
    get_recent_engagements,
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


def _normalize_text(text: str | None) -> str:
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"@\w+", " ", t)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return " ".join(t.split())


def _is_similar_reply(candidate: str, previous: str) -> bool:
    a = _normalize_text(candidate)
    b = _normalize_text(previous)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < 20 or len(b) < 20:
        return False

    ratio = SequenceMatcher(None, a, b).ratio()
    if ratio >= 0.90:
        return True

    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return False
    overlap = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    return overlap >= 0.72 and ratio >= 0.72


def _iter_our_thread_texts(tweet_context: dict) -> list[str]:
    texts: list[str] = []
    our_handle = "decentcloud_org"
    for p in tweet_context.get("parentChain") or []:
        if (p.get("username") or "").lower() == our_handle and p.get("text"):
            texts.append(str(p["text"]))
    for r in tweet_context.get("otherReplies") or []:
        if (r.get("username") or "").lower() == our_handle and r.get("text"):
            texts.append(str(r["text"]))
    for ex in tweet_context.get("priorExchanges") or []:
        reply = ex.get("our_reply_text")
        if reply:
            texts.append(str(reply))
    return texts


def _immediate_parent_is_us(tweet_context: dict) -> bool:
    our_handle = "decentcloud_org"
    parent_chain = tweet_context.get("parentChain") or []
    if parent_chain:
        parent_user = (parent_chain[-1].get("username") or "").lower()
        if parent_user == our_handle:
            return True
    reply_to = tweet_context.get("replyTo") or {}
    return (reply_to.get("username") or "").lower() == our_handle


def main() -> int:
    print("=== TWITTER ENGAGEMENT POST ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    try:
        with get_conn() as conn:
            ensure_schema(conn)
            items = get_pipeline_items_by_status(conn, "analyzed", limit=20)
            recent_engagements = get_recent_engagements(conn, hours=72, limit=300)
        if not items:
            print("No analyzed candidates ready for posting", flush=True)
            return 0

        recent_reply_texts = [
            str(e.get("our_reply_text") or "")
            for e in recent_engagements
            if e.get("our_reply_text")
        ]

        posted_count = 0
        with concurrency("twitter-browser", occupy=1):
            for item in items:
                if posted_count >= 8:
                    break

                tweet_id = item["tweet_id"]
                with get_conn() as conn:
                    if is_engaged(conn, tweet_id):
                        mark_pipeline_skipped(conn, tweet_id, "already engaged")
                        continue

                decision = _decode_json(item.get("decision_json"))
                tweet_context = _decode_json(item.get("context_json"))
                author = item.get("author") or "unknown"
                search_term = item.get("search_term") or ""
                url = item.get("url") or f"https://x.com/i/web/status/{tweet_id}"

                if not decision or not decision.get("shouldEngage"):
                    with get_conn() as conn:
                        mark_pipeline_skipped(conn, tweet_id, "missing/negative decision")
                    continue

                engagement_type = decision.get("engagementType", "reply")
                reply_text = decision.get("reply")
                posted = False
                our_reply_id = None
                engagement_source = "search"

                if engagement_type in {"reply", "quote"} and _immediate_parent_is_us(tweet_context):
                    with get_conn() as conn:
                        mark_pipeline_skipped(conn, tweet_id, "we are latest reply in thread")
                    continue

                if engagement_type in {"reply", "quote"} and reply_text:
                    compare_texts = _iter_our_thread_texts(tweet_context) + recent_reply_texts
                    if any(_is_similar_reply(reply_text, prev) for prev in compare_texts):
                        with get_conn() as conn:
                            mark_pipeline_skipped(conn, tweet_id, "reply too similar to recent/ thread reply")
                        continue

                jitter_sleep()
                try:
                    if engagement_type == "like":
                        posted = like_tweet(tweet_id)
                        engagement_source = "like"
                    elif engagement_type == "quote":
                        if not reply_text:
                            with get_conn() as conn:
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
                            with get_conn() as conn:
                                mark_pipeline_post_failed(conn, tweet_id, msg)
                            continue
                        engagement_source = "quote"
                    else:
                        if not reply_text:
                            with get_conn() as conn:
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
                            with get_conn() as conn:
                                mark_pipeline_post_failed(conn, tweet_id, msg)
                            continue
                except Exception as e:
                    with get_conn() as conn:
                        mark_pipeline_post_failed(conn, tweet_id, str(e))
                    continue

                if not posted:
                    with get_conn() as conn:
                        mark_pipeline_post_failed(conn, tweet_id, "posting returned false")
                    continue

                if engagement_type != "like":
                    auto_follow_after_engagement(
                        None, author, tweet_id, source=engagement_source
                    )

                stats = tweet_context.get("stats", {})
                with get_conn() as conn:
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
                if reply_text:
                    recent_reply_texts.append(reply_text)
                posted_count += 1

        print(f"Posting complete: posted={posted_count}", flush=True)
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
