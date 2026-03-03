#!/usr/bin/env python3
"""Phase 1: prepare engagement candidates for LLM analysis.

This phase performs search/queue intake + CDP context collection only.
No LLM calls happen here.
"""

from __future__ import annotations

import sys
from collections import Counter

sys.path.insert(0, "/projects/automations")

from prefect.concurrency.sync import concurrency
from engagement_triage import filter_candidates_for_engagement, reach_score
from db import (
    ensure_schema,
    get_conn,
    get_engaged_tweet_ids,
    get_engagements_with_user,
    get_our_thread_context,
    get_queued_candidates,
    is_engaged,
    mark_queue_processed,
    upsert_prepared_candidate,
    upsert_search_term_stats,
    upsert_tweet_replies,
)
from twitter_utils import (
    BLOCKED_AUTHORS,
    fetch_tweet_context,
    get_user_profile,
    utc_now,
)


def main() -> int:
    print("=== TWITTER ENGAGEMENT PREPARE ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    try:
        with get_conn() as conn:
            ensure_schema(conn)
            engaged_ids = get_engaged_tweet_ids(conn)
            candidates = get_queued_candidates(conn, limit=100)

        using_queue = bool(candidates)
        if using_queue:
            print(f"Using {len(candidates)} candidates from queue", flush=True)
        else:
            print("Queue empty — nothing to prepare", flush=True)
            return 0

        eligible, discard_ids = filter_candidates_for_engagement(
            candidates,
            engaged_ids={str(i) for i in engaged_ids},
            blocked_authors=set(BLOCKED_AUTHORS),
        )

        if using_queue and discard_ids:
            with get_conn() as conn:
                mark_queue_processed(conn, discard_ids)

        if not eligible:
            print("No suitable candidates after filtering", flush=True)
            return 0

        eligible.sort(key=reach_score, reverse=True)
        selected = eligible[:30]
        print(f"Preparing context for {len(selected)} candidates", flush=True)

        term_candidate_counts: Counter = Counter(
            c.get("searchTerm", "") for c in selected if c.get("searchTerm")
        )
        prepared = 0

        with concurrency("twitter-browser", occupy=1):
            for candidate in selected:
                tweet_id = str(candidate["tweetId"])
                author = candidate.get("author") or "unknown"

                with get_conn() as conn:
                    if is_engaged(conn, tweet_id):
                        if using_queue:
                            mark_queue_processed(conn, [tweet_id])
                        continue

                print(f"Fetching context for {tweet_id} (@{author})...", flush=True)
                tweet_context = fetch_tweet_context(tweet_id)
                if not tweet_context:
                    print(f"  Failed to fetch {tweet_id}", flush=True)
                    continue

                with get_conn() as conn:
                    visible_ids = [tweet_id]
                    for p in tweet_context.get("parentChain") or []:
                        if p.get("tweetId"):
                            visible_ids.append(str(p["tweetId"]))
                    for t in tweet_context.get("threadContinuation") or []:
                        if t.get("id"):
                            visible_ids.append(str(t["id"]))
                    tweet_context["ourThreadContext"] = get_our_thread_context(conn, visible_ids)

                    other_replies = tweet_context.get("otherReplies") or []
                    if other_replies:
                        upsert_tweet_replies(conn, tweet_id, other_replies)

                    author_name = tweet_context.get("author", "") or author
                    tweet_context["priorExchanges"] = get_engagements_with_user(conn, author_name)

                    upsert_prepared_candidate(conn, candidate, tweet_context)
                    if using_queue:
                        mark_queue_processed(conn, [tweet_id])

                author_name = tweet_context.get("author", "") or author
                if author_name:
                    author_profile = get_user_profile(author_name)
                    if author_profile:
                        tweet_context["authorProfile"] = author_profile
                        with get_conn() as conn:
                            upsert_prepared_candidate(conn, candidate, tweet_context)

                prepared += 1

        with get_conn() as conn:
            for term, count in term_candidate_counts.items():
                upsert_search_term_stats(conn, term, candidates_delta=count, engaged_delta=0)

        print(f"Prepared {prepared} candidates for LLM analysis", flush=True)
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
