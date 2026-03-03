#!/usr/bin/env python3
"""Append a structured recentPosts entry to DB kv_state.

Why: keep duplicate detection reliable by storing the actual link when we post.

Usage:
  log_recent_post.py --type tweet --text "..." --link "https://..." [--tweet-id 123]

"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import get_conn, kv_get_json, kv_set_json

KV_RECENT_POSTS = "twitter:recent_posts"
KV_LAST_POST = "twitter:last_post"
MAX_RECENT_POSTS = 500


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", required=True, help="tweet|reply|like|engagement|value-drop|teaser|repo-update")
    ap.add_argument("--text", required=True)
    ap.add_argument("--link", default=None)
    ap.add_argument("--tweet-id", dest="tweet_id", default=None)
    args = ap.parse_args()
    now = utc_now()

    entry: dict = {
        "date": now[:10],
        "type": args.type,
        "text": args.text,
    }
    if args.link:
        entry["link"] = args.link
    if args.tweet_id:
        entry["tweetId"] = args.tweet_id

    with get_conn() as conn:
        recent = kv_get_json(conn, KV_RECENT_POSTS, [])
        if not isinstance(recent, list):
            recent = []
        recent.append(entry)
        if len(recent) > MAX_RECENT_POSTS:
            recent = recent[-MAX_RECENT_POSTS:]
        kv_set_json(conn, KV_RECENT_POSTS, recent)

        if args.type in {"tweet", "reply", "value-drop", "teaser", "repo-update"}:
            kv_set_json(conn, KV_LAST_POST, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
