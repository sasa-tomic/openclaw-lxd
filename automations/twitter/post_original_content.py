#!/usr/bin/env python3
"""Post original Twitter content for @DecentCloud_org.

Strategy: /projects/Notes/Pickle/Twitter/decent-cloud-twitter-plan.md

Runs daily to ensure consistent original content posting (1-2 tweets/day).
Phase 1 mode: founder voice — no links, no product mentions, no hashtags.

Approach: fetch the top-performing tweets from the last 24 hours, use an LLM
to analyze their structural patterns (length, tone, hooks, format), then
generate original tweets that mimic those winning structures.

DRY-RUN CLI: `--dry-run` previews all ranked candidates with scores without
posting anything.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent.parent))  # lib.*
sys.path.insert(0, str(Path(__file__).parent))  # db, twitter_utils

from lib.llm_utils import call_llm_simple as call_llm, extract_json
from twitter_utils import (
    ensure_browser_ready,
    fetch_top_tweets,
    humanize,
    jitter_sleep,
    load_project_context,
    post_tweet,
    post_reply_with_retries,
    send_error_alert,
    utc_now,
    get_latest_own_tweet_id,
)
from db import (
    get_conn,
    get_latest_morning_research,
    count_posts_today,
    ensure_schema,
    get_recent_posts,
    get_top_posts,
    insert_post,
)

LLM_LOG_DIR = Path("/home/openclaw/clawd/logs")


def _log_llm_call(prompt: str, response: str, name: str) -> None:
    """Log prompt and response to a timestamped file for debugging."""
    try:
        LLM_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_file = LLM_LOG_DIR / f"original-content-{ts}-{name}.log"
        log_file.write_text(
            f"=== PROMPT ===\n{prompt}\n\n=== RESPONSE ===\n{response}\n"
        )
        print(f"Logged LLM call to {log_file}", flush=True)
    except Exception as e:
        print(f"Failed to log LLM call: {e}", file=sys.stderr)


def _merge_top_tweets_with_own(
    top_tweets: list[dict] | None, own_posts: list[dict] | None
) -> list[dict]:
    """Merge external top tweets with our own top posts into a single pool.

    Returns a unified list sorted by engagement (likes + retweets) descending,
    so the LLM sees all examples in one ranked view.
    """
    merged: list[dict] = []

    for t in (top_tweets or []):
        if not _is_text_primary_tweet(t):
            continue
        merged.append({
            "author": t.get("author", "?"),
            "text": re.sub(r"\s*👇\s*", " ", (t.get("text") or "")).strip(),
            "likes": t.get("likes", 0) or 0,
            "rts": t.get("retweets", 0) or 0,
            "ours": False,
        })

    for p in (own_posts or []):
        text = (p.get("text") or "").replace("\n", " ")
        text = re.sub(r"\s*👇\s*", " ", text).strip()
        if len(text) < 60:
            continue
        merged.append({
            "author": "DecentCloud_org",
            "text": text,
            "likes": p.get("likes", 0) or 0,
            "rts": p.get("rts", 0) or 0,
            "ours": True,
        })

    merged.sort(key=lambda t: t["likes"] + t["rts"], reverse=True)
    return merged


def _format_merged_examples(merged: list[dict]) -> str:
    """Format the merged pool of top tweets + our posts for the prompt."""
    if not merged:
        return ""
    lines = [
        "## TOP-PERFORMING TWEETS — study structure and voice, write like the best ones:"
    ]
    for t in merged[:30]:
        tag = " [OURS]" if t["ours"] else ""
        lines.append(
            f"  [{t['likes']}L {t['rts']}RT] @{t['author']}{tag}: {t['text']}"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRODUCT_MENTION_PATTERNS = [
    r"\bwe\s+(just|recently|today|now)?\s*(shipped|launched|built|released|deployed|published)\b",
    r"\bwe\s+just\b",
    r"\bdecent\s*cloud\b",
    r"\bour\s+(platform|product|marketplace|service|tool|app)\b",
    r"\bcheck\s+out\b",
    r"\bsign\s+up\b",
    r"\bwaitlist\b",
    r"\bearly\s+access\b",
    r"\bfull\s+visibility\s+into\b",
    r"\bdeployment\s+recipe\b",
]

STANDALONE_FORMATS = set()  # determined dynamically from top tweets now


def _is_text_primary_tweet(tweet: dict) -> bool:
    """Filter out tweets that are primarily images, links, listicles, or too short."""
    text = (tweet.get("text") or "").strip()
    # Remove t.co URLs to see actual text content
    text_no_urls = re.sub(r"https?://t\.co/\S+", "", text).strip()
    # Must have substantial text (not just a caption for an image)
    if len(text_no_urls) < 60:
        return False
    # Skip if more than half the tweet is URLs
    if len(text_no_urls) < len(text) * 0.5:
        return False
    # Skip numbered lists / educational listicles (Step-1, "1.", "2.", etc.)
    numbered_items = re.findall(r"(?:^|\n)\s*(?:Step-?\d|\d+[.)]\s)", text)
    if len(numbered_items) >= 3:
        return False
    return True


def _analyze_top_tweet_structures(top_tweets: list[dict]) -> str:
    """Use LLM to extract structural patterns from top-performing tweets."""
    # Filter to text-primary tweets only (no image memes, link dumps)
    text_tweets = [t for t in (top_tweets or []) if _is_text_primary_tweet(t)]
    if not text_tweets:
        return ""

    tweet_lines = []
    for t in text_tweets[:25]:
        text = (t.get("text") or "").replace("\n", " \\n ")
        likes = t.get("likes", 0) or 0
        rts = t.get("retweets", 0) or 0
        author = t.get("author", "?")
        tweet_lines.append(f"[{likes}L {rts}RT] @{author}: {text}")

    prompt = f"""Analyze these top-performing tweets from the last 24 hours.
Extract the STRUCTURAL PATTERNS that make them work — not the topics.

## Tweets
{chr(10).join(tweet_lines)}

For each distinct structural pattern you find, describe:
1. FORMAT: What's the structure? (e.g. "hot take + receipts", "personal story with twist ending",
   "one-liner observation", "list of 3 things", "question that forces a reply", "contrarian claim")
2. LENGTH: Roughly how long? (one-liner, 2-3 sentences, paragraph)
3. HOOK TECHNIQUE: How does it grab attention in the first 5 words?
4. TONE: What's the emotional register? (irreverent, deadpan, outraged, confessional, matter-of-fact)
5. WHY IT WORKS: What psychological trigger makes people engage?

Return ONLY a JSON object:
{{"patterns": [
  {{"name": "short descriptive name",
    "description": "2-3 sentence description of the structure",
    "example_tweet": "copy the best example tweet text verbatim",
    "hook_technique": "how it opens",
    "tone": "emotional register",
    "why_it_works": "psychological trigger"}}
]}}

Find 3-6 distinct patterns. Focus on STRUCTURE not content."""

    try:
        raw = call_llm(prompt, timeout=180, json_mode=True, temperature=0.3)
        if not raw:
            return ""
        _log_llm_call(prompt, raw, "analyze-structures")
        return raw
    except Exception as e:
        print(f"Structure analysis failed: {e}", file=sys.stderr)
        return ""


# Minimal guardrails — no prescriptive format rules
_PROMPT_RULES = """# Rules
- No hashtags, no links, no "Decent Cloud", no product mentions.
- No AI words: "Furthermore", "Additionally", "crucial", "landscape",
  "It's worth noting", "I have a confession", "Here's the thing".
- No formulaic openings: don't start with "I have a confession to make",
  "Hot take:", "Unpopular opinion:", or any templated intro.
- Max 280 chars per tweet. If you include a reply thread tweet, also max 280 chars.
- DO NOT rehash any scenario from the "RECENTLY POSTED" list — not even with different
  wording. Same plot with new numbers is still a duplicate.
- Each tweet must be about a DIFFERENT topic.
- Every claim must be factually defensible. A senior engineer reading it must think
  "yeah, that tracks" — never "that's made up."
"""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_META_PREFIXES = (
    "let me ",
    "here are ",
    "generate ",
    "write ",
    "draft ",
    "output ",
    "consider ",
    "thinking about ",
)
_META_SUBSTRINGS = (
    "analyze the requirements",
    "project & strategy context",
    "json array",
    "your task",
    "rules for all posts",
    "output only",
    "guidelines",
    "something about ",
    "let me think",
    "that's good",
    "this needs to be",
)
_META_SUFFIXES = ("angle", "strategy")


def _looks_like_meta_response(text: str) -> bool:
    """Detect LLM meta-commentary / thinking-out-loud instead of actual content."""
    t = (text or "").strip().lower()
    t_plain = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    if any(t.startswith(p) for p in _META_PREFIXES):
        return True
    if any(s in t for s in _META_SUBSTRINGS):
        return True
    if t.endswith(":") or bool(re.match(r"\*\*[^*]+\*\*\s*:", t)):
        return True
    if any(t_plain.endswith(s) for s in _META_SUFFIXES):
        return True
    return False


def has_product_mention(text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in PRODUCT_MENTION_PATTERNS)


def _is_valid_tweet_text(text: str) -> bool:
    """Validate a single tweet text (hook or reveal)."""
    t = (text or "").strip()
    # Strip trailing thread indicators the LLM may add (we add our own later)
    t = re.sub(r"\s*👇\s*$", "", t).strip()
    if not (20 <= len(t) <= 600):
        return False
    if _looks_like_meta_response(t):
        return False
    if not re.search(r'[.!?""]\s*$', t):
        return False
    if len(t.split()) < 6:
        return False
    return True


def _is_valid_thread_entry(entry: dict) -> bool:
    """Validate a thread entry (hook + optional reveal)."""
    hook = (entry.get("hook") or entry.get("text") or "").strip()
    reveal = (entry.get("reveal") or "").strip()

    if not hook or not _is_valid_tweet_text(hook) or has_product_mention(hook):
        return False

    # Reveal is optional — standalone tweets are fine
    if not reveal:
        return True

    if not _is_valid_tweet_text(reveal) or has_product_mention(reveal):
        return False
    return True


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------


def _clean_llm_text(text: str) -> str:
    """Strip quotes, LLM categorization prefixes, and trailing thread indicators."""
    t = (text or "").strip().strip('"').strip("'")
    t = re.sub(r"^\([^)]{3,30}\)\s*", "", t).strip()
    # Remove trailing 👇 — we add our own thread indicator later
    t = re.sub(r"\s*👇\s*$", "", t).strip()
    return t


def _parse_llm_json(raw: str) -> object | None:
    """Parse JSON from raw LLM output, tolerating code fences and wrapper text."""
    text = extract_json(raw)
    if text:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass
    return None


# ---------------------------------------------------------------------------
# LLM prompt context builders
# ---------------------------------------------------------------------------




def get_recent_commits(n: int = 25) -> list[str]:
    """Fetch recent commits from decent-cloud repo."""
    try:
        r = subprocess.run(
            ["git", "-C", "/projects/decent-cloud", "log", "--oneline", f"-{n}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip().split("\n")
    except Exception:
        pass
    return []


def _build_context_sections(
    conn,
    top_tweets: list[dict] | None = None,
    structure_analysis: str = "",
) -> str:
    """Build the shared context sections used by both batch and single draft prompts."""
    recent_posts = [
        p.get("text", "")[:200] for p in get_recent_posts(conn, days=14, limit=12)
    ]
    own_top_posts = get_top_posts(conn, limit=10)
    research = get_latest_morning_research(conn)
    project_context = load_project_context()

    research_text = ""
    if research:
        parts = []
        if research.get("hnStories"):
            for s in research["hnStories"][:3]:
                parts.append(f'- HN trending: "{s["title"]}" ({s["points"]} pts)')
        if research.get("devActivity"):
            parts.append(f"- Dev activity: {research['devActivity']}")
        research_text = "\n".join(parts) if parts else ""

    commits_text = ""
    if not research_text:
        commits = get_recent_commits(6)
        if commits:
            commits_text = "\n".join(f"- {c}" for c in commits[:4])

    # Merge external top tweets with our own top posts into one ranked pool
    merged_examples = _merge_top_tweets_with_own(top_tweets, own_top_posts)

    structure_section = ""
    if structure_analysis:
        structure_section = f"""
## STRUCTURAL PATTERNS FROM TODAY'S TOP TWEETS
These are the patterns that are getting engagement RIGHT NOW.
Your tweets MUST mimic these structures — not the topics, the STRUCTURES.
{structure_analysis}
"""

    return f"""# Context
{project_context}

{_format_merged_examples(merged_examples)}

{structure_section}

# RECENTLY POSTED — DO NOT DUPLICATE
The following posts were already published. DO NOT generate anything with the same
story, scenario, plot, or premise — even if you rephrase it. Same company + same
cost issue = duplicate. Same compliance dilemma = duplicate. Same migration story
= duplicate. You MUST write about DIFFERENT situations, companies, and problems.

{json.dumps(recent_posts, indent=2)}

{f"Trending today (NO links):{chr(10)}{research_text}" if research_text else ""}
{f"Dev activity:{chr(10)}{commits_text}" if commits_text else ""}"""


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------


def _filter_valid_unposted(queue: list[dict], conn=None) -> list[dict]:
    """Filter queue to valid unposted entries, marking invalid ones as posted.

    Handles both new thread-format entries and legacy plain-text entries.
    """
    valid = []
    for e in queue:
        if e.get("posted"):
            continue
        td = e.get("thread_data")
        if td and isinstance(td, dict):
            if _is_valid_thread_entry(td):
                valid.append(e)
            elif conn and e.get("id") is not None:
                mark_content_queue_posted(
                    conn, int(e["id"]), datetime.now(timezone.utc)
                )
        else:
            # Legacy entries without thread_data
            if _is_valid_tweet_text(e.get("text", "")):
                valid.append(e)
            elif conn and e.get("id") is not None:
                mark_content_queue_posted(
                    conn, int(e["id"]), datetime.now(timezone.utc)
                )
    return valid


def _to_rank_candidate(entry: dict) -> dict:
    """Convert a queue entry into a ranking candidate dict."""
    td = entry.get("thread_data")
    if td and isinstance(td, dict):
        return {
            "id": entry.get("id"),
            "format": td.get("format", ""),
            "hook": td.get("hook", ""),
            "reveal": td.get("reveal", ""),
        }
    return {
        "id": entry.get("id"),
        "format": "legacy",
        "hook": entry.get("text", ""),
        "reveal": "",
    }


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


def _build_thread_entry(item: dict) -> dict | None:
    """Parse and validate a single thread item from LLM output."""
    if not isinstance(item, dict):
        return None

    fmt = (item.get("format") or "").strip()
    hook = _clean_llm_text(item.get("hook") or "")
    reveal = _clean_llm_text(item.get("reveal") or "")
    thread_data = {"format": fmt, "hook": hook, "reveal": reveal}

    if not _is_valid_thread_entry(thread_data):
        print(f"Draft rejected ({fmt}): {hook[:80]}", file=sys.stderr)
        return None

    return {
        "text": hook,
        "draftedAt": datetime.now(timezone.utc).isoformat(),
        "llm_score": 0,
        "posted": False,
        "thread_data": thread_data,
    }


def draft_batch(context: str, temperature: float = 0.9) -> list[dict]:
    """Draft a batch of 6 tweets by mimicking top-tweet structures.

    Args:
        context: Pre-built context string (from _build_context_sections).
        temperature: LLM sampling temperature.
    """
    prompt = f"""Write 6 tweets about cloud infrastructure, DevOps, or engineering.

AUDIENCE: Senior engineers, CTOs, DevOps leads who have seen everything.
They scroll past listicles, roadmaps, "X things you should know" content.
They stop for: sharp observations they haven't heard before, genuine stories
with specific details, contrarian takes that are actually defensible.

Study the STRUCTURAL PATTERNS from today's top tweets (if provided below).
Write tweets that would fit naturally in the feed alongside those tweets.

CRITICAL RULES:
- Each tweet MUST use a DIFFERENT structure. No two tweets should feel similar.
- Write like you're texting a senior engineer friend, not creating content.
- Do NOT write numbered lists, roadmaps, or educational content.
- Do NOT use ALL CAPS for emphasis.
- Do NOT add 👇 or any thread indicators — the system handles that.
- Standalone tweets are fine. Only add a reveal if the story genuinely needs one.
- The best tweets say ONE thing sharply, not many things broadly.

{context}

{_PROMPT_RULES}

Output ONLY a JSON array. Each element:
{{"format": "short label for the structure",
  "hook": "the main tweet (max 280 chars)",
  "reveal": "optional self-reply (max 280 chars, empty string if standalone)"}}

No markdown, no explanation. Just the JSON array of 6 objects."""

    try:
        raw = call_llm(prompt, timeout=300, json_mode=True, temperature=temperature)
        if not raw:
            print("LLM returned nothing for batch draft", file=sys.stderr)
            return []

        _log_llm_call(prompt, raw, f"draft-batch-T{temperature}")

        items = _parse_llm_json(raw)
        if not isinstance(items, list):
            print(f"Failed to parse batch draft JSON: {raw[:200]}", file=sys.stderr)
            return []

        entries = [e for item in items if (e := _build_thread_entry(item)) is not None]
        print(
            f"Batch drafted {len(entries)} valid threads from {len(items)} candidates",
            flush=True,
        )
        return entries

    except Exception as e:
        print(f"LLM batch draft failed: {e}", file=sys.stderr)
        return []


def draft_single(context: str) -> dict | None:
    """Draft a single tweet. Fallback for batch failures."""

    prompt = f"""Write ONE tweet about cloud infrastructure, DevOps, or engineering.

Study the STRUCTURAL PATTERNS from today's top tweets (provided below)
and write a tweet that uses one of those structures.

{context}

{_PROMPT_RULES}

Output ONLY: {{"format": "short label for the structure", "hook": "the tweet", "reveal": "optional reply thread (empty string if standalone)"}}"""

    try:
        raw = call_llm(prompt, timeout=180, json_mode=True, temperature=0.9)
        if not raw:
            return None

        payload = _parse_llm_json(raw)
        if not isinstance(payload, dict):
            return None

        payload.setdefault("format", "freeform")
        return _build_thread_entry(payload)

    except Exception as e:
        print(f"Single draft failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def llm_rank_candidates(
    candidates: list[dict], recent_posts: list[str] | None = None
) -> list[dict]:
    """Rank thread candidates by engagement potential using a single LLM call.

    Returns candidates sorted best-first, each with an 'llm_score' added.
    Falls back to original order if the LLM call fails.
    """
    if not candidates:
        return candidates

    # Format candidates for the prompt
    lines = []
    for i, c in enumerate(candidates):
        label = f"C{i + 1}"
        fmt = c.get("format", "unknown")
        hook = (c.get("hook") or "").replace("\n", " ")
        reveal = (c.get("reveal") or "").replace("\n", " ")
        if reveal:
            lines.append(f"[{label}] ({fmt})\n  HOOK: {hook}\n  REVEAL: {reveal}")
        else:
            lines.append(f"[{label}] ({fmt})\n  TWEET: {hook}")

    recent_section = ""
    if recent_posts:
        recent_section = f"""
## Our recently published posts (for duplicate detection)

{chr(10).join(f"- {p}" for p in recent_posts)}
"""

    prompt = f"""You are scrolling Twitter. You see 200 posts per session. Most are forgettable.

Below are tweet thread drafts. Each has a HOOK (the main tweet) and optionally a REVEAL (self-reply).

## The candidates

{chr(10).join(lines)}
{recent_section}
## How to judge

For each thread, evaluate:
1. HOOK POWER: Would you stop scrolling? Does it create unbearable curiosity?
   - "And then we opened the bill" → you MUST click to see the reveal
   - "Kubernetes saves money" → you MUST correct this person
   - Generic observation with no tension → skip, score 0-3

2. REVEAL PAYOFF (if present): Does the reveal DELIVER?
   - Directly continues from the hook's unresolved moment → good
   - Specific numbers, real consequences, closes the story → score 8-10
   - Vague "it was bad" or disconnected lecture → score 0-4

3. HOOK → REVEAL CONNECTION: Does the reveal answer the hook?
   - Hook ends on "I thought it was a billing error" → reveal opens "It wasn't." → strong
   - Hook ends on a cliffhanger → reveal opens with unrelated explanation → weak, score -2

4. HUMAN EMOTION: Does the post make you FEEL something?
   - Disbelief, outrage, "oh no I've been there", schadenfreude, curiosity → good
   - Numbers delivered like a spreadsheet with no reaction → score 0-3
   - The author must feel something too — if the reveal reads like a report, it's dead

5. PARTICIPATION PULL: Would you reply, quote-tweet, or share?
   - "Reply with your number" + relatable question → high
   - Closed statement nobody needs to add to → low

6. DUPLICATE DETECTION: Does this candidate rehash a recently published post?
   - Same story/scenario/premise as a recent post, even with different wording → score 0
   - Same company + same cost problem = duplicate → score 0
   - Same compliance/migration/outage dilemma with swapped numbers = duplicate → score 0
   - A candidate that tells a GENUINELY different story is fine even if it mentions
     the same company — "AWS egress fees" and "AWS support pricing" are different topics.

REJECT (score 0):
- DUPLICATES: Any candidate whose story, scenario, or premise overlaps with our
  recently published posts. Rewording the same plot with new numbers is STILL a
  duplicate. This is the FIRST thing to check — if it's a duplicate, score 0
  immediately, do not evaluate further.
- SPREADSHEET REVEALS: Numbers listed as line items with no human reaction.
  "AWS egress: $0.09/GB. Wholesale: $0.001/GB. That's 9,000% markup." is a
  calculator, not a person. The author must FEEL something about the numbers.
- HYPERBOLE / UNVERIFIABLE CLAIMS: "would get any other industry investigated",
  "most profitable line item ever", "nobody talks about this" — if it can't be
  backed up with a link, don't say it. Exaggeration kills credibility.
- PROMPT ARTIFACTS in the tweet text: "Wrong answers only", "Reply with your
  number", "Here's why", or any phrase that reads like an instruction to the
  reader rather than natural speech. If a real person wouldn't say it out loud
  at a bar, it doesn't belong in the tweet.
- Common knowledge hooks ("cloud is expensive" — yeah, we know)
- AI-sounding language (hedge-y, generic, no voice)
- Passive/dry report voice ("resources were provisioned", "the bill was high")
- Missing payoff (tension with no resolution)
- Reveal that doesn't answer the hook (disconnected lecture)
- Reveal that trails off on a dangling fact without resolution
- Incomplete thoughts or meta-commentary
- FACTUALLY WRONG claims (numbers that don't add up, stats a practitioner would call fake)
- OBVIOUS statements that anyone with basic tech knowledge already knows
- Hooks that make the author look ignorant rather than provocative
- "impossible_quiz" questions where the real answer is ALREADY well-known to the
  target audience (e.g. "how fast are leaked keys exploited?" — everyone knows
  it's minutes, there's no surprise)

Return ONLY valid JSON:
{{
  "rankings": [
    {{"id": "C1", "score": 9, "reason": "one sentence: why this stops the scroll"}}
  ]
}}

Requirements:
- Rank ALL {len(candidates)} candidates: C1..C{len(candidates)}
- score: integer 0-10
- Be HARSH. Most tweets are forgettable. Only 1-2 should score above 7.
- Any duplicate of a recent post MUST get score 0."""

    try:
        raw = call_llm(prompt, timeout=600, json_mode=True)
        if not raw:
            print("LLM ranking returned nothing, using original order", file=sys.stderr)
            return candidates

        _log_llm_call(prompt, raw, "rank")

        payload = _parse_llm_json(raw)
        rankings = None
        if isinstance(payload, dict):
            rankings = payload.get("rankings")
        elif isinstance(payload, list):
            rankings = payload
        if not isinstance(rankings, list):
            print(
                "Could not parse LLM ranking response, using original order",
                file=sys.stderr,
            )
            return candidates

        expected_ids = {f"C{i + 1}" for i in range(len(candidates))}
        by_id: dict[str, dict] = {}
        for item in rankings:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id", "")).strip()
            try:
                score = float(item.get("score"))
            except (TypeError, ValueError):
                continue
            if cid not in expected_ids:
                continue
            by_id[cid] = {
                "score": max(0.0, min(10.0, score)),
                "reason": str(item.get("reason", "")).strip(),
            }

        if len(by_id) != len(expected_ids):
            print(
                "Incomplete LLM ranking response, using original order", file=sys.stderr
            )
            return candidates

        for i, c in enumerate(candidates):
            label = f"C{i + 1}"
            c["llm_score"] = by_id[label]["score"]
            reason = by_id[label]["reason"]
            if reason:
                print(f"  {label} (score={c['llm_score']}): {reason}", flush=True)

        return sorted(candidates, key=lambda c: c.get("llm_score", 0), reverse=True)

    except Exception as e:
        print(f"LLM ranking failed: {e}, using original order", file=sys.stderr)
        return candidates


# ---------------------------------------------------------------------------
# CLI + main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Post original Twitter content")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview generated posts without posting"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=None,
        help="Prompt for confirmation before posting (default: auto-detect TTY)",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="Skip confirmation even in interactive mode",
    )
    parser.add_argument(
        "--top-tweets-hours",
        type=int,
        default=48,
        help="How far back to look for top tweets (default: 48h)",
    )
    parser.add_argument(
        "--top-tweets-terms",
        type=int,
        default=8,
        help="Number of search terms to sample for top tweets (default: 8)",
    )
    parser.add_argument(
        "--batches",
        type=int,
        default=1,
        help="Number of draft batches to generate (default: 1)",
    )
    return parser


def _print_dry_run(top_tweets, ranked, hook_text, reveal_text, fmt):
    """Display dry-run results."""
    print("\n=== DRY-RUN RESULTS ===", flush=True)
    if top_tweets:
        print(f"\n--- Top tweets used ({len(top_tweets)}) ---", flush=True)
        for t in top_tweets[:10]:
            text = (t.get("text") or "").replace("\n", " ")
            print(
                f"  [{t.get('likes', 0)}L {t.get('retweets', 0)}RT] @{t.get('author', '?')}: {text}",
                flush=True,
            )
    print(f"\n--- All ranked candidates ({len(ranked)}) ---", flush=True)
    for i, c in enumerate(ranked):
        hook = (c.get("hook") or "").replace("\n", " ")
        reveal = (c.get("reveal") or "").replace("\n", " ")
        print(
            f"  #{i + 1} (score={c.get('llm_score', '?')}, {c.get('format', '?')}):",
            flush=True,
        )
        print(f"    HOOK: {hook}", flush=True)
        if reveal:
            print(f"    REVEAL: {reveal}", flush=True)
    print(f"\n--- Would post ---", flush=True)
    print(f"  Format: {fmt}", flush=True)
    display_hook = hook_text.rstrip() + " 👇" if reveal_text else hook_text
    print(f"  HOOK: {display_hook}", flush=True)
    if reveal_text:
        print(f"  REVEAL: {reveal_text}", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry_run: bool = args.dry_run

    print("=== TWITTER ORIGINAL CONTENT POSTER ===", flush=True)
    if dry_run:
        print("*** DRY-RUN MODE — will NOT post ***", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    if not dry_run:
        try:
            ensure_browser_ready()
        except RuntimeError as e:
            send_error_alert(f"Browser not ready: {e}")
            print(f"Browser not ready: {e}", file=sys.stderr)
            return 1

    # Fetch top tweets — use broad viral terms for structural inspiration
    top_tweets: list[dict] = []
    try:
        top_tweets = fetch_top_tweets(
            n_terms=args.top_tweets_terms,
            since_hours=args.top_tweets_hours,
            limit_per_term=20,
            viral=True,
            min_engagement=50,
        )
    except Exception as e:
        print(f"Top tweets fetch failed (non-fatal): {e}", flush=True)

    try:
        with get_conn() as conn:
            ensure_schema(conn)

            count = count_posts_today(conn)
            print(f"Posts today: {count}/6", flush=True)
            if count >= 6 and not dry_run:
                print("Already posted 6 original tweets today, skipping")
                return 0

            # Analyze structure of top tweets before generating
            print("Analyzing top tweet structures...", flush=True)
            structure_analysis = _analyze_top_tweet_structures(top_tweets)
            if structure_analysis:
                print("Structure analysis complete", flush=True)
            else:
                print("No structure analysis (will use top tweets directly)", flush=True)

            # Generate batches for diversity
            # Build context once (requires DB), then fan out LLM calls in parallel
            context = _build_context_sections(conn, top_tweets, structure_analysis)
            n_batches = max(1, args.batches)
            temps = [0.7 + i * 0.2 for i in range(n_batches)][:3]  # cap at 3
            print(
                f"Drafting {len(temps)} batches in parallel (T={temps})...", flush=True
            )
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _draft_with_delay(ctx: str, temp: float, delay: float) -> list[dict]:
                if delay > 0:
                    time.sleep(delay)
                return draft_batch(ctx, temp)

            new_entries: list[dict] = []
            with ThreadPoolExecutor(max_workers=len(temps)) as pool:
                futures = {
                    pool.submit(_draft_with_delay, context, t, i * 5): t
                    for i, t in enumerate(temps)
                }
                for fut in as_completed(futures):
                    t = futures[fut]
                    try:
                        batch = fut.result()
                        print(f"  T={t}: {len(batch)} drafts", flush=True)
                        new_entries.extend(batch)
                    except Exception as e:
                        print(f"  T={t}: failed ({e})", flush=True)
            if not new_entries:
                print("All batches failed, falling back to single draft...", flush=True)
                single = draft_single(context)
                new_entries = [single] if single else []

            unposted = _filter_valid_unposted(
                [
                    {"text": e.get("text", ""), "thread_data": e.get("thread_data")}
                    for e in new_entries
                ]
            )
            print(f"Drafted {len(new_entries)}, {len(unposted)} valid", flush=True)

            if not unposted:
                send_error_alert("All drafting failed or produced no valid entries")
                print("No valid tweets drafted")
                return 1

            # Rank candidates (pass recent posts so ranker can reject duplicates)
            recent_posts = [
                p.get("text", "")[:200]
                for p in get_recent_posts(conn, days=14, limit=12)
            ]
            rank_candidates = [_to_rank_candidate(e) for e in unposted[:20]]
            print(f"Ranking {len(rank_candidates)} candidates...", flush=True)
            ranked = llm_rank_candidates(rank_candidates, recent_posts=recent_posts)
            best = ranked[0] if ranked else None
            if best is None:
                print("No valid candidate after ranking", flush=True)
                return 1

            hook_text = best.get("hook", "")
            reveal_text = best.get("reveal", "")
            fmt = best.get("format", "unknown")
            print(
                f"Selected ({fmt}, score={best.get('llm_score', '?')}): {hook_text[:100]}...",
                flush=True,
            )

            # Dry run: display and exit
            if dry_run:
                _print_dry_run(top_tweets, ranked, hook_text, reveal_text, fmt)
                return 0

            # Humanize
            try:
                hook_text = humanize(hook_text)
                if reveal_text:
                    reveal_text = humanize(reveal_text)
                print(f"Humanized hook: {hook_text[:80]}...", flush=True)
            except Exception as e:
                print(
                    f"Humanize error: {e}, proceeding with original draft", flush=True
                )

            # Append thread indicator when there's a reveal coming
            if reveal_text and not hook_text.rstrip().endswith("👇"):
                hook_text = hook_text.rstrip() + " 👇"

            # Interactive confirmation
            interactive = args.confirm or (
                args.confirm is None and not args.no_confirm and sys.stdin.isatty()
            )
            if interactive:
                print(f"\n{'=' * 60}", flush=True)
                print(f"HOOK:   {hook_text}", flush=True)
                if reveal_text:
                    print(f"REVEAL: {reveal_text}", flush=True)
                print(f"{'=' * 60}", flush=True)
                try:
                    answer = input("Post this? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                if answer != "y":
                    print("Skipped.", flush=True)
                    return 0

            # Post hook
            print("Posting hook via browser CDP...", flush=True)
            if not post_tweet(hook_text):
                send_error_alert(
                    f"Failed to post hook via CDP\n\nHook was:\n{hook_text}"
                )
                print("Failed to post hook")
                return 1
            print("Hook posted successfully", flush=True)

            # Get tweet ID before recording
            jitter_sleep(6, 12)
            hook_tweet_id = get_latest_own_tweet_id("DecentCloud_org")

            # Record in DB immediately so the post is tracked even if
            # the reveal CDP call fails and rolls back the transaction.
            insert_post(
                conn,
                tweet_id=hook_tweet_id or f"unknown-{utc_now()}",
                type="post",
                text=hook_text,
            )
            conn.commit()
            print(f"Logged post to DB (tweet_id={hook_tweet_id})", flush=True)

            # Post reveal as self-reply (thread formats only)
            if reveal_text and hook_tweet_id:
                print(f"Posting reveal as reply to {hook_tweet_id}...", flush=True)
                jitter_sleep(3, 6)
                posted_reply, reply_id = post_reply_with_retries(
                    hook_tweet_id, reveal_text, attempts=2
                )
                if posted_reply:
                    print(
                        f"Reveal posted successfully (reply_id={reply_id})", flush=True
                    )
                else:
                    print("Failed to post reveal (hook is still live)", flush=True)
            elif reveal_text:
                print("Could not get hook tweet ID — reveal not posted", flush=True)

        return 0

    except Exception as e:
        send_error_alert(f"Unexpected error: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
