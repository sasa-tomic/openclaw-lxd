#!/usr/bin/env python3
"""Fill the Twitter engagement candidate queue via twscrape (no browser needed).

Searches Twitter for relevant tweets and stores them in `candidate_queue`.
The engagement flow drains the queue instead of doing live CDP searches,
freeing the browser for fetch_tweet_context() and post_reply() only.

Setup (first time):
  uv run python search_queue.py --setup

Then run searches:
  uv run python search_queue.py           # weighted sample of terms
  uv run python search_queue.py --all     # all SEARCH_TERMS
  uv run python search_queue.py --n 5     # sample 5 terms

The twscrape account DB lives at ~/.config/twscrape/accounts.db.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from getpass import getpass
from pathlib import Path

sys.path.insert(0, "/projects/automations/twitter")
sys.path.insert(0, "/projects/automations")

from twscrape import API

from db import (
    get_conn,
    get_engaged_tweet_ids,
    get_search_term_stats,
    insert_candidate_queue,
    queue_size,
)
from twitter_utils import (
    BLOCKED_AUTHORS,
    SEARCH_TERMS,
    is_junk,
    utc_now,
    weighted_sample_terms,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TWSCRAPE_DB = Path("/home/openclaw/.config/twscrape/accounts.db")
SEARCH_HOURS_BACK = 12     # look back N hours for fresh content
RESULTS_PER_TERM = 20      # tweets to fetch per search term
DEFAULT_SAMPLE_SIZE = 12   # terms to sample per run when not using --all


# ---------------------------------------------------------------------------
# Account setup
# ---------------------------------------------------------------------------


async def cmd_setup(api: API) -> int:
    print("=== twscrape Account Setup ===")
    print("You need the credentials for the @DecentCloud_org account.")
    print()
    username = input("Twitter username (without @): ").strip()
    password = getpass("Twitter password: ")
    email = input("Account email: ").strip()
    email_password = getpass(
        "Email password (for 2FA verification codes — can be same as Twitter password): "
    ).strip()

    await api.pool.add_account(username, password, email, email_password or password)
    print(f"\nAdded @{username}. Logging in (may take a few seconds)...")
    await api.pool.login_all()

    accounts = await api.pool.get_all()
    active = [a for a in accounts if a.active]
    if active:
        print(f"✓ Login successful. {len(active)} account(s) active.")
        return 0
    else:
        print("✗ Login failed — check credentials and try again.")
        return 1


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def search_terms(
    api: API,
    terms: list[str],
    engaged_ids: set[str],
    hours_back: int = SEARCH_HOURS_BACK,
    limit_per_term: int = RESULTS_PER_TERM,
) -> list[dict]:
    """Search all terms via twscrape, return deduplicated candidates."""
    since_dt = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    since_ts = int(since_dt.timestamp())

    seen: dict[str, dict] = {}  # tweet_id → candidate (dedup across terms)
    blocked = {a.lower() for a in BLOCKED_AUTHORS}

    for term in terms:
        print(f"  Searching: {term!r}...", flush=True)
        term_count = 0
        try:
            # Twitter search operators: since_time, -is:retweet, lang:en
            query = f"{term} since_time:{since_ts} -is:retweet lang:en"
            async for tweet in api.search(query, limit=limit_per_term):
                tid = str(tweet.id)

                # Skip duplicates and already-engaged tweets
                if tid in seen or tid in engaged_ids:
                    continue

                # Skip blocked authors
                author = tweet.user.username if tweet.user else ""
                if author.lower() in blocked:
                    continue

                # Skip junk content
                text = tweet.rawContent or ""
                if is_junk(text):
                    continue

                seen[tid] = {
                    "tweet_id": tid,
                    "author": author or "unknown",
                    "text": text[:500],
                    "search_term": term,
                    "url": f"https://x.com/i/web/status/{tid}",
                    "tweet_datetime": tweet.date,
                    "likes": tweet.likeCount or 0,
                    "retweets": tweet.retweetCount or 0,
                    "replies": tweet.replyCount or 0,
                }
                term_count += 1

        except Exception as e:
            print(f"    Error searching {term!r}: {e}", flush=True)

        print(f"    → {term_count} new candidates", flush=True)

    return list(seen.values())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    TWSCRAPE_DB.parent.mkdir(parents=True, exist_ok=True)
    api = API(str(TWSCRAPE_DB))

    if args.setup:
        return await cmd_setup(api)

    # Verify we have active accounts
    accounts = await api.pool.get_all()
    active = [a for a in accounts if a.active]
    if not accounts:
        print(
            "ERROR: No twscrape accounts configured.\n"
            "Run: uv run python search_queue.py --setup",
            file=sys.stderr,
        )
        return 1
    if not active:
        print(
            f"WARNING: {len(accounts)} account(s) configured but none active. "
            "Attempting re-login...",
            flush=True,
        )
        await api.pool.login_all()
        active = [a for a in await api.pool.get_all() if a.active]
        if not active:
            print("ERROR: Login failed. Check credentials with --setup.", file=sys.stderr)
            return 1

    print(f"twscrape: {len(active)}/{len(accounts)} account(s) active", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    with get_conn() as conn:
        engaged_ids = get_engaged_tweet_ids(conn)
        term_stats = get_search_term_stats(conn)

        # Select terms to search
        if args.all:
            terms = list(SEARCH_TERMS)
            print(f"Searching all {len(terms)} terms...", flush=True)
        else:
            n = args.n or DEFAULT_SAMPLE_SIZE
            terms = weighted_sample_terms(
                SEARCH_TERMS, term_stats, min(n, len(SEARCH_TERMS))
            )
            print(f"Searching {len(terms)} weighted terms...", flush=True)

        candidates = await search_terms(
            api,
            terms,
            engaged_ids,
            hours_back=args.hours,
            limit_per_term=args.limit,
        )

        print(f"\nFound {len(candidates)} fresh candidates total", flush=True)

        if candidates:
            inserted = insert_candidate_queue(conn, candidates)
            print(f"Inserted {inserted} new candidates into queue", flush=True)

        size = queue_size(conn)
        print(
            f"Queue: {size} unprocessed candidates ready for engagement flow",
            flush=True,
        )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fill Twitter engagement queue via twscrape (no browser needed)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Add Twitter account credentials interactively",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Search all SEARCH_TERMS (default: weighted sample)",
    )
    parser.add_argument(
        "-n",
        type=int,
        metavar="N",
        help=f"Number of terms to sample (default: {DEFAULT_SAMPLE_SIZE})",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=SEARCH_HOURS_BACK,
        metavar="H",
        help=f"Look back N hours (default: {SEARCH_HOURS_BACK})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=RESULTS_PER_TERM,
        metavar="N",
        help=f"Max results per term (default: {RESULTS_PER_TERM})",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
