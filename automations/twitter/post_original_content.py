#!/usr/bin/env python3
"""Post original Twitter content for @DecentCloud_org.

Strategy: /projects/Notes/Pickle/Twitter/decent-cloud-twitter-plan.md

Runs daily to ensure consistent original content posting (1-2 tweets/day).

Phase 1 mode: founder voice takes only — no links, no product mentions, no hashtags.

Sources:
1. Morning research results (HN stories, curated links)
2. Dev updates (repo activity)
3. Insights/learnings (from memory, notes)

This complements engagement replies with original value-adding content.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent.parent))  # /projects/automations (for lib.*)
sys.path.insert(0, str(Path(__file__).parent))          # /projects/automations/twitter (for db, twitter_utils)
from lib.llm_utils import call_llm_simple as call_llm, extract_json
from twitter_utils import (
    ensure_browser_ready,
    humanize,
    load_project_context,
    post_tweet,
    send_error_alert,
    utc_now,
    get_latest_own_tweet_id,
)
from db import (
    get_content_queue,
    get_conn,
    get_latest_morning_research,
    count_posts_today,
    ensure_schema,
    insert_content_queue_entries,
    mark_content_queue_posted,
    prune_content_queue,
    get_recent_posts,
    get_recent_engagements,
    get_top_posts,
    get_popular_candidate_tweets,
    insert_post,
)

# Phase 1 hard rules: zero product mentions in original posts
PRODUCT_MENTION_PATTERNS = [
    r"\bwe\s+(just|recently|today|now)?\s*(shipped|launched|built|released|deployed|published)\b",
    r"\bwe\s+just\b",
    r"\bdecent\s*cloud\b",
    r"\bour\s+(platform|product|marketplace|service|tool|app)\b",
    r"\bcheck\s+out\b",
    r"\bsign\s+up\b",
    r"\bwaitlist\b",
    r"\bearly\s+access\b",
    r"\bwe\s+shipped\b",
    r"\bwe\s+launched\b",
    r"\bfull\s+visibility\s+into\b",
    r"\bdeployment\s+recipe\b",
]


def has_product_mention(text: str) -> bool:
    for pattern in PRODUCT_MENTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _try_parse_json_payload(raw: str) -> object | None:
    """Parse JSON from raw model output, tolerating code fences and wrappers."""
    text = (raw or "").strip()
    if not text:
        return None

    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # Extract first balanced JSON array
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, TypeError):
                        start = None
                        continue

    # Extract first balanced JSON object
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, TypeError):
                        start = None
                        continue
    return None


def _looks_like_meta_response(text: str) -> bool:
    t = (text or "").strip().lower()
    return (
        t.startswith("let me ")
        or t.startswith("here are ")
        or t.startswith("generate ")
        or t.startswith("write ")
        or t.startswith("draft ")
        or t.startswith("output ")
        or "analyze the requirements" in t
        or "project & strategy context" in t
        or "format a" in t
        or "format b" in t
        or "json array" in t
        or "each must be a different topic and angle" in t
        or "your task" in t
        or "rules for all posts" in t
        or "output only" in t
        or "recent posts — avoid repeating these angles" in t
        or "avoid repeating recent post angles" in t
        or "follow the voice and style guidelines" in t
        or "topics should align with the content mix" in t
        or "recent posts to avoid" in t
        or "guidelines" in t
        or t.endswith(":")
    )


def _is_valid_candidate_text(text: str) -> bool:
    t = (text or "").strip()
    return 20 <= len(t) <= 280 and not _looks_like_meta_response(t)


def load_queue(conn) -> list[dict]:
    """Load the content queue from typed DB table."""
    return get_content_queue(conn)


def llm_rank_candidates(candidates: list[dict], top_posts: list[dict]) -> list[dict]:
    """Rank unposted candidates by predicted engagement using a single LLM call.

    Returns candidates sorted best-first, each with an 'llm_score' (0-10) added.
    Falls back to original order if the LLM call fails.
    """
    if not candidates:
        return candidates

    # Format top posts as engagement reference
    top_posts_text = ""
    if top_posts:
        lines = []
        for p in top_posts:
            likes = p.get("likes") or 0
            rts = p.get("rts") or 0
            text = (p.get("text") or "").replace("\n", " ")
            lines.append(f"  [{likes}L {rts}RT] {text}")
        top_posts_text = "\n".join(lines)

    # Format candidates with IDs
    candidate_lines = []
    for i, c in enumerate(candidates):
        label = f"C{i+1}"
        text = (c.get("text") or "").replace("\n", " ")
        candidate_lines.append(f"[{label}] {text}")
    candidates_text = "\n\n".join(candidate_lines)

    prompt = f"""You are scoring tweet drafts for @DecentCloud_org by predicted engagement.

## What high-engagement looks like for this account
These are the best-performing posts by likes+retweets. Learn the patterns:

{top_posts_text if top_posts_text else "  (no historical data yet)"}

## Candidates to score

{candidates_text}

## Scoring task

Score each candidate from 0 to 10:
- 10 = will almost certainly spark real discussion, strong opinions, or go viral in the infra/cloud space
- 7-9 = solid, likely to get genuine replies or retweets from practitioners
- 4-6 = decent but forgettable, might get a few likes
- 1-3 = generic, obvious, preaching to the choir, or no hook
- 0 = actively bad (wrong tone, product shill, AI-sounding fluff)

Judge on: specificity of the claim, strength of the tension or insight, how much it forces a reaction.
Do NOT reward safe takes. Reward posts where a senior engineer has a genuine opinion either way.

Return ONLY valid JSON matching this exact schema:
{{
  "rankings": [
    {{"id": "C1", "score": 8, "reason": "one short sentence"}}
  ]
}}

Hard requirements:
- `rankings` length must be exactly {len(candidates)}
- Include each candidate id exactly once: C1..C{len(candidates)}
- `score` must be a number in [0, 10]
- No markdown, no prose, no extra keys at top level"""

    try:
        raw = call_llm(prompt, timeout=60, json_mode=True)
        if not raw:
            print("LLM ranking returned nothing, using original order", file=sys.stderr)
            return candidates

        payload = _try_parse_json_payload(raw)
        if payload is None:
            json_str = extract_json(raw)
            if json_str:
                payload = _try_parse_json_payload(json_str)

        rankings = None
        if isinstance(payload, dict):
            rankings = payload.get("rankings")
        elif isinstance(payload, list):
            rankings = payload
        if not isinstance(rankings, list):
            print("Could not parse LLM ranking response, using original order", file=sys.stderr)
            return candidates

        expected_ids = [f"C{i+1}" for i in range(len(candidates))]
        by_id: dict[str, dict] = {}
        for item in rankings:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id", "")).strip()
            score_raw = item.get("score")
            try:
                score = float(score_raw)
            except (TypeError, ValueError):
                continue
            if cid not in expected_ids:
                continue
            by_id[cid] = {
                "score": max(0.0, min(10.0, score)),
                "reason": str(item.get("reason", "")).strip(),
            }

        if len(by_id) != len(expected_ids):
            print("Incomplete/invalid LLM ranking response, using original order", file=sys.stderr)
            return candidates

        for i, c in enumerate(candidates):
            label = f"C{i+1}"
            c["llm_score"] = by_id[label]["score"]
            reason = by_id[label]["reason"]
            if reason:
                print(f"  {label} (score={c['llm_score']}): {reason}", flush=True)

        ranked = sorted(candidates, key=lambda c: c.get("llm_score", 0), reverse=True)
        return ranked

    except Exception as e:
        print(f"LLM ranking failed: {e}, using original order", file=sys.stderr)
        return candidates


def load_morning_research(conn) -> dict | None:
    """Load latest morning research results from typed DB tables."""
    return get_latest_morning_research(conn)


def get_recent_commits(n: int = 5) -> list[str]:
    """Fetch recent commits from decent-cloud repo."""
    try:
        r = subprocess.run(
            ["git", "-C", "/projects/decent-cloud", "log", f"--oneline", f"-{n}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip().split("\n")
    except Exception:
        pass
    return []


def _get_engagement_themes(recent_engagements: list[dict], n: int = 10) -> list[str]:
    """Extract recurring themes from recent engagements to inform content direction."""
    recent = recent_engagements[-n:]
    terms = [e.get("search_term", "") for e in recent if e.get("search_term")]
    return list(dict.fromkeys(terms))  # deduplicated, order-preserving


def draft_batch(conn) -> list[dict]:
    """Use LLM to draft a batch of 3-5 tweets. Returns list of queue entries."""

    research = load_morning_research(conn)
    recent_posts_db = get_recent_posts(conn, days=14, limit=12)
    recent_engagements_db = get_recent_engagements(conn, hours=168, limit=15)
    top_posts_db = get_top_posts(conn, limit=20)
    popular_candidates_db = get_popular_candidate_tweets(conn, days=30, limit=25)

    # Extended history: last 12 posts for better dedup and thematic continuity
    recent_posts = [p.get("text", "")[:200] for p in recent_posts_db]
    recent_commits = get_recent_commits(6)
    engagement_themes = _get_engagement_themes(recent_engagements_db, n=15)

    # Build top-posts style anchor (only include entries with real engagement data)
    top_posts_with_stats = [p for p in top_posts_db if (p.get("likes") or 0) + (p.get("rts") or 0) > 0]
    top_posts_section = ""
    if top_posts_with_stats:
        lines = []
        for p in top_posts_with_stats:
            likes = p.get("likes") or 0
            rts = p.get("rts") or 0
            text = (p.get("text") or "").replace("\n", " ")
            lines.append(f"  [{likes}L {rts}RT] {text}")
        top_posts_section = "\n## Posts that got the most engagement — write in this style and voice:\n" + "\n".join(lines)

    # Build popular candidate tweets section (trending topics in our target space)
    popular_candidates_section = ""
    if popular_candidates_db:
        lines = []
        for c in popular_candidates_db:
            likes = c.get("likes") or 0
            rts = c.get("retweets") or 0
            text = (c.get("text") or "").replace("\n", " ")
            lines.append(f"  [{likes}L {rts}RT] {text}")
        popular_candidates_section = (
            "\n## What's getting traction in the space right now"
            " — use as angle/topic inspiration only, don't copy verbatim:\n"
            + "\n".join(lines)
        )

    project_context = load_project_context()

    # Build morning research context (HN trending topics + dev activity)
    research_text = ""
    if research:
        parts = []
        if research.get("hnStories"):
            for s in research["hnStories"][:3]:
                parts.append(f'- HN trending: "{s["title"]}" ({s["points"]} pts)')
        if research.get("devActivity"):
            parts.append(f"- Dev activity: {research['devActivity']}")
        if parts:
            research_text = "\n".join(parts)

    # Build dev commits context (fallback if morning research didn't run)
    commits_text = ""
    if not research_text and recent_commits:
        commits_text = "\n".join(f"- {c}" for c in recent_commits[:4])

    prompt = f"""Generate 4 original posts for a technical Twitter account. Mix two formats.

# Project & Strategy Context
{project_context}

# Your Task
Draft 4 posts. Use a mix of Format A and Format B (at least 1 of each). Each must be a DIFFERENT topic and angle.

## Format A — Observational take (short, 1–4 sentences)
Describe something real that most people haven't noticed or named.
Structure: setup (what people assume) → reality (what actually happens, with numbers) → implication (let it land).
NOT imperative. Never "make sure you", "you should". Describe; don't instruct.
Example: "Stripe can quietly hold back 10% of your revenue for up to 6 months if their system flags your account — rapid growth is often enough to trigger it."
Go for the uncomfortable truth over the comfortable observation.
"Everyone knows X" posts don't land — find the contradiction.
Safe: "Cloud migrations are hard." Spicy: "Most cloud migrations cost 3x because enterprises are trading human problems for API problems."

## Format B — Engineering dilemma (longer, structured, ends with a question)
A realistic scenario with no obvious correct answer. Forces the reader to pick a side.
Requirements:
- Concrete system snapshot: team size, traffic/RPS, deploy time, incidents/month, DB shape
- At least one time or org constraint (deadline, hiring freeze, audit requirement)
- At least one contradictory signal ("it works fine, but…")
- Both options clearly defensible — no cartoon villain choice
- Ends with 1–2 direct decision questions
The question must NOT have an obvious right answer. A senior engineer must be able to argue either side confidently.
Bad: "Should you rewrite a clearly broken system?" Good: "300ms P99, 2 engineers, Series A in 3 months — do you start the migration or wait?"

## Recent posts — AVOID repeating these angles (last 12):
{json.dumps(recent_posts, indent=2)}
{top_posts_section}
{popular_candidates_section}
## Audience engagement signal — active topics recently:
{json.dumps(engagement_themes, indent=2) if engagement_themes else "  (no data yet)"}
Use as inspiration for fresh angles only — don't repeat same framing.

{f"## Trending today (inspiration only — NO links in posts):{chr(10)}{research_text}" if research_text else ""}
{f"## Recent dev activity (for inspiration):{chr(10)}{commits_text}" if commits_text else ""}

## Rules for all posts
- ALWAYS capitalize the first word of every post. Standard sentence capitalization throughout: proper nouns (AWS, GCP, Stripe, etc.) capitalized, everything else normal case. BAD: "everyone claims..." GOOD: "Everyone claims..."
- No hashtags, no links, no product mentions, no "Decent Cloud" references
- No AI vocabulary: "Furthermore", "Additionally", "It's important to note that"
- Name specific platforms (AWS, GCP, Azure, Stripe) when discussing their gotchas. "The cloud" is vague; "AWS egress fees" is interesting.

Output as JSON array of 4 strings: ["post1", "post2", "post3", "post4"]
Output ONLY the JSON array. No explanation, no markdown wrapping."""

    try:
        raw = call_llm(prompt, timeout=120, json_mode=True)
        if not raw:
            print("LLM returned nothing for batch draft", file=sys.stderr)
            return []

        payload = _try_parse_json_payload(raw)
        tweets = payload if isinstance(payload, list) else None
        if not tweets:
            print(f"Failed to parse batch draft JSON: {raw[:200]}", file=sys.stderr)
            return []

        now = datetime.now(timezone.utc).isoformat()
        entries = []
        for text in tweets:
            if not isinstance(text, str):
                continue
            text = text.strip().strip('"').strip("'")
            # Strip LLM categorization prefixes like "(Observation/DR)", "(Technical insight)", etc.
            text = re.sub(r"^\([^)]{3,30}\)\s*", "", text).strip()
            if not text or len(text) < 20:
                print(f"Batch draft rejected: too short ({len(text)} chars)", file=sys.stderr)
                continue
            if _looks_like_meta_response(text):
                print(f"Batch draft rejected: meta response: {text[:80]}", file=sys.stderr)
                continue
            if has_product_mention(text):
                print(
                    f"Batch draft rejected: Phase 1 violation: {text[:80]}",
                    file=sys.stderr,
                )
                continue
            entries.append(
                {
                    "text": text,
                    "draftedAt": now,
                    "llm_score": 0,  # set at selection time by llm_rank_candidates()
                    "posted": False,
                }
            )

        print(
            f"Batch drafted {len(entries)} valid tweets from {len(tweets)} candidates",
            flush=True,
        )
        return entries

    except Exception as e:
        print(f"LLM batch draft failed: {e}", file=sys.stderr)
        return []


def draft_single(conn) -> dict | None:
    """Draft a single tweet using the proven single-tweet prompt. Fallback for batch failures."""
    recent_posts_db = get_recent_posts(conn, days=14, limit=12)
    recent_engagements_db = get_recent_engagements(conn, hours=168, limit=10)
    top_posts_db = get_top_posts(conn, limit=20)
    popular_candidates_db = get_popular_candidate_tweets(conn, days=30, limit=25)

    recent_posts = [p.get("text", "")[:200] for p in recent_posts_db]
    engagement_themes = _get_engagement_themes(recent_engagements_db, n=10)
    research = load_morning_research(conn)

    top_posts_with_stats = [p for p in top_posts_db if (p.get("likes") or 0) + (p.get("rts") or 0) > 0]
    top_posts_section = ""
    if top_posts_with_stats:
        lines = []
        for p in top_posts_with_stats:
            likes = p.get("likes") or 0
            rts = p.get("rts") or 0
            text = (p.get("text") or "").replace("\n", " ")
            lines.append(f"  [{likes}L {rts}RT] {text}")
        top_posts_section = "\n## Posts that got the most engagement — write in this style and voice:\n" + "\n".join(lines) + "\n"

    popular_candidates_section = ""
    if popular_candidates_db:
        lines = []
        for c in popular_candidates_db:
            likes = c.get("likes") or 0
            rts = c.get("retweets") or 0
            text = (c.get("text") or "").replace("\n", " ")
            lines.append(f"  [{likes}L {rts}RT] {text}")
        popular_candidates_section = (
            "\n## What's getting traction in the space right now"
            " — use as angle/topic inspiration only, don't copy verbatim:\n"
            + "\n".join(lines)
            + "\n"
        )

    project_context = load_project_context()

    research_text = ""
    if research:
        parts = []
        if research.get("hnStories"):
            for s in research["hnStories"][:2]:
                parts.append(f'- HN trending: "{s["title"]}" ({s["points"]} pts)')
        if research.get("devActivity"):
            parts.append(f"- Dev activity: {research['devActivity']}")
        if parts:
            research_text = "\n".join(parts)

    prompt = f"""You are drafting one original post for a technical Twitter account.

# Project & Strategy Context
{project_context}

# Your Task
Draft ONE post. Choose whichever format below produces the strongest result.

## Format A — Observational take (short, 1–4 sentences)
Describe something real that most people haven't noticed or named.
Structure: setup (what people assume) → reality (with numbers/specifics) → implication (let it land, don't prescribe a fix).
NOT imperative. Never "make sure you", "you should". Describe; don't instruct.

## Format B — Engineering dilemma (longer, structured, ends with a question)
A realistic scenario with no obvious correct answer. Forces the reader to pick a side.
- Concrete system snapshot (team size, traffic, deploy time, incidents, DB shape)
- At least one time or org constraint and one contradictory signal
- Both options defensible — no cartoon villain choice
- Ends with 1–2 direct decision questions with no obvious right answer

## Recent posts — avoid repeating these angles (last 12):
{json.dumps(recent_posts, indent=2)}
{top_posts_section}
{popular_candidates_section}
## Audience engagement signal:
{json.dumps(engagement_themes, indent=2) if engagement_themes else "  (no data yet)"}

{f"## Trending today (inspiration only — NO links):{chr(10)}{research_text}" if research_text else ""}

## Rules
- ALWAYS capitalize the first word. Standard sentence capitalization: proper nouns (AWS, GCP, Stripe, etc.) capitalized, everything else normal case. BAD: "everyone claims..." GOOD: "Everyone claims..."
- No hashtags, no links, no product mentions, no "Decent Cloud" references

Output ONLY the post text. No quotes, no JSON, no markdown."""

    try:
        text = call_llm(prompt, timeout=120)
        if not text:
            return None

        text = text.strip().strip('"').strip("'").strip("`")
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        # Strip categorization prefixes
        text = re.sub(r"^\([^)]{3,30}\)\s*", "", text).strip()
        # If multi-line, take the best substantive line (LLM thinking before answer)
        if "\n" in text:
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            # Find the best candidate line (longest line that looks like a tweet)
            candidates = [
                l
                for l in lines
                if len(l) >= 20
                and not l.startswith("*")
                and not l.startswith("#")
            ]
            if candidates:
                text = max(candidates, key=len)
            elif lines:
                text = lines[-1]

        if not text or len(text) < 20:
            return None
        if has_product_mention(text):
            return None

        return {
            "text": text,
            "draftedAt": datetime.now(timezone.utc).isoformat(),
            "llm_score": 0,  # set at selection time by llm_rank_candidates()
            "posted": False,
        }
    except Exception as e:
        print(f"Single draft failed: {e}", file=sys.stderr)
        return None


def main() -> int:
    print("=== TWITTER ORIGINAL CONTENT POSTER ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    # Pre-flight: ensure Chrome CDP is reachable and clear any blocking dialogs.
    try:
        ensure_browser_ready()
    except RuntimeError as e:
        send_error_alert(f"Browser not ready: {e}")
        print(f"Browser not ready: {e}", file=sys.stderr)
        return 1

    try:
        with get_conn() as conn:
            ensure_schema(conn)
            # Check if we've already posted enough today
            count = count_posts_today(conn)
            print(f"Posts today: {count}/5", flush=True)

            if count >= 5:
                print("Already posted 5 original tweets today, skipping")
                return 0

            # Load queue and clean up old posted entries (>7 days)
            pruned = prune_content_queue(conn, posted_older_than_days=7)
            if pruned:
                print(f"Pruned {pruned} old posted queue entries", flush=True)
            queue = load_queue(conn)
            unposted = [entry for entry in queue if not entry.get("posted")]
            invalid = [e for e in unposted if not _is_valid_candidate_text(e.get("text", ""))]
            if invalid:
                print(f"Ignoring {len(invalid)} invalid queued draft(s)", flush=True)
                now = datetime.now(timezone.utc)
                for entry in invalid:
                    if entry.get("id") is not None:
                        mark_content_queue_posted(conn, int(entry["id"]), now)
            unposted = [e for e in unposted if _is_valid_candidate_text(e.get("text", ""))]
            print(f"Queue: {len(queue)} total, {len(unposted)} unposted", flush=True)

            # If <3 unposted entries, draft a batch (keep buffer ahead of 5/day demand)
            if len(unposted) < 3:
                print("Queue low, drafting batch...", flush=True)
                new_entries = draft_batch(conn)
                if new_entries:
                    inserted = insert_content_queue_entries(conn, new_entries)
                    print(f"Inserted {inserted} drafted entries into queue", flush=True)
                    queue = load_queue(conn)
                    unposted = [entry for entry in queue if not entry.get("posted")]
                    unposted = [e for e in unposted if _is_valid_candidate_text(e.get("text", ""))]
                    print(f"Queue after batch: {len(unposted)} unposted", flush=True)
                else:
                    # Fallback: draft a single tweet (more reliable with GLM-5)
                    print("Batch failed, falling back to single draft...", flush=True)
                    single = draft_single(conn)
                    if single:
                        insert_content_queue_entries(conn, [single])
                        queue = load_queue(conn)
                        unposted = [entry for entry in queue if not entry.get("posted")]
                        unposted = [e for e in unposted if _is_valid_candidate_text(e.get("text", ""))]
                        print(
                            f"Queue after single draft: {len(unposted)} unposted",
                            flush=True,
                        )
                    else:
                        print("Single draft also failed", flush=True)

            if not unposted:
                send_error_alert("Content queue empty and all drafting failed")
                print("No tweets available in queue")
                return 1

            # Rank a bounded candidate set to keep ranking prompt reliably parseable.
            rank_pool = unposted[:20]
            top_posts = get_top_posts(conn, limit=20)
            print(f"Ranking {len(rank_pool)} candidates against {len(top_posts)} top posts...", flush=True)
            ranked = llm_rank_candidates(rank_pool, top_posts)
            best = next((c for c in ranked if _is_valid_candidate_text(c.get("text", ""))), None)
            if best is None:
                print("No valid candidate text after ranking", flush=True)
                return 1
            draft = best["text"]
            print(
                f"Selected (llm_score={best.get('llm_score', '?')}): {draft[:80]}...", flush=True
            )

            # Humanize (mandatory per strategy doc)
            try:
                draft = humanize(draft)
                print(f"Humanized: {draft[:80]}...", flush=True)
            except Exception as e:
                print(f"Humanize error: {e}, proceeding with original draft", flush=True)

            # Post
            print("Posting via browser CDP...", flush=True)
            if not post_tweet(draft):
                send_error_alert(f"Failed to post via CDP\n\nDraft was:\n{draft}")
                print("Failed to post")
                return 1

            print("Posted successfully", flush=True)

            # Mark as posted in queue
            if best.get("id") is not None:
                mark_content_queue_posted(conn, int(best["id"]), datetime.now(timezone.utc))

            # Get the tweet ID from our profile for tracking
            from twitter_utils import jitter_sleep
            jitter_sleep(6, 12)
            tweet_id = get_latest_own_tweet_id("DecentCloud_org")

            # Insert into posts table with deadlock retry.
            for attempt in range(1, 4):
                try:
                    insert_post(
                        conn,
                        tweet_id=tweet_id or f"unknown-{utc_now()}",
                        type="post",
                        text=draft,
                    )
                    break
                except psycopg2.errors.DeadlockDetected:
                    if attempt == 3:
                        raise
                    wait = 0.8 * attempt
                    print(f"insert_post deadlock; retrying in {wait:.1f}s (attempt {attempt}/3)", flush=True)
                    time.sleep(wait)
            print(f"Logged post to DB (tweet_id={tweet_id})", flush=True)

        return 0

    except Exception as e:
        send_error_alert(f"Unexpected error: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
