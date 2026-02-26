#!/usr/bin/env python3
"""Shared utilities for Twitter automation scripts.

Consolidates common functions used across multiple Twitter automation scripts.
All Twitter interactions go through Chrome CDP via CDPSession (direct WebSocket).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from email.utils import parsedate_to_datetime as _parse_twitter_date
from urllib.parse import quote

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import OPENCLAW_BIN, TWITTER_BASE_URL

from cdp import CDPSession

THREAD_INDEX_PATH = Path("/home/openclaw/clawd/memory/twitter-thread-index.json")
STRATEGY_PATH = Path("/projects/automations/twitter/STRATEGY.md")
HUMANIZE_SCRIPT = Path("/projects/automations/text/humanize.py")

CDP_LOCK_PATH = Path("/tmp/twitter-cdp.lock")
CDP_LOCK_TIMEOUT = 300  # seconds


@contextlib.contextmanager
def cdp_lock():
    """File-based lock to serialize CDP tab access across Prefect flows.

    Multiple flows (reply-monitor, engagement, health check) share the same
    Chrome tab. Without locking, concurrent navigations corrupt each other.

    Uses fcntl.flock() — non-blocking first, then blocks with 1s retry loop.
    """
    lock_file = open(CDP_LOCK_PATH, "w")
    try:
        # Try non-blocking first
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("  Waiting for CDP lock (another flow is running)...", flush=True)
            start = time.monotonic()
            while True:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    elapsed = time.monotonic() - start
                    print(f"  CDP lock acquired after {elapsed:.0f}s", flush=True)
                    break
                except BlockingIOError:
                    if time.monotonic() - start > CDP_LOCK_TIMEOUT:
                        raise TimeoutError(
                            f"CDP lock timeout after {CDP_LOCK_TIMEOUT}s"
                        )
                    time.sleep(1)
        yield
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


_PROJECT_CONTEXT_CACHE: dict = {}  # {mtime: float, content: str}


def load_project_context() -> str:
    """Load the Twitter strategy / project context from STRATEGY.md.

    This is the single source of truth for all LLM prompts — positioning,
    phase rules, target audience, voice. Update STRATEGY.md to change what
    every LLM call knows about the project.

    Memoized per process: reads the file at most once per run unless the
    file is modified between calls (checked via mtime).
    """
    try:
        mtime = STRATEGY_PATH.stat().st_mtime
        if _PROJECT_CONTEXT_CACHE.get("mtime") == mtime:
            return _PROJECT_CONTEXT_CACHE["content"]
        content = STRATEGY_PATH.read_text().strip()
        _PROJECT_CONTEXT_CACHE["mtime"] = mtime
        _PROJECT_CONTEXT_CACHE["content"] = content
        return content
    except Exception as e:
        logger.debug(f"load_project_context failed: {e}")
        return (
            "Account: @DecentCloud_org — building a P2P cloud compute marketplace "
            "(like Airbnb for compute/GPUs). Founder voice. Phase 1: no product "
            "mentions, no links, no hashtags. Build reply reputation first."
        )


# AI bots and known noise accounts — engaging wastes quota and builds no reputation
BLOCKED_AUTHORS = {
    "grok",  # xAI's AI — won't reply back, won't follow
    "ChatGPT",
    "openai",
    "claude_ai",
    "perplexity_ai",
    "hackernoon",  # content farm, never replies
}

SKIP_PATTERNS = [
    r"\bairdrop\b",
    r"\bgiveaway\b",
    r"\bfree\s+nft\b",
    r"\b(buy|sell)\s+(my|our)\b",
    r"\bclick\s+here\b",
    r"\.sol\b",
    # Crypto / token noise — mostly irrelevant and LLM-expensive to analyze
    r"\bnft\b",
    r"\bdefi\b",
    r"\bweb3\b",
    r"\bhodl\b",
    r"\btokenomics\b",
    r"\bsolana\b",
    r"\bstaking\b",
    r"\bpresale\b",
    r"\bcoin\s+launch\b",
    r"\btoken\s+launch\b",
    # DePIN / crypto project signals not caught above
    r"\btestnet\b",
    r"\bdepin\b",
    r"\bwhitelist\b",
    r"\bplay.to.earn\b",
    r"\bearn\s+crypto\b",
    r"\$[A-Za-z]{2,10}\b",  # token tickers: $SOL $ETH $RNDR $GPU etc.
]

# ---------------------------------------------------------------------------
# Search term building blocks — max 3-4 options per clause
# ---------------------------------------------------------------------------

_CLOUD = "(aws OR gcp OR azure OR k8s OR vultr OR linode OR digitalocean OR hetzner OR contabo OR cloud)"
_SUPPORT = "(down OR broken OR unreliable OR 'no support' OR 'bad support' OR trust OR ghost OR ghosted)"
_P2P = "(runpod OR akash OR 'vast.ai' OR 'lambda labs' OR decentralized)"
_PAAS = "(fly.io OR heroku OR 'render.com' OR railway)"
_GPU = "(h100 OR a100 OR gpu OR B100 OR B300)"
_PAIN = "(terrible OR useless OR ghosted OR broken)"
_COST = "(expensive OR insane OR overpriced OR scam OR overpaying)"

# Kept for backwards-compat with generate_dynamic_keywords()
PROVIDERS = _CLOUD

SEARCH_TERMS = [
    # Provider support failures — named providers anchor to real incidents
    f"({_CLOUD} OR {_P2P} OR {_PAAS}) AND {_SUPPORT}",
    # P2P compute pain — named platforms + pain
    f"{_P2P} {_SUPPORT}",
    # Cloud/GPU cost and availability
    f"{_GPU} OR {_CLOUD} AND {_COST}",
    # Egress — the hidden charge people vent about
    f"egress {_COST}",
    # Cloud lock-in — people who feel trapped
    f'{_CLOUD} AND "lock-in"',
    # k8s / serverless cost reality
    f"(kubernetes OR serverless) AND {_COST}",
    # Cloud outages + SLA frustration
    f"{_CLOUD} AND (outage OR incident) AND (SLA OR useless OR compensation OR credit OR nightmare OR burnout OR frustrating)",
    # Cloud exit — exact phrases people actually tweet
    f'(leaving OR ditching OR "moving off") AND {_CLOUD}',
    # Cloud migration regret
    f"{_CLOUD} AND migration AND (nightmare OR regret OR painful OR stuck)",
    # Self-hosting vs cloud debate
    '(cheaper OR better OR worth it) AND ("self-hosting" OR "self-hosted")',
    # Provider accountability — direct positioning angle
    f"{_CLOUD} AND (accountability OR recourse OR responsible OR support)",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# Thread index: tweet_id → note_path (for O(1) lookup during engagement)
# ---------------------------------------------------------------------------


def load_thread_index() -> dict[str, str]:
    """Load the tweet_id → note_path index. Returns {} if not found."""
    if THREAD_INDEX_PATH.exists():
        try:
            return json.loads(THREAD_INDEX_PATH.read_text())
        except Exception as e:
            logger.debug(f"load_thread_index failed: {e}")
    return {}


def update_thread_index(tweet_ids: list[str], note_path: str) -> None:
    """Add tweet_id → note_path entries to the index (atomic write)."""
    index = load_thread_index()
    for tid in tweet_ids:
        index[str(tid)] = note_path
    THREAD_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = THREAD_INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(THREAD_INDEX_PATH)


def lookup_our_thread(tweet_ids: list[str]) -> str | None:
    """Given a list of tweet IDs, return the markdown note content if any ID
    belongs to one of our posted threads. Returns None if no match.

    Use this during engagement to give the LLM full thread context when someone
    replies to or engages with one of our posted thread tweets.
    """
    if not tweet_ids:
        return None
    index = load_thread_index()
    for tid in tweet_ids:
        note_path = index.get(str(tid))
        if note_path:
            try:
                return Path(note_path).read_text()
            except Exception as e:
                logger.debug(f"lookup_our_thread failed to read {note_path}: {e}")
                return None
    return None


ENCOUNTERED_THREADS_DIR = Path("/projects/Notes/Pickle/Twitter/encountered")


def save_encountered_thread(
    tweet_context: dict,
    decision: dict | None,
    tweet_id: str,
    search_term: str = "",
) -> Path | None:
    """Save a thread we analyzed for later analysis.

    Stores the full conversation context + LLM decision (engaged or not).
    File: /projects/Notes/Pickle/Twitter/encountered/YYYY-MM-DD-{tweet_id}-@author.md
    """
    try:
        ENCOUNTERED_THREADS_DIR.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        date_str = now.date().isoformat()
        author = tweet_context.get("author", "unknown")
        filename = f"{date_str}-{tweet_id}-@{author}.md"
        path = ENCOUNTERED_THREADS_DIR / filename

        tweet_url = f"{TWITTER_BASE_URL}/{author}/status/{tweet_id}"
        stats = tweet_context.get("stats", {})

        should_engage = decision.get("shouldEngage", False) if decision else False
        our_reply = decision.get("reply", "") if decision else ""
        reasoning = decision.get("reasoning", "") if decision else "No LLM decision"

        engagement_status = "ENGAGED" if should_engage else "SKIPPED"

        profile = tweet_context.get("authorProfile")
        profile_bio = profile.get("bio", "") if profile else ""
        profile_followers = profile.get("followersCount", 0) if profile else 0

        lines = [
            "---",
            f"date: {date_str}",
            f"author: @{author}",
            f'tweetId: "{tweet_id}"',
            f'url: "{tweet_url}"',
            f"likes: {stats.get('likes', 0)}",
            f"retweets: {stats.get('retweets', 0)}",
            f"replies: {stats.get('replies', 0)}",
            f"engagement: {engagement_status}",
            f'searchTerm: "{search_term}"',
            f"authorFollowers: {profile_followers}",
            "tags: [twitter, engagement, encountered]",
            "---",
            "",
            f"# @{author}: {tweet_context.get('text', '')[:60]}...",
            "",
            f"*Encountered: {date_str} | Status: **{engagement_status}***",
            "",
            "---",
            "",
            "## Original Tweet",
            "",
            f"**@{author}** ({tweet_context.get('authorName', '')})",
            "",
            f"> {tweet_context.get('text', '')}",
            "",
            f"*Stats: {stats.get('likes', 0)} likes, {stats.get('retweets', 0)} RTs, {stats.get('replies', 0)} replies*",
            "",
        ]

        if profile:
            lines += [
                "---",
                "",
                "## Author Profile",
                "",
            ]
            if profile.get("bio"):
                lines.append(f"**Bio:** {profile['bio']}")
                lines.append("")
            if profile.get("location"):
                lines.append(f"**Location:** {profile['location']}")
                lines.append("")
            if profile.get("followersCount"):
                lines.append(f"**Followers:** {profile['followersCount']:,}")
                lines.append("")
            if profile.get("recentTweets"):
                lines.append("**Recent tweets:**")
                lines.append("")
                for i, t in enumerate(profile["recentTweets"][:5], 1):
                    lines.append(f"{i}. {t[:150]}")
                lines.append("")

        if tweet_context.get("parentChain"):
            lines += [
                "---",
                "",
                "## Conversation Context (tweets above)",
                "",
            ]
            for p in tweet_context["parentChain"]:
                p_user = p.get("username", "?")
                p_text = p.get("text", "")
                lines.append(f"**@{p_user}:**")
                lines.append(f"> {p_text}")
                lines.append("")

        if tweet_context.get("threadContinuation"):
            lines += [
                "---",
                "",
                "## Thread Continuation (author's follow-ups)",
                "",
            ]
            for t in tweet_context["threadContinuation"]:
                t_text = t.get("text", "") if isinstance(t, dict) else str(t)
                lines.append(f"> {t_text}")
                lines.append("")

        if tweet_context.get("otherReplies"):
            lines += [
                "---",
                "",
                "## Other Replies",
                "",
            ]
            for r in tweet_context["otherReplies"][:5]:
                r_user = r.get("username", "?")
                r_text = r.get("text", "")
                lines.append(f"**@{r_user}:** {r_text}")
                lines.append("")

        if our_reply:
            lines += [
                "---",
                "",
                "## Our Reply",
                "",
                f"> {our_reply}",
                "",
            ]

        lines += [
            "---",
            "",
            "## LLM Decision",
            "",
            f"**Should engage:** {should_engage}",
            "",
            "### Reasoning",
            "",
            reasoning,
            "",
        ]

        path.write_text("\n".join(lines))
        return path

    except Exception as e:
        print(f"  WARNING: Failed to save encountered thread: {e}", flush=True)
        return None


def send_error_alert(message: str) -> None:
    try:
        r = subprocess.run(
            [
                "/home/openclaw/.npm-global/bin/openclaw",
                "message",
                "send",
                "--channel",
                "telegram",
                "--target",
                "5996479639",
                "--message",
                f"🚨 Twitter Engagement Error\n\n{message}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            print(
                f"  WARNING: Telegram alert failed: {r.stderr.strip()[:200]}",
                flush=True,
            )
    except Exception as e:
        print(f"  WARNING: Telegram alert failed: {e}", flush=True)


def is_junk(text: str) -> bool:
    text_lower = text.lower()
    for pattern in SKIP_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True
    return False


def jitter_sleep(min_sec: int = 30, max_sec: int = 120) -> None:
    delay = random.randint(min_sec, max_sec)
    print(f"  Waiting {delay}s...", flush=True)
    time.sleep(delay)


def humanize(text: str) -> str:
    p = subprocess.run(
        [sys.executable, str(HUMANIZE_SCRIPT)],
        input=text,
        text=True,
        capture_output=True,
        timeout=20,
    )
    if p.returncode != 0:
        raise RuntimeError(f"humanize failed: {p.stderr.strip()}")
    return p.stdout.strip()


def post_reply(
    tweet_id: str,
    reply_text: str,
    our_username: str = "DecentCloud_org",
) -> tuple[bool, str | None]:
    """Post a reply via Chrome CDP.

    Returns (success, reply_tweet_id).  reply_tweet_id is the Snowflake ID of
    the reply we just posted, extracted from the thread DOM while still on the
    page — no extra navigation needed.  It is None only when the reply was
    posted but the ID couldn't be read from the page (rare; treated as success).
    """
    # JS that finds the highest /our_username/status/<ID> link on the page,
    # i.e. our freshly posted reply (must be > tweet_id since IDs are monotonic).
    _extract_js = (
        "(() => {"
        f"  const pat = /\\/{our_username}\\/status\\/(\\d+)$/;"
        "  let best = null;"
        "  for (const a of document.querySelectorAll('a[href]')) {"
        "    const m = a.href.match(pat);"
        "    if (m && (!best || BigInt(m[1]) > BigInt(best))) best = m[1];"
        "  }"
        "  return best;"
        "})()"
    )

    try:
        with cdp_lock():
            tweet_url = f"{TWITTER_BASE_URL}/i/web/status/{tweet_id}"
            print(f"  CDP: navigating to {tweet_url}", flush=True)

            try:
                with CDPSession.connect() as cdp:
                    if not cdp.navigate(tweet_url, wait_sec=2):
                        return False, None
                    TEXTAREA = '[data-testid="tweetTextarea_0"]'
                    REPLY_BTN = '[data-testid="tweetButtonInline"]'
                    if not cdp.wait_for(TEXTAREA, timeout=10):
                        print("  CDP: reply textbox not found", flush=True)
                        return False, None
                    # Click to focus/expand the reply box
                    cdp.click(TEXTAREA)
                    time.sleep(0.5)
                    print(f"  CDP: typing reply...", flush=True)
                    if not cdp.type_text(TEXTAREA, reply_text):
                        print("  CDP: typing failed", flush=True)
                        return False, None
                    time.sleep(0.5)
                    print(f"  CDP: clicking Reply...", flush=True)
                    if not cdp.click(REPLY_BTN):
                        print("  CDP: Reply button not found", flush=True)
                        return False, None
                    # Poll up to 8s for textarea to clear (reply submitted → div resets)
                    deadline = time.time() + 8
                    while time.time() < deadline:
                        still_text = cdp.evaluate(f'document.querySelector({json.dumps(TEXTAREA)})?.textContent?.trim()')
                        if not still_text:
                            # Give Twitter a moment to surface any error toast before
                            # declaring success (errors also clear the textarea).
                            time.sleep(1.2)
                            toast = cdp.evaluate(
                                'document.querySelector(\'[data-testid="toast"]\')?.textContent?.trim()'
                            )
                            if toast:
                                print(f"  CDP: Twitter error toast detected: {toast[:120]}", flush=True)
                                return False, None
                            # Extract our reply ID from the thread DOM (already on this page).
                            # The new reply renders immediately; its ID must be > tweet_id.
                            reply_id: str | None = None
                            for _ in range(5):
                                raw = cdp.evaluate(_extract_js)
                                candidate = str(raw) if raw else None
                                if candidate and int(candidate) > int(tweet_id):
                                    reply_id = candidate
                                    break
                                time.sleep(1)
                            if reply_id:
                                print(f"  CDP: reply posted (id={reply_id})", flush=True)
                            else:
                                print("  CDP: reply posted (could not read ID from page)", flush=True)
                            return True, reply_id
                        time.sleep(1)
                    print("  CDP: reply may have failed (textarea still has text)", flush=True)
                    return False, None
            except Exception as e:
                logger.warning(f"post_reply CDP failed: {e}")
                return False, None

    except Exception as e:
        print(f"  CDP reply exception: {e}", flush=True)
        return False, None


def post_tweet(text: str) -> bool:
    """Post a new tweet via Chrome CDP (CDPSession direct WebSocket).

    Flow: navigate to compose → find textbox → type → click Post.
    """
    try:
        with cdp_lock():
            compose_url = f"{TWITTER_BASE_URL}/compose/post"
            print("  CDP: navigating to compose...", flush=True)

            try:
                with CDPSession.connect() as cdp:
                    if not cdp.navigate(compose_url, wait_sec=2):
                        return False
                    TEXTAREA = '[data-testid="tweetTextarea_0"]'
                    POST_BTN = '[data-testid="tweetButton"]'
                    if not cdp.wait_for(TEXTAREA, timeout=10):
                        print("  CDP: compose textbox not found", flush=True)
                        return False
                    print(f"  CDP: typing tweet...", flush=True)
                    if not cdp.type_text(TEXTAREA, text):
                        print("  CDP: typing failed", flush=True)
                        return False
                    time.sleep(0.5)
                    print(f"  CDP: clicking Post...", flush=True)
                    if not cdp.click(POST_BTN):
                        print("  CDP: Post button not found", flush=True)
                        return False
                    # Poll up to 8s for compose to close (textarea gone or empty)
                    deadline = time.time() + 8
                    while time.time() < deadline:
                        still_text = cdp.evaluate(f'document.querySelector({json.dumps(TEXTAREA)})?.textContent?.trim()')
                        if not still_text:
                            print("  CDP: tweet posted successfully", flush=True)
                            return True
                        time.sleep(1)
                    print("  CDP: post may have failed (compose still open)", flush=True)
                    return False
            except Exception as e:
                logger.warning(f"post_tweet CDP failed: {e}")
                return False

    except Exception as e:
        print(f"  CDP tweet exception: {e}", flush=True)
        return False


def _extract_graphql_tweet_node(node: dict) -> dict | None:
    """Extract a tweet dict from a Twitter GraphQL result node.

    Handles both direct Tweet nodes and the newer TweetWithVisibilityResults wrapper.
    Returns None for non-tweet nodes (cursors, users, etc.).
    """
    typename = node.get("__typename", "")
    if typename == "TweetWithVisibilityResults":
        inner = node.get("tweet")
        if isinstance(inner, dict):
            return _extract_graphql_tweet_node(inner)
        return None
    if typename != "Tweet":
        return None
    try:
        core = node.get("core", {})
        user_result = core.get("user_results", {}).get("result", {})
        # screen_name is under result.core in newer API responses; fall back to legacy
        user_core = user_result.get("core", {})
        legacy_user = user_result.get("legacy", {})
        username = (
            user_core.get("screen_name")
            or legacy_user.get("screen_name")
            or ""
        )
        legacy = node.get("legacy", {})
        tweet_id = legacy.get("id_str") or node.get("rest_id", "")
        text = legacy.get("full_text", "")
        created_at = legacy.get("created_at", "")
        # Twitter's legacy date format ("Thu Feb 27 12:00:00 +0000 2025") is not
        # ISO 8601 — normalize it so the downstream fromisoformat() recency check works.
        try:
            created_at = _parse_twitter_date(created_at).isoformat()
        except Exception:
            pass  # keep original if parsing fails; server-side since_time handles freshness
        likes = legacy.get("favorite_count", 0)
        retweets = legacy.get("retweet_count", 0)
        replies = legacy.get("reply_count", 0)
        if tweet_id:
            return {
                "tweetId": tweet_id,
                "username": username,
                "text": text[:500],
                "datetime": created_at,
                "likes": likes,
                "retweets": retweets,
                "replies": replies,
            }
    except Exception:
        pass
    return None


def _parse_graphql_search_body(body: str) -> list[dict]:
    """Walk a raw SearchTimeline GraphQL response body and return tweet dicts."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return []

    tweets: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            t = _extract_graphql_tweet_node(node)
            if t is not None:
                tweets.append(t)
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return tweets


def _cdp_search(
    term: str, limit: int = 15, since_dt: datetime | None = None
) -> list[dict]:
    """Search Twitter for a term by intercepting its SearchTimeline GraphQL responses.

    Rather than scraping the virtualised DOM (~15-20 visible articles), this enables
    CDP Network events and reads the raw API responses that Twitter itself uses.
    Each scroll triggers a new paginated request, typically yielding 20 tweets per
    response and 5+ responses per search (100+ tweets vs the old ~15-20 cap).

    Must be called while holding cdp_lock().
    """
    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=SEARCH_CACHE_TTL_HOURS)
    since_ts = int(since_dt.timestamp())
    encoded = quote(f"{term} since_time:{since_ts}")
    url = f"{TWITTER_BASE_URL}/search?q={encoded}&f=live"

    bodies: list[str] = []
    pending_ids: dict[str, bool] = {}   # requestId -> True for in-flight SearchTimeline reqs
    _lock = threading.Lock()
    _shutdown = threading.Event()

    def _fetch_body(cdp, req_id: str) -> None:
        """Called from a worker thread — safe to call cdp.send() here."""
        try:
            resp = cdp.send("Network.getResponseBody", {"requestId": req_id}, timeout=15)
            body = resp.get("result", {}).get("body") or resp.get("body", "")
            if body:
                with _lock:
                    bodies.append(body)
        except Exception as e:
            logger.debug(f"_cdp_search getResponseBody {req_id}: {e}")

    try:
        with CDPSession.connect() as cdp:
            executor = ThreadPoolExecutor(max_workers=4)

            cdp.send("Network.enable")

            def on_response(params: dict) -> None:
                resp_url = params.get("response", {}).get("url", "")
                req_id = params.get("requestId", "")
                if req_id and "SearchTimeline" in resp_url:
                    with _lock:
                        pending_ids[req_id] = True

            def on_loading_finished(params: dict) -> None:
                if _shutdown.is_set():
                    return
                req_id = params.get("requestId", "")
                with _lock:
                    if req_id not in pending_ids:
                        return
                    del pending_ids[req_id]
                try:
                    executor.submit(_fetch_body, cdp, req_id)
                except RuntimeError:
                    pass  # executor already shut down

            cdp.on("Network.responseReceived", on_response)
            cdp.on("Network.loadingFinished", on_loading_finished)

            if not cdp.navigate(url, wait_sec=5):
                _shutdown.set()
                executor.shutdown(wait=False)
                return []

            # Each scroll fires a new paginated SearchTimeline request
            for _ in range(5):
                cdp.scroll_to_bottom()
                time.sleep(2)

            # Signal handlers to stop submitting, then drain the executor
            _shutdown.set()
            executor.shutdown(wait=True)

    except Exception as e:
        logger.warning(f"_cdp_search CDP failed: {e}")
        return []

    # Deduplicate across paginated responses
    tweets: list[dict] = []
    seen_ids: set[str] = set()
    for body in bodies:
        for t in _parse_graphql_search_body(body):
            tid = t["tweetId"]
            if tid not in seen_ids:
                seen_ids.add(tid)
                tweets.append(t)

    return tweets[:limit] if limit else tweets


def _term_weight(stats: dict) -> float:
    """Compute sampling weight for a search term from its accumulated stats.

    Higher weight = more likely to be selected next run.
    Two signals, on different timescales:
    - Hit rate (fast): candidates seen vs engagements sent — available after 1 run
    - Reply performance (slow): likes/replies on our replies — needs 24h+ to accumulate

    Returns a float in [0.1, ∞). Never zero so no term is permanently excluded.
    """
    if not stats:
        return 1.0  # neutral prior for terms with no data yet

    candidates = stats.get("candidates", 0)
    engaged = stats.get("engaged", 0)
    perf_measured = stats.get("perfMeasured", 0)
    total_likes = stats.get("totalLikes", 0)
    zero_perf = stats.get("zeroPerfReplies", 0)

    score = 1.0

    # Hit rate signal — fast feedback, available after each run
    if candidates >= 10:  # need at least 10 candidates before judging
        hit_rate = engaged / candidates
        if hit_rate >= 0.10:  # >=10% candidates worth engaging
            score *= 2.5
        elif hit_rate >= 0.05:  # 5-10%
            score *= 1.5
        elif hit_rate == 0:  # never produced an engagement despite volume
            score *= 0.3

    # Reply performance signal — slower, needs 24h+ for likes to land
    if perf_measured >= 2:
        avg_likes = total_likes / perf_measured
        zero_rate = zero_perf / perf_measured
        if avg_likes >= 2:
            score *= 2.0
        elif avg_likes >= 1:
            score *= 1.5
        if zero_rate >= 0.8:  # 80%+ of replies got nothing back
            score *= 0.5

    return max(score, 0.1)  # floor: never fully exclude a term


def weighted_sample_terms(all_terms: list[str], term_stats: dict, n: int) -> list[str]:
    """Sample n unique terms without replacement, weighted by performance stats.

    Terms with no data get a neutral prior (weight=1.0).
    Terms with good hit rate or reply performance get higher weight.
    Terms that consistently produce nothing get lower weight but are never excluded.
    """
    terms = list(all_terms)
    weights = [_term_weight(term_stats.get(t, {})) for t in terms]
    selected: list[str] = []
    while len(selected) < n and terms:
        total = sum(weights)
        if total <= 0:
            break
        r = random.uniform(0, total)
        cumulative = 0.0
        for i, w in enumerate(weights):
            cumulative += w
            if r <= cumulative:
                selected.append(terms.pop(i))
                weights.pop(i)
                break
    return selected


def search_candidates(
    terms: list[str] | None = None,
    limit: int = 20,
    term_stats: dict | None = None,
    bypass_cache: bool = False,
    since_hours: int | None = None,
) -> list[dict]:
    """Search for engagement candidates via Chrome CDP.

    Results are cached per search term with a 6h TTL so consecutive runs
    (every 3h) can reuse results without re-navigating Twitter search pages.
    Cache misses fall back to CDP search and populate the cache.

    bypass_cache: skip the shared cache entirely; always do a live search.
        Used by search_queue.py so it gets fresh results every hour without
        polluting the engagement flow's cache or being blocked by it.
    since_hours: when bypass_cache=True, look this far back (default 2h).
        Ignored when bypass_cache=False (cache's own timestamp is used instead).
    """
    candidates = []
    if terms is not None:
        search_terms = terms
    elif term_stats is not None:
        search_terms = weighted_sample_terms(
            SEARCH_TERMS, term_stats, min(12, len(SEARCH_TERMS))
        )
    else:
        search_terms = random.sample(SEARCH_TERMS, min(12, len(SEARCH_TERMS)))

    if bypass_cache:
        terms_to_fetch = list(search_terms)
        print(
            f"  CDP: bypass_cache — live search for {len(terms_to_fetch)} term(s)...",
            flush=True,
        )
    else:
        search_cache = _load_search_cache()
        terms_to_fetch: list[str] = []
        cache_hits = 0

        # Serve cached results immediately; collect terms that need a live search
        for term in search_terms:
            cached = _get_cached_search(search_cache, term)
            if cached is not None:
                cache_hits += 1
                candidates.extend(cached)
            else:
                terms_to_fetch.append(term)

        if cache_hits:
            print(
                f"  CDP: {cache_hits}/{len(search_terms)} search term(s) served from cache",
                flush=True,
            )

        if not terms_to_fetch:
            print(
                f"  CDP: all {len(search_terms)} terms cached — skipping live search",
                flush=True,
            )
            return candidates

    with cdp_lock():
        if not bypass_cache:
            print(
                f"  CDP: searching {len(terms_to_fetch)} uncached term(s)...",
                flush=True,
            )

        cache_updated = False
        _fixed_since_dt: datetime | None = None
        if bypass_cache:
            _fixed_since_dt = datetime.now(timezone.utc) - timedelta(
                hours=(since_hours if since_hours is not None else 2)
            )

        for term in terms_to_fetch:
            if bypass_cache:
                since_dt = _fixed_since_dt
            else:
                # Use the last time we searched this term as the since cutoff.
                # Falls back to SEARCH_CACHE_TTL_HOURS ago for first-time queries.
                raw_entry = search_cache.get(term.lower().strip())
                since_dt: datetime
                if raw_entry and raw_entry.get("cachedAt"):
                    try:
                        since_dt = datetime.fromisoformat(
                            raw_entry["cachedAt"].replace("Z", "+00:00")
                        )
                    except (ValueError, TypeError):
                        logger.debug(
                            f"search_candidates cache date parse failed for '{term}'"
                        )
                        since_dt = datetime.now(timezone.utc) - timedelta(
                            hours=SEARCH_CACHE_TTL_HOURS
                        )
                else:
                    since_dt = datetime.now(timezone.utc) - timedelta(
                        hours=SEARCH_CACHE_TTL_HOURS
                    )

            try:
                tweets = _cdp_search(term, limit=limit, since_dt=since_dt)
                term_candidates = [
                    {
                        "tweetId": t.get("tweetId"),
                        "author": t.get("username") or "unknown",
                        "text": t.get("text") or "",
                        "searchTerm": term,
                        "url": f"https://x.com/i/web/status/{t.get('tweetId')}",
                        "datetime": t.get("datetime"),
                        "likes": t.get("likes", 0),
                        "retweets": t.get("retweets", 0),
                        "replies": t.get("replies", 0),
                    }
                    for t in tweets
                ]

                # Client-side recency check as a safety net against the query operator
                fresh_candidates = []
                stale = 0
                for c in term_candidates:
                    dt_str = c.get("datetime")
                    if dt_str:
                        try:
                            if (
                                datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                                < since_dt
                            ):
                                stale += 1
                                continue
                        except (ValueError, TypeError):
                            pass  # Malformed date — keep candidate anyway
                    fresh_candidates.append(c)

                hit_count = len(fresh_candidates)
                stale_note = f", {stale} stale dropped" if stale else ""
                since_note = f" since:{since_dt.strftime('%H:%M UTC')}" if since_dt else ""
                print(
                    f"  CDP: '{term}'{since_note} => {hit_count} hits{stale_note}",
                    flush=True,
                )

                candidates.extend(fresh_candidates)
                if not bypass_cache:
                    search_cache[term.lower().strip()] = {
                        "cachedAt": utc_now(),
                        "results": fresh_candidates,
                    }
                    cache_updated = True
            except Exception as e:
                print(f"  CDP search error for '{term}': {e}", flush=True)

        if not bypass_cache and cache_updated:
            _save_search_cache(search_cache)

    print(f"  CDP: {len(candidates)} fresh candidates across all terms", flush=True)
    return candidates


def fetch_tweet_context(tweet_id: str) -> dict | None:
    """Fetch tweet context (text, author, stats, full thread) via Chrome CDP.

    Returns a dict with:
    - author, authorName, text, stats, quotedTweet
    - threadContinuation: author's own follow-up tweets (self-replies below)
    - otherReplies: visible replies from OTHER users (up to 5)
    - parentChain: tweets this tweet is replying to (conversation ancestry)
    - replyTo: the direct parent tweet (last item in parentChain), if any

    Uses CDPSession (direct WebSocket).
    """
    tweet_url = f"https://x.com/i/web/status/{tweet_id}"

    # Extract tweet data via JavaScript.
    # Finds the main tweet by tweet_id match (handles replies where the main tweet
    # isn't articles[0]), captures parent chain (above), thread continuation
    # (author's self-replies below), and other visible replies from other users.
    js = f"""(() => {{
  const targetId = '{tweet_id}';
  const articles = document.querySelectorAll('article[data-testid="tweet"]');
  if (!articles.length) return JSON.stringify(null);

  function getUsername(article) {{
    const userLinks = article.querySelectorAll('a[role="link"]');
    for (const ul of userLinks) {{
      const m = ul.href.match(/^https:\\/\\/x\\.com\\/([^/]+)$/);
      if (m && m[1] !== 'search' && m[1] !== 'explore' && m[1] !== 'i') return m[1];
    }}
    return null;
  }}

  function getDisplayName(article) {{
    const userLinks = article.querySelectorAll('a[role="link"]');
    for (const ul of userLinks) {{
      const m = ul.href.match(/^https:\\/\\/x\\.com\\/([^/]+)$/);
      if (m && m[1] !== 'search' && m[1] !== 'explore' && m[1] !== 'i') {{
        const nameEl = ul.querySelector('span');
        return nameEl ? nameEl.textContent : null;
      }}
    }}
    return null;
  }}

  function getStats(article) {{
    let likes = 0, retweets = 0, replies = 0;
    const buttons = article.querySelectorAll('button');
    for (const btn of buttons) {{
      const label = (btn.getAttribute('aria-label') || '').toLowerCase();
      const likeM = label.match(/(\\d+)\\s+like/);
      if (likeM) likes = parseInt(likeM[1]);
      const rtM = label.match(/(\\d+)\\s+repost/);
      if (rtM) retweets = parseInt(rtM[1]);
      const replyM = label.match(/(\\d+)\\s+repl/);
      if (replyM) replies = parseInt(replyM[1]);
    }}
    return {{ likes, retweets, replies }};
  }}

  function getTweetId(article) {{
    const links = article.querySelectorAll('a[href*="/status/"]');
    const sl = Array.from(links).find(l => /\\/status\\/\\d+$/.test(l.href));
    return sl ? sl.href.match(/\\/status\\/(\\d+)/)?.[1] : null;
  }}

  function getText(article) {{
    const textEl = article.querySelector('[data-testid="tweetText"]');
    return textEl ? textEl.textContent.slice(0, 1000) : '';
  }}

  // Find main article by matching targetId in status links
  // (needed for replies where parent tweets appear above as articles[0..n-1])
  let mainIdx = 0;
  for (let i = 0; i < articles.length; i++) {{
    const links = articles[i].querySelectorAll('a[href*="/status/' + targetId + '"]');
    if (links.length > 0) {{ mainIdx = i; break; }}
  }}

  const main = articles[mainIdx];
  const username = getUsername(main);
  const displayName = getDisplayName(main);
  const text = getText(main);
  const stats = getStats(main);

  // Quoted tweet: a nested article inside the main article (document.querySelectorAll
  // returns nested articles too, so we must detect and exclude them from reply loops).
  let quotedTweet = null;
  const nestedQtArticle = main.querySelector('article[data-testid="tweet"]');
  if (nestedQtArticle) {{
    const qUser = getUsername(nestedQtArticle);
    const qText = getText(nestedQtArticle);
    const qId = getTweetId(nestedQtArticle);
    if (qUser && qText) {{
      quotedTweet = {{ username: qUser, text: qText.slice(0, 500), tweetId: qId }};
    }}
  }}

  // Parent chain: articles BEFORE the main tweet (conversation ancestry)
  const parentChain = [];
  for (let i = 0; i < mainIdx && parentChain.length < 3; i++) {{
    const pUser = getUsername(articles[i]);
    const pText = getText(articles[i]);
    if (pText && pUser) {{
      parentChain.push({{ username: pUser, text: pText.slice(0, 500), tweetId: getTweetId(articles[i]) }});
    }}
  }}

  // Articles after the main tweet: thread continuation (self-replies) + other replies.
  // Skip any article nested inside main (the quoted tweet) — document order places nested
  // articles after their parent in the NodeList, so they'd otherwise appear here.
  const threadContinuation = [];
  const otherReplies = [];
  for (let i = mainIdx + 1; i < articles.length && i <= mainIdx + 12; i++) {{
    if (main.contains(articles[i])) continue;
    const aUser = getUsername(articles[i]);
    const aText = getText(articles[i]);
    if (!aUser || !aText) continue;
    if (aUser === username && threadContinuation.length < 5) {{
      threadContinuation.push({{ text: aText, id: getTweetId(articles[i]) }});
    }} else if (aUser !== username && otherReplies.length < 5) {{
      otherReplies.push({{ username: aUser, text: aText.slice(0, 300), tweetId: getTweetId(articles[i]) }});
    }}
    if (threadContinuation.length >= 5 && otherReplies.length >= 5) break;
  }}

  return JSON.stringify({{
    username, displayName, text,
    likes: stats.likes, retweets: stats.retweets, replies: stats.replies,
    quotedTweet: quotedTweet,
    threadContinuation: threadContinuation.length ? threadContinuation : null,
    otherReplies: otherReplies.length ? otherReplies : null,
    parentChain: parentChain.length ? parentChain : null
  }});
}})()"""

    with cdp_lock():
        try:
            with CDPSession.connect() as cdp:
                if not cdp.navigate(tweet_url, wait_sec=4):
                    return None
                raw = cdp.evaluate(js, timeout=20)
        except Exception as e:
            logger.warning(f"fetch_tweet_context CDP failed: {e}")
            return None

        if not raw:
            return None

        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if not data or not data.get("username"):
                return None

            parent_chain = data.get("parentChain") or []
            reply_to = parent_chain[-1] if parent_chain else None

            return {
                "tweetId": tweet_id,
                "author": data["username"],
                "authorName": data.get("displayName", ""),
                "text": data.get("text", ""),
                "quotedTweet": data.get("quotedTweet"),
                "replyTo": reply_to,
                "parentChain": parent_chain if parent_chain else None,
                "stats": {
                    "likes": data.get("likes", 0),
                    "retweets": data.get("retweets", 0),
                    "replies": data.get("replies", 0),
                },
                "threadContinuation": data.get("threadContinuation"),
                "otherReplies": data.get("otherReplies"),
            }
        except (json.JSONDecodeError, TypeError) as e:
            print(f"  CDP fetch_tweet_context parse error: {e}", flush=True)
            return None


PROFILE_CACHE_PATH = Path("/home/openclaw/clawd/memory/twitter-profile-cache.json")
PROFILE_CACHE_TTL_DAYS = 7

SEARCH_CACHE_PATH = Path("/home/openclaw/clawd/memory/twitter-search-cache.json")
SEARCH_CACHE_TTL_HOURS = 6


def load_profile_cache() -> dict:
    """Load the profile cache. Returns {} if not found."""
    if PROFILE_CACHE_PATH.exists():
        try:
            return json.loads(PROFILE_CACHE_PATH.read_text())
        except Exception as e:
            logger.debug(f"load_profile_cache failed: {e}")
    return {}


def save_profile_cache(cache: dict) -> None:
    """Save the profile cache atomically."""
    PROFILE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROFILE_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(PROFILE_CACHE_PATH)


def get_cached_profile(username: str) -> dict | None:
    """Get cached profile if not expired. Returns None if missing/stale."""
    cache = load_profile_cache()
    entry = cache.get(username.lower())
    if not entry:
        return None
    try:
        cached_at = datetime.fromisoformat(entry["cachedAt"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - cached_at > timedelta(
            days=PROFILE_CACHE_TTL_DAYS
        ):
            return None
    except (KeyError, ValueError) as e:
        logger.debug(f"get_cached_profile date parse failed: {e}")
        return None
    return entry.get("profile")


def cache_profile(username: str, profile: dict) -> None:
    """Cache a user's profile data."""
    cache = load_profile_cache()
    cache[username.lower()] = {
        "cachedAt": utc_now(),
        "profile": profile,
    }
    save_profile_cache(cache)


def _load_search_cache() -> dict:
    """Load the search results cache. Returns {} if not found."""
    if SEARCH_CACHE_PATH.exists():
        try:
            return json.loads(SEARCH_CACHE_PATH.read_text())
        except Exception as e:
            logger.debug(f"_load_search_cache failed: {e}")
    return {}


def _save_search_cache(cache: dict) -> None:
    """Save the search results cache atomically."""
    SEARCH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEARCH_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(SEARCH_CACHE_PATH)


def _get_cached_search(cache: dict, term: str) -> list[dict] | None:
    """Return cached results for a search term if fresh, else None."""
    entry = cache.get(term.lower().strip())
    if not entry:
        return None
    try:
        cached_at = datetime.fromisoformat(entry["cachedAt"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - cached_at > timedelta(
            hours=SEARCH_CACHE_TTL_HOURS
        ):
            return None
    except (KeyError, ValueError) as e:
        logger.debug(f"_get_cached_search date parse failed: {e}")
        return None
    return entry.get("results", [])


def fetch_user_profile(username: str) -> dict | None:
    """Fetch user profile data via CDP: bio, recent tweets, stats.

    Returns dict with:
    - displayName, bio, location, website
    - followersCount, followingCount, tweetsCount
    - recentTweets: list of last 5-10 tweet texts
    - isVerified, isBlueVerified

    Uses CDPSession (direct WebSocket).
    """
    profile_url = f"{TWITTER_BASE_URL}/{username}"

    # Extract profile data + recent tweets
    js = """(() => {
  const result = {
    displayName: null,
    bio: null,
    location: null,
    website: null,
    followersCount: null,
    followingCount: null,
    tweetsCount: null,
    isVerified: false,
    isBlueVerified: false,
    recentTweets: []
  };

  // Get display name from header
  const nameSpans = document.querySelectorAll('span');
  for (const span of nameSpans) {
    const text = span.textContent || '';
    if (text.length > 0 && text.length < 100) {
      // Look for the main name in the profile header
      const parent = span.closest('[data-testid="UserName"]');
      if (parent) {
        result.displayName = text;
        break;
      }
    }
  }

  // Get bio from the bio/description element
  const bioEl = document.querySelector('[data-testid="UserDescription"]');
  if (bioEl) {
    result.bio = bioEl.textContent.slice(0, 500);
  }

  // Get location and website
  const locationEl = document.querySelector('[data-testid="UserLocation"]');
  if (locationEl) result.location = locationEl.textContent;

  const websiteEl = document.querySelector('[data-testid="UserProfileUrl"]');
  if (websiteEl) result.website = websiteEl.href;

  // Check verification
  const verifiedBadge = document.querySelector('[data-testid="icon-verified"]');
  if (verifiedBadge) result.isVerified = true;

  // Get follower/following/tweets counts from profile stats
  const statLinks = document.querySelectorAll('a[href*="followers"], a[href*="/following"]');
  for (const link of statLinks) {
    const text = link.textContent || '';
    const numMatch = text.match(/([\\d,.]+[KkMm]?)/);
    if (!numMatch) continue;
    let val = numMatch[1].replace(/,/g, '');
    if (val.endsWith('K') || val.endsWith('k')) val = parseFloat(val) * 1000;
    else if (val.endsWith('M') || val.endsWith('m')) val = parseFloat(val) * 1000000;
    else val = parseInt(val);

    if (text.includes('Follower')) result.followersCount = val;
    else if (text.includes('Following')) result.followingCount = val;
  }

  // Get tweets count from profile
  const tweetsTab = document.querySelector('a[href$="/tweets"]');
  if (tweetsTab) {
    const text = tweetsTab.textContent || '';
    const numMatch = text.match(/([\\d,.]+[KkMm]?)/);
    if (numMatch) {
      let val = numMatch[1].replace(/,/g, '');
      if (val.endsWith('K') || val.endsWith('k')) val = parseFloat(val) * 1000;
      else if (val.endsWith('M') || val.endsWith('m')) val = parseFloat(val) * 1000000;
      result.tweetsCount = parseInt(val);
    }
  }

  // Get recent tweets from timeline
  const articles = document.querySelectorAll('article[data-testid="tweet"]');
  for (let i = 0; i < Math.min(articles.length, 8); i++) {
    const textEl = articles[i].querySelector('[data-testid="tweetText"]');
    if (textEl) {
      result.recentTweets.push(textEl.textContent.slice(0, 280));
    }
  }

  return result;
})()"""

    with cdp_lock():
        try:
            with CDPSession.connect() as cdp:
                if not cdp.navigate(profile_url, wait_sec=5):
                    return None
                raw = cdp.evaluate(js, timeout=15)
        except Exception as e:
            logger.warning(f"fetch_user_profile CDP failed: {e}")
            return None

        if raw is None:
            return None

        try:
            profile = json.loads(raw) if isinstance(raw, str) else raw
            if profile and isinstance(profile, dict):
                return profile
            return None
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug(f"fetch_user_profile JSON parse failed: {e}")
            return None


def get_user_profile(username: str, use_cache: bool = True) -> dict | None:
    """Get user profile, using cache if available and fresh."""
    if use_cache:
        cached = get_cached_profile(username)
        if cached:
            print(f"  Using cached profile for @{username}", flush=True)
            return cached

    print(f"  Fetching profile for @{username}...", flush=True)
    profile = fetch_user_profile(username)
    if profile:
        cache_profile(username, profile)
    return profile


# In-process follower count cache — deduplicates within a single script run.
# Keyed by lowercase username. Cleared on each new process start.
_follower_count_run_cache: dict[str, int] = {}


def get_follower_count(username: str) -> int | None:
    """Fetch a user's follower count, with two caching layers:

    1. In-process dict (deduplicates within a single run — free)
    2. Profile file cache (cross-run, 7-day TTL — saves profile page navigations)

    Falls back to a CDP profile page visit on a full cache miss.
    """
    username_lower = username.lower()

    # Layer 1: in-process (same author appearing in multiple search results)
    if username_lower in _follower_count_run_cache:
        return _follower_count_run_cache[username_lower]

    # Layer 2: file-based profile cache (cross-run)
    cached_profile = get_cached_profile(username)
    if cached_profile is not None and cached_profile.get("followersCount") is not None:
        count = int(cached_profile["followersCount"])
        _follower_count_run_cache[username_lower] = count
        return count

    # Cache miss — navigate to profile page
    js = r"""(() => {
  // Look for the followers link which contains the count
  const links = document.querySelectorAll('a[href$="/verified_followers"]');
  for (const link of links) {
    const text = link.textContent || '';
    // Matches patterns like "1,234 Followers" or "5.2K Followers" or "12M Followers"
    const m = text.match(/([\d,.]+[KkMm]?)\s*Follower/);
    if (m) {
      let val = m[1].replace(/,/g, '');
      if (val.endsWith('K') || val.endsWith('k')) return parseFloat(val) * 1000;
      if (val.endsWith('M') || val.endsWith('m')) return parseFloat(val) * 1000000;
      return parseInt(val);
    }
  }
  return null;
})()"""

    profile_url = f"https://x.com/{username}"
    count = None
    with cdp_lock():
        try:
            with CDPSession.connect() as cdp:
                if not cdp.navigate(profile_url, wait_sec=3):
                    return None
                raw = cdp.evaluate(js, timeout=10)
        except Exception as e:
            logger.warning(f"get_follower_count CDP failed: {e}")
            return None

        if raw is None:
            return None

        try:
            result = json.loads(raw) if isinstance(raw, str) else raw
            count = int(result) if result is not None else None
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.debug(f"get_follower_count parse failed for @{username}: {e}")
            return None

    if count is not None:
        # Populate both cache layers so subsequent calls are free
        _follower_count_run_cache[username_lower] = count
        file_cache = load_profile_cache()
        existing_entry = file_cache.get(username_lower, {})
        existing_profile = existing_entry.get("profile", {})
        existing_profile["followersCount"] = count
        file_cache[username_lower] = {
            "cachedAt": utc_now(),
            "profile": existing_profile,
        }
        save_profile_cache(file_cache)

    return count


def follow_user(username: str) -> bool:
    """Follow a user via CDP by navigating to their profile and clicking Follow.

    Uses CDPSession (direct WebSocket).
    """
    with cdp_lock():
        profile_url = f"https://x.com/{username}"

        try:
            with CDPSession.connect() as cdp:
                if not cdp.navigate(profile_url, wait_sec=2):
                    return False
                FOLLOW_BTN = '[data-testid$="-follow"]'
                UNFOLLOW_BTN = '[data-testid$="-unfollow"]'
                # Wait for either button to appear (page loaded)
                if not cdp.wait_for(f'{FOLLOW_BTN}, {UNFOLLOW_BTN}', timeout=10):
                    print(f"  CDP: follow/unfollow button not found for @{username}", flush=True)
                    return False
                # Check if already following
                already = cdp.evaluate(f'document.querySelector({json.dumps(UNFOLLOW_BTN)}) !== null')
                if already:
                    print(f"  CDP: already following @{username}", flush=True)
                    return True
                print(f"  CDP: clicking Follow for @{username}...", flush=True)
                if not cdp.click(FOLLOW_BTN):
                    print(f"  CDP: Follow button not found for @{username}", flush=True)
                    return False
                # Poll up to 5s for unfollow button to confirm follow took effect
                deadline = time.time() + 5
                while time.time() < deadline:
                    if cdp.evaluate(f'document.querySelector({json.dumps(UNFOLLOW_BTN)}) !== null'):
                        print(f"  CDP: followed @{username}", flush=True)
                        return True
                    time.sleep(1)
                print(f"  CDP: follow @{username} may not have succeeded", flush=True)
                return False
        except Exception as e:
            logger.warning(f"follow_user CDP failed: {e}")
            return False


def unfollow_user(username: str) -> bool:
    """Unfollow a user via CDP by clicking Following → confirm Unfollow.

    Uses CDPSession (direct WebSocket).
    """
    with cdp_lock():
        profile_url = f"https://x.com/{username}"

        try:
            with CDPSession.connect() as cdp:
                if not cdp.navigate(profile_url, wait_sec=2):
                    return False
                FOLLOW_BTN = '[data-testid$="-follow"]'
                UNFOLLOW_BTN = '[data-testid$="-unfollow"]'
                CONFIRM_BTN = '[data-testid="confirmationSheetConfirm"]'
                # Wait for either button to appear (page loaded)
                if not cdp.wait_for(f'{FOLLOW_BTN}, {UNFOLLOW_BTN}', timeout=10):
                    print(f"  CDP: follow/unfollow button not found for @{username}", flush=True)
                    return False
                # Check if actually following
                if not cdp.evaluate(f'document.querySelector({json.dumps(UNFOLLOW_BTN)}) !== null'):
                    print(f"  CDP: not following @{username} (no unfollow button)", flush=True)
                    return True  # Already not following
                print(f"  CDP: clicking Following to unfollow @{username}...", flush=True)
                if not cdp.click(UNFOLLOW_BTN):
                    print(f"  CDP: click Following failed for @{username}", flush=True)
                    return False
                # Wait for confirmation dialog
                if not cdp.wait_for(CONFIRM_BTN, timeout=5):
                    print(f"  CDP: Unfollow confirmation not found for @{username}", flush=True)
                    return False
                if not cdp.click(CONFIRM_BTN):
                    return False
                # Poll up to 5s for unfollow to confirm
                deadline = time.time() + 5
                while time.time() < deadline:
                    if not cdp.evaluate(f'document.querySelector({json.dumps(UNFOLLOW_BTN)}) !== null'):
                        print(f"  CDP: unfollowed @{username}", flush=True)
                        return True
                    time.sleep(1)
                print(f"  CDP: unfollow @{username} may not have succeeded", flush=True)
                return False
        except Exception as e:
            logger.warning(f"unfollow_user CDP failed: {e}")
            return False


def check_follows_back(username: str) -> bool | None:
    """Check if a user follows us back by looking for 'Follows you' on their profile.

    Returns True/False, or None if the check failed.
    Uses CDPSession (direct WebSocket).
    """
    profile_url = f"https://x.com/{username}"
    # JS to detect "Follows you" badge on the profile page
    js = """(() => {
  const spans = document.querySelectorAll('span');
  for (const span of spans) {
    if (span.textContent && span.textContent.trim() === 'Follows you') return true;
  }
  return false;
})()"""

    with cdp_lock():
        try:
            with CDPSession.connect() as cdp:
                if not cdp.navigate(profile_url, wait_sec=3):
                    return None
                result = cdp.evaluate(js, timeout=10)
                if result is not None:
                    return bool(result)
                return None
        except Exception as e:
            logger.warning(f"check_follows_back CDP failed: {e}")
            return None


def get_latest_own_tweet_id(username: str = "DecentCloud_org") -> str | None:
    """Get the ID of the most recent tweet/reply from our account via CDP.

    Navigates to the profile's Replies tab and extracts the first tweet ID.
    Used by thread posting to chain replies.
    Uses CDPSession (direct WebSocket).
    """
    url = f"https://x.com/{username}/with_replies"
    js = """(() => {
  const articles = document.querySelectorAll('article[data-testid="tweet"]');
  for (const article of articles) {
    const links = article.querySelectorAll('a[href*="/status/"]');
    const statusLink = Array.from(links).find(l => /\\/status\\/\\d+$/.test(l.href));
    if (statusLink) {
      const m = statusLink.href.match(/\\/status\\/(\\d+)/);
      if (m) return m[1];
    }
  }
  return null;
})()"""

    with cdp_lock():
        try:
            with CDPSession.connect() as cdp:
                if not cdp.navigate(url, wait_sec=4):
                    return None
                raw = cdp.evaluate(js, timeout=10)
        except Exception as e:
            logger.warning(f"get_latest_own_tweet_id CDP failed: {e}")
            return None

        if not raw:
            return None

        try:
            result = json.loads(raw) if isinstance(raw, str) else raw
            return str(result) if result else None
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug(f"get_latest_own_tweet_id JSON parse failed: {e}")
            return None


def get_tweet_stats(tweet_id: str) -> dict | None:
    """Navigate to a tweet URL and extract its engagement stats via JS.

    Returns a dict with keys: likes, retweets, replies (all int).
    Returns None if navigation fails or stats cannot be extracted.
    Used by daily_strategy_eval.py to measure reply performance.
    Uses CDPSession (direct WebSocket).
    """
    tweet_url = f"https://x.com/i/web/status/{tweet_id}"

    js = r"""(() => {
  // Twitter renders counts as aria-label on action buttons, e.g.
  // "17 Likes", "3 reposts", "2 replies"
  const stats = {likes: 0, retweets: 0, replies: 0};

  // Helper: parse a humanized count string into an integer ("1.2K" → 1200)
  function parseCount(text) {
    if (!text) return 0;
    const m = text.match(/([\d,.]+)([KkMm]?)/);
    if (!m) return 0;
    let n = parseFloat(m[1].replace(/,/g, ''));
    if (m[2].toLowerCase() === 'k') n = Math.round(n * 1000);
    if (m[2].toLowerCase() === 'm') n = Math.round(n * 1000000);
    return Math.round(n);
  }

  // Walk all aria-label text in action bar elements
  const groups = document.querySelectorAll('[role="group"]');
  for (const group of groups) {
    const buttons = group.querySelectorAll('[data-testid]');
    for (const btn of buttons) {
      const testId = btn.getAttribute('data-testid') || '';
      const ariaLabel = btn.getAttribute('aria-label') || btn.textContent || '';
      if (testId === 'like' || testId === 'unlike') {
        const m = ariaLabel.match(/([\d,.]+[KkMm]?)\s*(Like|likes)/i);
        if (m) stats.likes = parseCount(m[1]);
      } else if (testId === 'retweet' || testId === 'unretweet') {
        const m = ariaLabel.match(/([\d,.]+[KkMm]?)\s*(repost|Repost|retweet|Retweet)/i);
        if (m) stats.retweets = parseCount(m[1]);
      } else if (testId === 'reply') {
        const m = ariaLabel.match(/([\d,.]+[KkMm]?)\s*(repl|Repl)/i);
        if (m) stats.replies = parseCount(m[1]);
      }
    }
  }

  // Fallback: scrape the tweet status bar which shows "X Likes" as text
  const spans = document.querySelectorAll('[href$="/likes"] span, [href$="/retweets"] span');
  for (const span of spans) {
    const parent = span.closest('a') || span;
    const href = (parent.getAttribute && parent.getAttribute('href')) || '';
    const val = parseCount(span.textContent);
    if (href.endsWith('/likes') && val > 0) stats.likes = val;
    if (href.endsWith('/retweets') && val > 0) stats.retweets = val;
  }

  return JSON.stringify(stats);
})()"""

    with cdp_lock():
        try:
            with CDPSession.connect() as cdp:
                if not cdp.navigate(tweet_url, wait_sec=5):
                    return None
                raw = cdp.evaluate(js, timeout=15)
        except Exception as e:
            logger.warning(f"get_tweet_stats CDP failed: {e}")
            return None

        if not raw:
            return None

        try:
            # cdp.evaluate may return a JSON-encoded string or the raw value
            result = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(result, str):
                result = json.loads(result)
            if not isinstance(result, dict):
                return None
            return {
                "likes": int(result.get("likes", 0)),
                "retweets": int(result.get("retweets", 0)),
                "replies": int(result.get("replies", 0)),
            }
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.debug(f"get_tweet_stats parse failed for {tweet_id}: {e}")
            return None


def auto_follow_after_engagement(conn, username: str, tweet_id: str) -> bool:
    """Follow a user after engaging with their tweet. Returns True if followed.

    conn: a psycopg2 connection (from get_conn() context manager)
    """
    from db import is_followed, set_followed, upsert_account

    # Skip our own account
    if username.lower() == "decentcloud_org":
        return False

    # Check DB: already followed?
    if is_followed(conn, username):
        print(f"  Already following @{username} in DB", flush=True)
        return False

    print(f"  Auto-following @{username}...", flush=True)
    if follow_user(username):
        # Ensure account row exists first, then mark followed
        upsert_account(
            conn,
            username,
            stage="followed",
            discovery_source="engagement",
        )
        set_followed(conn, username)
        return True
    return False
