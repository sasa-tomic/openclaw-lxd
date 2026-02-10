#!/usr/bin/env python3
"""Execute pre-approved engagement queue for @DecentCloud_org.

Consumes:
- /home/openclaw/clawd/memory/twitter-approved-engagement-queue.json

Default behavior per approved item:
- REPLY via `bird reply`
- Random 5–60s delay between actions and between items
- Dedupe: skip if already engaged recently
- Per-author consistency: avoid near-duplicate question patterns to the same author (best-effort)

Note: `bird` currently does NOT support a "like" mutation command. When asked to LIKE+REPLY,
this executor will reply (and log the reply) but will mark like as skipped.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

twitter_state_path = Path("/home/openclaw/clawd/memory/twitter-state.json")
QUEUE_PATH = Path("/home/openclaw/clawd/memory/twitter-approved-engagement-queue.json")
HUMANIZE = Path("/projects/automations/text/humanize.py")
LOG_RECENT = Path("/projects/automations/twitter/log_recent_post.py")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def jitter_sleep() -> None:
    time.sleep(random.randint(5, 60))


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def bird_read(tweet_id: str) -> dict:
    r = run(["bird", "read", "--json", tweet_id], timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"bird read failed: {r.stderr.strip() or r.stdout.strip()}")
    return json.loads(r.stdout)


def humanize(text: str) -> str:
    p = subprocess.run(
        [sys.executable, str(HUMANIZE)],
        input=text,
        text=True,
        capture_output=True,
        timeout=20,
    )
    if p.returncode != 0:
        raise RuntimeError(f"humanize failed: {p.stderr.strip()}")
    return p.stdout.strip()


def log_recent(kind: str, text: str, link: str, tweet_id: str) -> None:
    # keep these calls best-effort
    run([
        sys.executable,
        str(LOG_RECENT),
        "--type",
        kind,
        "--text",
        text,
        "--link",
        link,
        "--tweet-id",
        tweet_id,
    ], timeout=30)


def normalize_tokens(s: str) -> set[str]:
    s = s.lower()
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"[^a-z0-9_\s/-]", " ", s)
    toks = {t for t in s.split() if len(t) >= 3}
    # drop super-common words
    stop = {"the", "and", "for", "with", "that", "this", "what", "your", "you", "are", "was", "but"}
    return {t for t in toks if t not in stop}


def too_similar(prev_texts: list[str], candidate: str, min_overlap: int = 4) -> bool:
    cand = normalize_tokens(candidate)
    if not cand:
        return False
    for p in prev_texts:
        prev = normalize_tokens(p)
        if len(cand & prev) >= min_overlap:
            return True
    return False


def main() -> int:
    print("=== TWITTER APPROVED QUEUE (EXECUTE via bird) ===")
    print(f"Time: {utc_now()}")

    if not QUEUE_PATH.exists():
        print("Queue missing; no-op")
        return 0

    queue = json.loads(QUEUE_PATH.read_text())
    state = json.loads(twitter_state_path.read_text())

    engaged: set[str] = {e.get("tweetId") for e in state.get("engagedPosts", []) if e.get("tweetId")}
    for rp in state.get("recentPosts", []):
        link = (rp.get("link") or "")
        if "/status/" in link:
            engaged.add(link.split("/status/")[-1].split("?")[0])

    # Per-author prior reply text (best effort: parse @handle from recentPosts text)
    prev_replies_by_author: dict[str, list[str]] = {}
    for rp in state.get("recentPosts", []):
        if rp.get("type") != "reply":
            continue
        t = rp.get("text", "")
        m = re.search(r"@([A-Za-z0-9_]{2,})", t)
        if not m:
            continue
        author = m.group(1).lower()
        prev_replies_by_author.setdefault(author, []).append(t)

    # Pick first 1–2 approved
    selected_idx: list[int] = []
    for i, item in enumerate(queue):
        if item.get("status") == "approved":
            selected_idx.append(i)
        if len(selected_idx) >= 2:
            break

    if not selected_idx:
        print("no-op")
        return 0

    results: list[str] = []

    for i in selected_idx:
        item = queue[i]
        tweet_id = str(item.get("tweetId") or "").strip()
        url = str(item.get("url") or f"https://x.com/i/web/status/{tweet_id}").strip()
        draft = str(item.get("reply") or "").strip()

        if not tweet_id:
            item["status"] = "skipped"
            item["skippedAt"] = utc_now()
            item["skipReason"] = "missing tweetId"
            continue

        if tweet_id in engaged:
            item["status"] = "skipped"
            item["skippedAt"] = utc_now()
            item["skipReason"] = "already engaged recently"
            continue

        if not draft:
            item["status"] = "skipped"
            item["skippedAt"] = utc_now()
            item["skipReason"] = "empty reply draft"
            continue

        # Fetch author to enforce per-author consistency
        try:
            tweet = bird_read(tweet_id)
            author = (tweet.get("author", {}) or {}).get("username")
            author_key = (author or "").lower()
        except Exception as e:
            item["status"] = "skipped"
            item["skippedAt"] = utc_now()
            item["skipReason"] = f"bird read failed: {e}"
            continue

        prev_texts = prev_replies_by_author.get(author_key, [])
        if author_key and too_similar(prev_texts, draft):
            item["status"] = "skipped"
            item["skippedAt"] = utc_now()
            item["skipReason"] = f"per-author consistency: too similar to recent replies for @{author_key}"
            continue

        # Humanize
        try:
            reply_text = humanize(draft)
        except Exception as e:
            item["status"] = "skipped"
            item["skippedAt"] = utc_now()
            item["skipReason"] = f"humanize failed: {e}"
            continue

        # LIKE step: bird doesn't support it
        item["like"] = "skipped"
        item["likeSkippedAt"] = utc_now()
        item["likeSkipReason"] = "bird has no like command"

        # Reply
        jitter_sleep()
        r = run(["bird", "reply", tweet_id, reply_text], timeout=90)
        if r.returncode != 0:
            item["status"] = "skipped"
            item["skippedAt"] = utc_now()
            item["skipReason"] = f"bird reply failed: {r.stderr.strip() or r.stdout.strip()}"
            continue

        item["status"] = "done"
        item["doneAt"] = utc_now()

        # Logging
        log_recent("reply", f"Replied to @{author_key} ({tweet_id}).", url, tweet_id)

        # Update in-memory guards so we don't double-act within the same run
        engaged.add(tweet_id)
        if author_key:
            prev_replies_by_author.setdefault(author_key, []).append(reply_text)

        results.append(f"{tweet_id} | {url} | reply")

        jitter_sleep()

    QUEUE_PATH.write_text(json.dumps(queue, indent=2, ensure_ascii=False) + "\n")

    if results:
        print("\n".join(results))
    else:
        print("no-op")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
