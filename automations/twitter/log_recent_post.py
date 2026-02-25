#!/usr/bin/env python3
"""Append a structured recentPosts entry to twitter-state.json.

Why: keep duplicate detection reliable by storing the actual link when we post.

Usage:
  log_recent_post.py --type tweet --text "..." --link "https://..." [--tweet-id 123]

Env:
  TWITTER_STATE=/home/openclaw/clawd/memory/twitter-state.json
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", required=True, help="tweet|reply|like|engagement|value-drop|teaser|repo-update")
    ap.add_argument("--text", required=True)
    ap.add_argument("--link", default=None)
    ap.add_argument("--tweet-id", dest="tweet_id", default=None)
    ap.add_argument("--state", default=os.environ.get("TWITTER_STATE", "/home/openclaw/clawd/memory/twitter-state.json"))
    args = ap.parse_args()

    state_path = Path(args.state)
    if not state_path.exists():
        raise SystemExit(f"state file not found: {state_path}")

    state = json.loads(state_path.read_text())
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

    state.setdefault("recentPosts", [])
    state["recentPosts"].append(entry)

    # Keep lastPost updated for real posts/replies.
    if args.type in {"tweet", "reply", "value-drop", "teaser", "repo-update"}:
        state["lastPost"] = now

    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=False) + "\n")
    tmp.replace(state_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
