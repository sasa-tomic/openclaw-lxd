#!/usr/bin/env python3
"""Manage the Twitter engagement candidate queue.

Subcommands:
  fill   Search Twitter via CDP and add candidates to the queue (default)
  show   Display queued candidates
  stats  Breakdown by search term and age
  clear  Remove all unprocessed entries
  drop   Remove specific tweet IDs

Examples:
  uv run python search_queue.py              # fill: weighted sample of terms
  uv run python search_queue.py fill --all   # fill: all SEARCH_TERMS
  uv run python search_queue.py fill -n 5    # fill: sample 5 terms
  uv run python search_queue.py show
  uv run python search_queue.py show --limit 20
  uv run python search_queue.py stats
  uv run python search_queue.py clear
  uv run python search_queue.py drop 1234567890 9876543210
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, "/projects/automations/twitter")
sys.path.insert(0, "/projects/automations")

import psycopg2.extras

from db import (
    ensure_schema,
    get_conn,
    get_engaged_tweet_ids,
    get_queued_candidates,
    get_search_term_stats,
    insert_candidate_queue,
    queue_size,
)
from twitter_utils import (
    SEARCH_TERMS,
    search_candidates,
    utc_now,
    weighted_sample_terms,
)

DEFAULT_SAMPLE_SIZE = len(SEARCH_TERMS)  # search all terms by default


# ---------------------------------------------------------------------------
# Fill
# ---------------------------------------------------------------------------


def _candidates_for_term(
    cdp_candidates: list[dict],
    engaged_ids: set[str],
    already_queued_ids: set[str],
) -> list[dict]:
    """Filter CDP results down to new candidates not already seen or engaged."""
    result = []
    for c in cdp_candidates:
        tid = str(c.get("tweetId", ""))
        if not tid or tid in engaged_ids or tid in already_queued_ids:
            continue
        result.append(
            {
                "tweet_id": tid,
                "author": c.get("author") or "unknown",
                "text": (c.get("text") or "")[:500],
                "search_term": c.get("searchTerm") or "",
                "url": c.get("url") or f"https://x.com/i/web/status/{tid}",
                "tweet_datetime": None,
                "likes": c.get("likes", 0),
                "retweets": c.get("retweets", 0),
                "replies": c.get("replies", 0),
            }
        )
    return result


def cmd_fill(args: argparse.Namespace) -> int:
    print("=== SEARCH QUEUE FILL ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    # Phase 1: short read — fetch everything needed before the CDP searches.
    with get_conn() as conn:
        ensure_schema(conn)
        engaged_ids = get_engaged_tweet_ids(conn)
        term_stats = get_search_term_stats(conn)
        already_queued_ids = {c["tweetId"] for c in get_queued_candidates(conn, limit=10000)}

    if args.all:
        terms = list(SEARCH_TERMS)
        print(f"Searching all {len(terms)} terms...", flush=True)
    else:
        n = args.n or DEFAULT_SAMPLE_SIZE
        terms = weighted_sample_terms(
            SEARCH_TERMS, term_stats, min(n, len(SEARCH_TERMS))
        )
        print(f"Searching {len(terms)} weighted terms...", flush=True)

    # Phase 2+3 interleaved: search one term at a time and insert immediately.
    # This way a timeout only loses the terms not yet searched, not everything.
    total_inserted = 0
    for i, term in enumerate(terms, 1):
        print(f"  [{i}/{len(terms)}] '{term}'", flush=True)

        cdp_candidates = search_candidates(
            terms=[term], term_stats=term_stats, bypass_cache=True, since_hours=2, limit=50
        )
        new_candidates = _candidates_for_term(cdp_candidates, engaged_ids, already_queued_ids)

        if new_candidates:
            with get_conn() as conn:
                inserted = insert_candidate_queue(conn, new_candidates)
            total_inserted += inserted
            # Track newly queued IDs so later terms don't re-insert them
            for c in new_candidates:
                already_queued_ids.add(c["tweet_id"])
            print(f"    => {inserted} inserted", flush=True)

    # Final report
    print(f"\n  CDP: {total_inserted} fresh candidates across all terms", flush=True)
    print(f"Found {total_inserted} fresh candidates", flush=True)
    with get_conn() as conn:
        size = queue_size(conn)
        print(f"Queue: {size} unprocessed candidates ready for engagement", flush=True)

    return 0


# ---------------------------------------------------------------------------
# Show
# ---------------------------------------------------------------------------


_TWITTER_EPOCH_MS = 1288834974657  # Nov 4 2010 UTC


def _tweet_age_str(tweet_id: str, now: datetime) -> str:
    """Compute human-readable tweet age from its Snowflake ID."""
    try:
        ms = (int(tweet_id) >> 22) + _TWITTER_EPOCH_MS
        created = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        mins = int((now - created).total_seconds() / 60)
        return f"{mins}m" if mins < 60 else f"{mins // 60}h{mins % 60:02d}m"
    except Exception:
        return "?"


def cmd_show(args: argparse.Namespace) -> int:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT cq.tweet_id, cq.author, cq.search_term, cq.queued_at, cq.text
                FROM candidate_queue cq
                WHERE cq.processed_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM engagements e WHERE e.tweet_id = cq.tweet_id)
                  AND to_timestamp(((cq.tweet_id::bigint >> 22) + 1288834974657) / 1000.0)
                        > now() - interval '24 hours'
                ORDER BY cq.tweet_id DESC
                LIMIT %s
                """,
                (args.limit,),
            )
            rows = cur.fetchall()
        total = queue_size(conn)

    if not rows:
        print("Queue is empty.")
        return 0

    now = datetime.now(timezone.utc)
    print(f"{'TWEET_ID':<20} {'AUTHOR':<22} {'TERM':<28} {'TWEET AGE':>9}  TEXT")
    print("-" * 160)
    for r in rows:
        age_str = _tweet_age_str(r["tweet_id"], now)
        search_term = (r["search_term"] or "")[:28]
        text = (r["text"] or "").replace("\n", " ")[:100]
        print(
            f"{r['tweet_id']:<20} @{(r['author'] or ''):<21} "
            f"{search_term:<28} {age_str:>9}  {text}"
        )

    shown = len(rows)
    print(f"\n{shown} shown" + (f" (of {total} total)" if total > shown else "") + ".")
    return 0


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def cmd_stats(_args: argparse.Namespace) -> int:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT cq.tweet_id, cq.search_term
                FROM candidate_queue cq
                WHERE cq.processed_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM engagements e WHERE e.tweet_id = cq.tweet_id)
                  AND to_timestamp(((cq.tweet_id::bigint >> 22) + 1288834974657) / 1000.0)
                        > now() - interval '24 hours'
                ORDER BY cq.tweet_id ASC
                """
            )
            rows = cur.fetchall()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM candidate_queue WHERE processed_at IS NOT NULL"
            )
            processed_total = cur.fetchone()[0]

    if not rows:
        print(f"Queue is empty.  ({processed_total} candidates processed all-time)")
        return 0

    now = datetime.now(timezone.utc)
    by_term: Counter = Counter()
    by_age: Counter = Counter({"<1h": 0, "1-6h": 0, "6-24h": 0})

    for r in rows:
        by_term[r["search_term"] or "(none)"] += 1
        age_str = _tweet_age_str(r["tweet_id"], now)
        age_h = 0.0
        try:
            ms = (int(r["tweet_id"]) >> 22) + _TWITTER_EPOCH_MS
            age_h = (now - datetime.fromtimestamp(ms / 1000, tz=timezone.utc)).total_seconds() / 3600
        except Exception:
            pass
        if age_h < 1:
            by_age["<1h"] += 1
        elif age_h < 6:
            by_age["1-6h"] += 1
        else:
            by_age["6-24h"] += 1

    print(f"Unprocessed candidates: {len(rows)}")
    print(f"Processed all-time:     {processed_total}")
    print()
    print("By search term:")
    for term, count in by_term.most_common():
        bar = "█" * count
        print(f"  {term:<35} {count:>3}  {bar}")
    print()
    print("By age:")
    for bucket, count in by_age.items():
        print(f"  {bucket:<8} {count}")
    return 0


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------


def cmd_clear(_args: argparse.Namespace) -> int:
    with get_conn() as conn:
        size = queue_size(conn)
        if size == 0:
            print("Queue is already empty.")
            return 0
        confirm = input(f"Clear {size} unprocessed candidates? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return 0
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM candidate_queue
                WHERE processed_at IS NULL
                  AND queued_at > now() - interval '24 hours'
                  AND tweet_id NOT IN (SELECT tweet_id FROM engagements)
                """
            )
            deleted = cur.rowcount
    print(f"Cleared {deleted} entries.")
    return 0


# ---------------------------------------------------------------------------
# Drop
# ---------------------------------------------------------------------------


def cmd_drop(args: argparse.Namespace) -> int:
    ids = args.tweet_ids
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM candidate_queue WHERE tweet_id = ANY(%s)",
                (ids,),
            )
            deleted = cur.rowcount
    print(f"Dropped {deleted} of {len(ids)} requested entries.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage the Twitter engagement candidate queue",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd")

    # fill (default)
    p_fill = sub.add_parser("fill", help="Search Twitter and add candidates to queue")
    p_fill.add_argument("--all", action="store_true", help="Search all SEARCH_TERMS")
    p_fill.add_argument(
        "-n",
        type=int,
        metavar="N",
        help=f"Terms to sample (default: {DEFAULT_SAMPLE_SIZE})",
    )

    # show
    p_show = sub.add_parser("show", help="Display queued candidates")
    p_show.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="Max rows to display (default: 50)",
    )

    # stats
    sub.add_parser("stats", help="Breakdown by search term and age")

    # clear
    sub.add_parser("clear", help="Remove all unprocessed entries")

    # drop
    p_drop = sub.add_parser("drop", help="Remove specific tweet IDs from queue")
    p_drop.add_argument("tweet_ids", nargs="+", metavar="TWEET_ID")

    args = parser.parse_args()

    # Default to 'fill' when called with no subcommand (preserves old behaviour)
    if args.cmd is None:
        args.cmd = "fill"
        args.all = False
        args.n = None

    dispatch = {
        "fill": cmd_fill,
        "show": cmd_show,
        "stats": cmd_stats,
        "clear": cmd_clear,
        "drop": cmd_drop,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
