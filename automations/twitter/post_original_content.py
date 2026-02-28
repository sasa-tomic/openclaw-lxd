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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # /projects/automations (for lib.*)
sys.path.insert(0, str(Path(__file__).parent))          # /projects/automations/twitter (for db, twitter_utils)
from lib.llm_utils import call_llm_simple as call_llm, extract_json
from twitter_utils import (
    humanize,
    load_project_context,
    post_tweet,
    send_error_alert,
    utc_now,
    get_latest_own_tweet_id,
)
from db import (
    get_conn,
    count_posts_today,
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


MORNING_RESEARCH_CACHE = Path("/tmp/twitter-morning-research.json")
QUEUE_PATH = Path("/home/openclaw/clawd/memory/twitter-content-queue.json")


def load_queue() -> list[dict]:
    """Load the content queue from disk."""
    if QUEUE_PATH.exists():
        try:
            return json.loads(QUEUE_PATH.read_text())
        except Exception:
            return []
    return []


def save_queue(queue: list[dict]) -> None:
    """Atomically save the content queue to disk."""
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(QUEUE_PATH)


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

Output ONLY a JSON array — one object per candidate, in the same order:
[{{"id": "C1", "score": 8, "reason": "one short sentence"}}, ...]"""

    try:
        raw = call_llm(prompt, timeout=60)
        if not raw:
            print("LLM ranking returned nothing, using original order", file=sys.stderr)
            return candidates

        json_str = extract_json(raw)
        if not json_str:
            # Try finding a JSON array directly
            import re as _re
            m = _re.search(r'\[.*\]', raw, _re.DOTALL)
            json_str = m.group(0) if m else None
        if not json_str:
            print("Could not parse LLM ranking response, using original order", file=sys.stderr)
            return candidates

        scores = json.loads(json_str)
        score_map = {s["id"]: s["score"] for s in scores if "id" in s and "score" in s}

        for i, c in enumerate(candidates):
            label = f"C{i+1}"
            c["llm_score"] = score_map.get(label, 0)
            if label in score_map:
                reason = next((s.get("reason", "") for s in scores if s.get("id") == label), "")
                if reason:
                    print(f"  {label} (score={c['llm_score']}): {reason}", flush=True)

        ranked = sorted(candidates, key=lambda c: c.get("llm_score", 0), reverse=True)
        return ranked

    except Exception as e:
        print(f"LLM ranking failed: {e}, using original order", file=sys.stderr)
        return candidates


def load_morning_research() -> dict | None:
    """Load cached morning research results if available."""
    if not MORNING_RESEARCH_CACHE.exists():
        return None

    try:
        return json.loads(MORNING_RESEARCH_CACHE.read_text())
    except Exception:
        return None


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

    research = load_morning_research()
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
- Standard sentence capitalization: capitalize the first word and proper nouns (AWS, GCP, Stripe, etc.)
- No hashtags, no links, no product mentions, no "Decent Cloud" references
- No AI vocabulary: "Furthermore", "Additionally", "It's important to note that"
- Name specific platforms (AWS, GCP, Azure, Stripe) when discussing their gotchas. "The cloud" is vague; "AWS egress fees" is interesting.

Output as JSON array of 4 strings: ["post1", "post2", "post3", "post4"]
Output ONLY the JSON array. No explanation, no markdown wrapping."""

    try:
        raw = call_llm(prompt, timeout=120)
        if not raw:
            print("LLM returned nothing for batch draft", file=sys.stderr)
            return []

        # Try to extract JSON array from the response
        raw = raw.strip()
        # Strip markdown fences
        if raw.startswith("```json"):
            raw = raw[7:]
        if raw.startswith("```"):
            raw = raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

        # Try direct parse as array
        tweets = None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                tweets = parsed
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: try extract_json (finds objects), then look for array
        if tweets is None:
            # Try to find a JSON array in the text
            bracket_depth = 0
            start = None
            for i, ch in enumerate(raw):
                if ch == "[":
                    if bracket_depth == 0:
                        start = i
                    bracket_depth += 1
                elif ch == "]":
                    bracket_depth -= 1
                    if bracket_depth == 0 and start is not None:
                        candidate = raw[start : i + 1]
                        try:
                            parsed = json.loads(candidate)
                            if isinstance(parsed, list):
                                tweets = parsed
                                break
                        except (json.JSONDecodeError, ValueError):
                            start = None
                            continue

        # Fallback: parse as numbered/line-separated plain text
        if not tweets or not isinstance(tweets, list):
            lines = raw.split("\n")
            text_tweets = []
            for line in lines:
                line = line.strip()
                # Strip numbering like "1.", "1)", "1:", "- "
                line = re.sub(r"^[\d]+[.):\-]\s*", "", line).strip()
                line = re.sub(r"^[-•]\s*", "", line).strip()
                # Strip surrounding quotes
                line = line.strip('"').strip("'").strip("`")
                if line and 20 <= len(line) <= 280:
                    text_tweets.append(line)
            if text_tweets:
                tweets = text_tweets
                print(
                    f"Parsed {len(tweets)} tweets from plain text response", flush=True
                )
            else:
                print(f"Failed to parse batch draft: {raw[:200]}", file=sys.stderr)
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
    research = load_morning_research()

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
- Standard sentence capitalization: capitalize first word and proper nouns (AWS, GCP, Stripe, etc.)
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

    try:
        with get_conn() as conn:
            # Check if we've already posted enough today
            count = count_posts_today(conn)
            print(f"Posts today: {count}/5", flush=True)

            if count >= 5:
                print("Already posted 5 original tweets today, skipping")
                return 0

            # Load queue and clean up old posted entries (>7 days)
            queue = load_queue()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            queue = [
                entry
                for entry in queue
                if not (entry.get("posted") and entry.get("postedAt", "") < cutoff)
            ]
            unposted = [entry for entry in queue if not entry.get("posted")]
            print(f"Queue: {len(queue)} total, {len(unposted)} unposted", flush=True)

            # If <3 unposted entries, draft a batch (keep buffer ahead of 5/day demand)
            if len(unposted) < 3:
                print("Queue low, drafting batch...", flush=True)
                new_entries = draft_batch(conn)
                if new_entries:
                    queue.extend(new_entries)
                    save_queue(queue)
                    unposted = [entry for entry in queue if not entry.get("posted")]
                    print(f"Queue after batch: {len(unposted)} unposted", flush=True)
                else:
                    # Fallback: draft a single tweet (more reliable with GLM-5)
                    print("Batch failed, falling back to single draft...", flush=True)
                    single = draft_single(conn)
                    if single:
                        queue.append(single)
                        save_queue(queue)
                        unposted = [entry for entry in queue if not entry.get("posted")]
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

            # Rank all unposted candidates with a single LLM call
            top_posts = get_top_posts(conn, limit=20)
            print(f"Ranking {len(unposted)} candidates against {len(top_posts)} top posts...", flush=True)
            ranked = llm_rank_candidates(unposted, top_posts)
            best = ranked[0]
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
            best["posted"] = True
            best["postedAt"] = datetime.now(timezone.utc).isoformat()
            save_queue(queue)

            # Get the tweet ID from our profile for tracking
            from twitter_utils import jitter_sleep
            jitter_sleep(6, 12)
            tweet_id = get_latest_own_tweet_id("DecentCloud_org")

            # Insert into posts table
            insert_post(
                conn,
                tweet_id=tweet_id or f"unknown-{utc_now()}",
                type="post",
                text=draft,
            )
            print(f"Logged post to DB (tweet_id={tweet_id})", flush=True)

        return 0

    except Exception as e:
        send_error_alert(f"Unexpected error: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
