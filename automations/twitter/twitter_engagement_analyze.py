#!/usr/bin/env python3
"""Phase 2: LLM-only engagement analysis (no browser/Prefect lock)."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "/projects/automations")

from db import (
    ensure_schema,
    get_bottom_reply_combos,
    get_conn,
    get_pipeline_items_by_status,
    get_recent_engagements,
    get_recent_posts,
    get_top_reply_combos,
    update_pipeline_analysis,
)
from twitter_engagement import draft_reply_with_full_context
from twitter_utils import utc_now


def _decode_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def main() -> int:
    print("=== TWITTER ENGAGEMENT ANALYZE (LLM-ONLY) ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    try:
        with get_conn() as conn:
            ensure_schema(conn)
            items = get_pipeline_items_by_status(conn, "prepared", limit=30)
            if not items:
                print("No prepared candidates to analyze", flush=True)
                return 0
            recent_engagements = get_recent_engagements(conn, hours=168, limit=8)
            recent_posts = get_recent_posts(conn, limit=5)
            top_combos = get_top_reply_combos(conn, limit=20)
            bottom_combos = get_bottom_reply_combos(conn, limit=10)

        print(f"Running LLM analysis for {len(items)} candidates...", flush=True)

        def _analyze_one(item: dict) -> tuple[str, dict | None, str | None]:
            tweet_id = item["tweet_id"]
            candidate = _decode_json(item.get("candidate_json"))
            ctx = _decode_json(item.get("context_json"))
            if not candidate:
                candidate = {
                    "tweetId": tweet_id,
                    "author": item.get("author") or "unknown",
                    "text": item.get("text") or "",
                    "searchTerm": item.get("search_term") or "",
                    "url": item.get("url") or f"https://x.com/i/web/status/{tweet_id}",
                    "likes": item.get("likes") or 0,
                    "retweets": item.get("retweets") or 0,
                    "replies": item.get("replies") or 0,
                }
            if not ctx:
                return tweet_id, None, "missing context_json"
            decision = draft_reply_with_full_context(
                candidate,
                ctx,
                recent_engagements,
                recent_posts,
                top_combos,
                bottom_combos,
            )
            return tweet_id, decision, None

        max_workers = min(len(items), 3)
        outcomes: list[tuple[str, dict | None, str | None]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_analyze_one, item) for item in items]
            for future in as_completed(futures):
                outcomes.append(future.result())

        analyzed = 0
        skipped = 0
        failures = 0
        with get_conn() as conn:
            for tweet_id, decision, error in outcomes:
                if error:
                    update_pipeline_analysis(
                        conn, tweet_id, None, status="analysis_failed", error=error
                    )
                    failures += 1
                    continue
                if not decision or not decision.get("shouldEngage"):
                    update_pipeline_analysis(conn, tweet_id, decision, status="skipped")
                    skipped += 1
                    continue
                update_pipeline_analysis(conn, tweet_id, decision, status="analyzed")
                analyzed += 1

        print(
            f"Analysis complete: analyzed={analyzed}, skipped={skipped}, failed={failures}",
            flush=True,
        )
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
