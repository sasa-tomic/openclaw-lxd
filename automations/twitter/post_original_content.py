#!/usr/bin/env python3
"""Post original Twitter content for @DecentCloud_org.

Strategy: /projects/Notes/Pickle/Twitter/decent-cloud-twitter-plan.md

Runs daily to ensure consistent original content posting (1-2 tweets/day).
Phase 1 mode: founder voice — no links, no product mentions, no hashtags.
This complements engagement replies with original value-adding content.

Requirements (keep these in sync when changing generation logic):

1. THREAD STRUCTURE — every post is a hook tweet + self-reply reveal.
   The hook stops the scroll (curiosity gap, shock, forced participation).
   The reveal delivers the payoff (specific numbers, receipts, consequences).
   Standalone formats (reply_with_number, impossible_quiz) skip the reveal.

2. NARRATIVE & VOICE — each thread must read like someone telling a story at a bar,
   not a dry incident report. Write in active, not passive voice. The reader must feel
   "oh no, I've been there" or "wait, WHAT?"
   - Hook must end on an unresolved moment: a reaction, disbelief, a question.
     ("I thought AWS made a billing error." / "Nobody could explain the number.")
   - Reveal must directly continue from the hook's unresolved moment.
     ("They didn't. Here's what actually happened..." / "Turns out...")
   - Reveal must close the story — what you did about it, what you learned,
     what happened next. Never trail off with a dangling fact.

3. SIX ENGAGEMENT FORMATS (see ENGAGEMENT_FORMATS constant):
   cliffhanger, deliberately_wrong, reply_with_number,
   confession, math_nobody_did, impossible_quiz.
   Each batch must use a DIFFERENT format per thread.

4. SPECIFICITY — every thread must contain real company names (AWS, GCP, Azure,
   Cloudflare, Vercel, etc.) and realistic numbers (dollar amounts, percentages,
   timeframes). No generic "cloud is expensive" takes.

5. PARTICIPATION — at least some formats must force replies:
   open-ended questions ("What would you do?", "What do you cut first?"),
   "reply with your number", deliberately wrong claims people must correct.

6. ANTI-AI VOICE — no hedge words, no "Furthermore/Additionally/crucial/landscape",
   no "It's worth noting." Standard capitalization, complete sentences.
   No hashtags, no links, no "Decent Cloud", no product mentions.

7. REAL SIGNALS — creative seeds are extracted from actually-trending top tweets
   (fetched via f=top search). The LLM riffs on these, never copies.

8. FACTUAL ACCURACY — nothing we post should be technically wrong or obvious
   to anyone with basic tech knowledge. Every number must be plausible and
   defensible. Every claim must survive a reply from a senior engineer.
   "deliberately_wrong" hooks are the ONE exception — and even those MUST
   have a reveal that corrects them with real proof. If someone screenshots
   only the hook, the reveal must be clearly a self-reply correction thread.

9. DRY-RUN CLI — `--dry-run` previews all ranked candidates with scores,
   formats, hooks, and reveals without posting anything.
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

sys.path.insert(0, str(Path(__file__).parent.parent))   # lib.*
sys.path.insert(0, str(Path(__file__).parent))           # db, twitter_utils

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

STANDALONE_FORMATS = {"reply_with_number", "impossible_quiz"}

ENGAGEMENT_FORMATS = """
## The 6 engagement formats — you MUST use a DIFFERENT format for each tweet

## VOICE (applies to ALL formats)
Write like you're telling a friend what happened — not writing a post-mortem.
Active voice - NOT passive. Short sentences. The reader must feel the story, not read a report.
When you cite numbers, REACT to them like a human would. Don't just list "X at $Y each" —
show that you felt something. "We had 340,000 of them. At $0.05 each." hits differently
than "340,000 custom metrics at $0.05 each."
BAD: "The deployment was misconfigured. Several services experienced degraded performance for approximately 47 minutes."
GOOD: "I fat-fingered a deploy config and took down prod for 47 minutes. My Slack hit 200 notifications before I could even open the dashboard."

## HOOK → REVEAL CONNECTION (applies to all thread formats)
The hook must end on something UNRESOLVED — a reaction, disbelief, a question the reader needs answered.
The reveal must OPEN by directly answering that unresolved moment, then CLOSE the story (what you did, what you learned).
BAD hook ending: "The costs were higher than expected." (flat, no tension)
BAD reveal opening: "Data transfer fees apply between availability zones." (disconnected lecture)
GOOD hook ending: "I checked the bill expecting $200. It said $1,100."
GOOD reveal opening: "Every Lambda cold start was pulling container images through our NAT Gateway."

### 1. CLIFFHANGER THREAD (format: "cliffhanger")
Hook: Tell a real story. Build tension. End on your reaction or disbelief — make the reader NEED to know what happened.
Reveal: Open by answering the hook's unresolved moment. Explain what actually happened. End with what you did about it.
ACCURACY: Every number must be realistic for that service/company. A senior engineer must think "yeah, that tracks."
Example hook: "Our Datadog bill quietly crossed $18,000/month. We're a 12-person startup. Nobody approved this."
Example reveal: "APM auto-instrumentation was indexing every database query as a separate custom metric. We had 340,000 of them. At $0.05 each. I added one sampling filter and it dropped to $3,200. Nobody tells you this during onboarding."

### 2. DELIBERATELY WRONG (format: "deliberately_wrong")
Hook: State something boldly, confidently wrong. So wrong that experts physically cannot stop themselves from replying.
Not slightly off — provocative bait. Think "hot take that triggers the reply guys."
Reveal: The correction with real, verifiable facts. Show the receipt. End with the punchline.
CRITICAL: The hook is SATIRE — it must read as provocation, not ignorance.
Example hook: "Kubernetes saves money. That's just a fact at this point."
Example reveal: "EKS charges you $73/month before a single pod even runs. That's just the control plane. Datadog's Container Report says average cluster CPU utilization is under 20%. You're paying for a scheduler to efficiently waste 80% of your compute."

### 3. REPLY WITH YOUR NUMBER (format: "reply_with_number") [STANDALONE — no reveal needed]
One tweet that asks for a SPECIFIC number from their experience. People love comparing.
Make it about money, time, or pain. Share yours to anchor the conversation.
ACCURACY: Your number must be realistic and relatable.
Example: "How many SaaS subscriptions is your company paying for that nobody has logged into in 6 months? Mine is 11."

### 4. CONFESSION (format: "confession")
Hook: Admit something most people would hide. Specific and slightly reckless. End with a detail that makes the reader gasp.
Reveal: Open by explaining WHY you did it. End with the consequence or what you discovered.
ACCURACY: Must be something a competent engineer might actually do for pragmatic reasons — not genuine negligence.
Example hook: "I deleted our entire staging environment 6 months ago. Nobody has noticed."
Example reveal: "It cost us $3,200/mo, had stale data, different configs from prod, and hadn't caught a single bug in 5 months. Turns out everyone was deploying behind feature flags anyway. Still off. That's $38K/year we were lighting on fire."

### 5. MATH NOBODY DID (format: "math_nobody_did")
Hook: Tease a calculation that reveals a hidden truth. End with your reaction to the result — make the reader NEED to see the numbers.
Reveal: Show the actual breakdown. Every step must be checkable. End with the punchline number.
ACCURACY: Every number must be internally consistent and match real pricing/industry data. If someone checks your math, it must hold up.
Example hook: "I calculated what percentage of our AWS bill is 'platform tax' — services that only exist because other AWS services are missing features."
Example reveal: "NAT Gateway: $220/mo because Lambda can't reach the internet without one. Cross-AZ transfer: $340/mo because AWS spreads your stuff across AZs then charges you for the traffic between them. CloudWatch: $180/mo to see what your own services are doing. That's $740 — 14% of our $5,200 bill goes to paying AWS to fix AWS."

### 6. IMPOSSIBLE QUIZ (format: "impossible_quiz") [STANDALONE — no reveal needed]
Ask a single factual question whose true answer is so counterintuitive that most
engineers will confidently guess wrong. The question alone must be the entire tweet —
no hints, no framing, no call-to-action, no instructions to the reader.
ACCURACY: The true answer MUST be verifiable. If someone Googles it, it checks out.
The answer must surprise practitioners — if most engineers already know it, it's not
a quiz, it's trivia. "How fast are leaked AWS keys exploited?" fails because everyone
knows it's minutes.
Example: "What percentage of S3 buckets in production right now have public read access?"
"""

# Prompt rules shared by batch and single drafting
_PROMPT_RULES = """# Rules
- No hashtags, no links, no "Decent Cloud", no product mentions.
- No AI words: "Furthermore", "Additionally", "crucial", "landscape", "It's worth noting."
- Standard capitalization. Complete sentences only.
- Hook: max 280 chars. Reveal: max 280 chars.
- Use REAL company names (AWS, GCP, Azure, Cloudflare, Vercel, etc.) and realistic numbers.
- Each thread must be about a DIFFERENT topic. No two threads about the same company or issue.
- DO NOT rehash any scenario from the "RECENTLY POSTED" list — not even with different
  wording. Same plot with new numbers is still a duplicate. If a recent post covered
  "enterprise customer demands SOC2", you MUST NOT write another SOC2-compliance thread.
  Pick a completely different situation.

# Voice — write like a person, not a report
- First person (I/we). You're telling a story, not filing an incident report.
- Short punchy sentences. Read it out loud — if it sounds stiff, rewrite it.
- Hook MUST end on something unresolved (your reaction, disbelief, a question).
- Reveal MUST open by directly answering the hook's unresolved moment.
- Reveal MUST close the story (what you did about it, what happened next).
  Never end a reveal on a dangling fact with no resolution.

# Accuracy — non-negotiable
- Every number (dollar amounts, percentages, timeframes) must be REALISTIC for that company/service.
  A senior engineer must read it and think "yeah, that tracks" — never "that's made up."
- Do NOT invent statistics. Use numbers that match real pricing pages and industry reports.
- Do NOT state something obviously wrong or dumb unless it's a "deliberately_wrong" format hook
  (and even then, the reveal must correct it with real facts).
- The bar: if a Hacker News commenter would reply "this is wrong because..." with a factual
  correction, the tweet fails. Every claim must survive scrutiny."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_META_PREFIXES = (
    "let me ", "here are ", "generate ", "write ", "draft ",
    "output ", "consider ", "thinking about ",
)
_META_SUBSTRINGS = (
    "analyze the requirements", "project & strategy context", "json array",
    "your task", "rules for all posts", "output only", "guidelines",
    "something about ", "let me think", "that's good", "this needs to be",
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

    if entry.get("format") in STANDALONE_FORMATS:
        return True

    if not reveal or not _is_valid_tweet_text(reveal) or has_product_mention(reveal):
        return False
    return True


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def _clean_llm_text(text: str) -> str:
    """Strip quotes and LLM categorization prefixes like '(Observation/DR)'."""
    t = (text or "").strip().strip('"').strip("'")
    return re.sub(r"^\([^)]{3,30}\)\s*", "", t).strip()


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

def _extract_anchors_from_tweets(tweets: list[dict]) -> list[str]:
    """Extract real talking points from fetched top tweets."""
    snippets: list[str] = []
    for t in (tweets or [])[:25]:
        text = t.get("text") or ""
        engagement = t.get("likes", 0) + t.get("retweets", 0)
        if engagement < 3 or len(text) < 30:
            continue
        sentences = re.split(r'(?<=[.!?])\s+', text.replace("\n", " "))
        snippet = " ".join(sentences[:2]).strip()[:200]
        snippets.append(f"[{engagement} engagements] {snippet}")
    return snippets


def _build_creative_seed(top_tweets: list[dict] | None) -> str:
    """Build a creative seed block from real signals."""
    anchors = _extract_anchors_from_tweets(top_tweets or [])
    parts = []
    if anchors:
        sample = random.sample(anchors, min(4, len(anchors)))
        parts.append("## What's getting engagement RIGHT NOW (riff on these, don't copy):")
        parts.extend(f"  - {a}" for a in sample)
    parts.append(f"\nDiversity seed: {random.randint(1000, 9999)}")
    return "\n".join(parts) + "\n"


def _format_top_tweets(top_tweets: list[dict] | None) -> str:
    """Format top tweets as structural examples for the prompt."""
    if not top_tweets:
        return ""
    lines = []
    for t in top_tweets[:20]:
        text = (t.get("text") or "").replace("\n", " ")
        lines.append(f"  [{t.get('likes', 0)}L {t.get('retweets', 0)}RT] @{t.get('author', '?')}: {text}")
    return "\n## High-performing tweets — study structure:\n" + "\n".join(lines) + "\n"


def get_recent_commits(n: int = 25) -> list[str]:
    """Fetch recent commits from decent-cloud repo."""
    try:
        r = subprocess.run(
            ["git", "-C", "/projects/decent-cloud", "log", "--oneline", f"-{n}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip().split("\n")
    except Exception:
        pass
    return []


def _build_context_sections(conn, top_tweets: list[dict] | None = None) -> str:
    """Build the shared context sections used by both batch and single draft prompts."""
    recent_posts = [p.get("text", "")[:200] for p in get_recent_posts(conn, days=14, limit=12)]
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

    return f"""# Context
{project_context}

{_build_creative_seed(top_tweets)}

{_format_top_tweets(top_tweets)}

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
                mark_content_queue_posted(conn, int(e["id"]), datetime.now(timezone.utc))
        else:
            # Legacy entries without thread_data
            if _is_valid_tweet_text(e.get("text", "")):
                valid.append(e)
            elif conn and e.get("id") is not None:
                mark_content_queue_posted(conn, int(e["id"]), datetime.now(timezone.utc))
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


def draft_batch(conn, top_tweets: list[dict] | None = None) -> list[dict]:
    """Draft a batch of 6 engagement-optimized thread entries."""
    context = _build_context_sections(conn, top_tweets)

    prompt = f"""Write 6 tweet threads. Each MUST use a DIFFERENT engagement format.
Each thread consists of a HOOK tweet and (for most formats) a REVEAL self-reply.

The reader sees 200 tweets per session. Your hook must be the one that makes them STOP and click.
The reveal must DELIVER — specific numbers, real consequences, receipts.

{ENGAGEMENT_FORMATS}

{context}

{_PROMPT_RULES}

Output ONLY a JSON array. Each element:
{{"format": "cliffhanger|deliberately_wrong|reply_with_number|confession|math_nobody_did|impossible_quiz", "hook": "the main tweet", "reveal": "the self-reply (empty string if standalone format)"}}

No markdown, no explanation. Just the JSON array of 6 objects."""

    try:
        raw = call_llm(prompt, timeout=180, json_mode=True, temperature=0.9)
        if not raw:
            print("LLM returned nothing for batch draft", file=sys.stderr)
            return []

        items = _parse_llm_json(raw)
        if not isinstance(items, list):
            print(f"Failed to parse batch draft JSON: {raw[:200]}", file=sys.stderr)
            return []

        entries = [e for item in items if (e := _build_thread_entry(item)) is not None]
        print(f"Batch drafted {len(entries)} valid threads from {len(items)} candidates", flush=True)
        return entries

    except Exception as e:
        print(f"LLM batch draft failed: {e}", file=sys.stderr)
        return []


def draft_single(conn, top_tweets: list[dict] | None = None) -> dict | None:
    """Draft a single engagement thread. Fallback for batch failures."""
    context = _build_context_sections(conn, top_tweets)

    fmt_choice = random.choice([
        "cliffhanger", "deliberately_wrong", "reply_with_number",
        "confession", "math_nobody_did", "impossible_quiz",
    ])

    prompt = f"""Write ONE tweet thread using the "{fmt_choice}" format.

{ENGAGEMENT_FORMATS}

{context}

{_PROMPT_RULES}

Output ONLY: {{"format": "{fmt_choice}", "hook": "the main tweet", "reveal": "the self-reply (empty string if standalone)"}}"""

    try:
        raw = call_llm(prompt, timeout=180, json_mode=True, temperature=0.9)
        if not raw:
            return None

        payload = _parse_llm_json(raw)
        if not isinstance(payload, dict):
            return None

        # Override format in case LLM changed it
        payload.setdefault("format", fmt_choice)
        return _build_thread_entry(payload)

    except Exception as e:
        print(f"Single draft failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def llm_rank_candidates(candidates: list[dict], recent_posts: list[str] | None = None) -> list[dict]:
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

        payload = _parse_llm_json(raw)
        rankings = None
        if isinstance(payload, dict):
            rankings = payload.get("rankings")
        elif isinstance(payload, list):
            rankings = payload
        if not isinstance(rankings, list):
            print("Could not parse LLM ranking response, using original order", file=sys.stderr)
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
            print("Incomplete LLM ranking response, using original order", file=sys.stderr)
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
    parser.add_argument("--dry-run", action="store_true", help="Preview generated posts without posting")
    parser.add_argument("--confirm", action="store_true", default=None,
                        help="Prompt for confirmation before posting (default: auto-detect TTY)")
    parser.add_argument("--no-confirm", action="store_true", help="Skip confirmation even in interactive mode")
    parser.add_argument("--top-tweets-hours", type=int, default=168, help="How far back to look for top tweets (default: 168 = 7 days)")
    parser.add_argument("--top-tweets-terms", type=int, default=4, help="Number of search terms to sample for top tweets (default: 4)")
    return parser


def _print_dry_run(top_tweets, ranked, hook_text, reveal_text, fmt):
    """Display dry-run results."""
    print("\n=== DRY-RUN RESULTS ===", flush=True)
    if top_tweets:
        print(f"\n--- Top tweets used ({len(top_tweets)}) ---", flush=True)
        for t in top_tweets[:10]:
            text = (t.get("text") or "").replace("\n", " ")
            print(f"  [{t.get('likes', 0)}L {t.get('retweets', 0)}RT] @{t.get('author', '?')}: {text}", flush=True)
    print(f"\n--- All ranked candidates ({len(ranked)}) ---", flush=True)
    for i, c in enumerate(ranked):
        hook = (c.get("hook") or "").replace("\n", " ")
        reveal = (c.get("reveal") or "").replace("\n", " ")
        print(f"  #{i+1} (score={c.get('llm_score', '?')}, {c.get('format', '?')}):", flush=True)
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

    # Fetch top tweets (cache-first; CDP only on cache miss)
    top_tweets: list[dict] = []
    try:
        top_tweets = fetch_top_tweets(
            n_terms=args.top_tweets_terms,
            since_hours=args.top_tweets_hours,
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

            # Generate fresh candidates every run
            print("Drafting batch...", flush=True)
            new_entries = draft_batch(conn, top_tweets=top_tweets)
            if not new_entries:
                print("Batch failed, falling back to single draft...", flush=True)
                single = draft_single(conn, top_tweets=top_tweets)
                new_entries = [single] if single else []

            unposted = _filter_valid_unposted(
                [{"text": e.get("text", ""), "thread_data": e.get("thread_data")} for e in new_entries]
            )
            print(f"Drafted {len(new_entries)}, {len(unposted)} valid", flush=True)

            if not unposted:
                send_error_alert("All drafting failed or produced no valid entries")
                print("No valid tweets drafted")
                return 1

            # Rank candidates (pass recent posts so ranker can reject duplicates)
            recent_posts = [p.get("text", "")[:200] for p in get_recent_posts(conn, days=14, limit=12)]
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
            print(f"Selected ({fmt}, score={best.get('llm_score', '?')}): {hook_text[:100]}...", flush=True)

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
                print(f"Humanize error: {e}, proceeding with original draft", flush=True)

            # Append thread indicator when there's a reveal coming
            if reveal_text and not hook_text.rstrip().endswith("👇"):
                hook_text = hook_text.rstrip() + " 👇"

            # Interactive confirmation
            interactive = (args.confirm or (args.confirm is None and not args.no_confirm
                           and sys.stdin.isatty()))
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
                send_error_alert(f"Failed to post hook via CDP\n\nHook was:\n{hook_text}")
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
                    print(f"Reveal posted successfully (reply_id={reply_id})", flush=True)
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
