#!/usr/bin/env python3
"""Autonomous Twitter engagement for @DecentCloud_org.

Strategy: /projects/Notes/Pickle/Twitter/decent-cloud-twitter-plan.md

Consolidated engagement script - the single source of truth for Twitter engagement.
Uses direct LLM calls for analysis and drafting, browser automation for posting.

Flow:
1. Search for relevant candidates
2. For each candidate:
   - Fetch full tweet + thread context
   - LLM analyzes and drafts response
   - Post immediately (if LLM approves)
3. Log everything to DB

Environment variables:
- OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL (for LLM)
"""

from __future__ import annotations

import json
import random
import re
import sys
import time as _time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/projects/automations/twitter")
sys.path.insert(0, "/projects/automations")  # needed for lib.llm_utils
from lib.llm_utils import call_llm_simple as call_llm, extract_json
from db import (
    get_conn,
    get_engaged_tweet_ids,
    get_queued_candidates,
    get_recent_engagements,
    get_recent_posts,
    get_search_term_stats,
    insert_engagement,
    is_engaged,
    mark_queue_processed,
    upsert_search_term_stats,
)
from twitter_utils import (
    BLOCKED_AUTHORS,
    SEARCH_TERMS,
    auto_follow_after_engagement,
    weighted_sample_terms,
    check_follows_back,
    fetch_tweet_context,
    follow_user,
    get_latest_own_tweet_id,
    get_user_profile,
    humanize,
    is_junk,
    jitter_sleep,
    load_project_context,
    lookup_our_thread,
    post_reply,
    save_encountered_thread,
    send_error_alert,
    unfollow_user,
    utc_now,
)

LLM_CACHE_TTL_DAYS = 7

PLATFORM_TERMS = [
    "cloud",
    "aws",
    "gcp",
    "azure",
    "k8s",
    "kubernetes",
    "serverless",
    "firebase",
    "vercel",
    "netlify",
    "cloudflare",
]

PROBLEM_TERMS = [
    "expensive",
    "costs",
    "pricing",
    "bill",
    "fees",
    "bill shock",
    "pricing complaint",
    "costs rising",
    "too expensive",
    "pricing high",
    "costs too high",
]


def get_cached_decision(decision_cache: dict, tweet_id: str) -> dict | None:
    entry = decision_cache.get(tweet_id)
    if not entry:
        return None
    try:
        cached_at = datetime.fromisoformat(entry["cachedAt"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - cached_at > timedelta(days=LLM_CACHE_TTL_DAYS):
            return None
    except (KeyError, ValueError):
        return None
    return entry.get("decision")


def cache_decision(decision_cache: dict, tweet_id: str, decision: dict | None) -> None:
    decision_cache[tweet_id] = {"cachedAt": utc_now(), "decision": decision}


def prune_decision_cache(decision_cache: dict) -> int:
    if not decision_cache:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=LLM_CACHE_TTL_DAYS)
    expired = []
    for tid, entry in decision_cache.items():
        try:
            cached_at = datetime.fromisoformat(entry["cachedAt"].replace("Z", "+00:00"))
            if cached_at < cutoff:
                expired.append(tid)
        except (KeyError, ValueError):
            expired.append(tid)
    for tid in expired:
        del decision_cache[tid]
    return len(expired)


def _build_voice_context(
    recent_engagements: list[dict], recent_posts: list[dict]
) -> tuple[str, str]:
    """Build voice consistency strings from DB records."""
    recent_our_replies = ""
    recent_our_posts = ""

    if recent_engagements:
        lines = [
            f'  - @{r.get("target_username", "?")} <- "{(r.get("our_reply_text") or "")[:120]}"'
            for r in recent_engagements
        ]
        recent_our_replies = "\n".join(lines)

    posts = [
        p
        for p in recent_posts
        if p.get("type") in ("post", "value-drop", "dev-update", "thread")
    ]
    if posts:
        lines = [f'  - "{(p.get("text") or "")[:120]}"' for p in posts]
        recent_our_posts = "\n".join(lines)

    return recent_our_replies, recent_our_posts


def draft_reply_with_full_context(
    candidate: dict,
    tweet_context: dict,
    recent_engagements: list[dict],
    recent_posts: list[dict],
) -> dict | None:
    recent_our_replies, recent_our_posts = _build_voice_context(
        recent_engagements, recent_posts
    )

    # Build full conversation context sections
    parent_chain_text = "None (original tweet, not a reply)"
    if tweet_context.get("parentChain"):
        parts = []
        for p in tweet_context["parentChain"]:
            parts.append(f'@{p.get("username", "?")} said: "{p.get("text", "")}"')
        parent_chain_text = " -> ".join(parts)
    elif tweet_context.get("replyTo"):
        rt = tweet_context["replyTo"]
        parent_chain_text = f'@{rt.get("username", "?")} said: "{rt.get("text", "")}"'

    other_replies_text = "None visible yet"
    if tweet_context.get("otherReplies"):
        lines = [
            f'  - @{r.get("username", "?")}: "{r.get("text", "")}"'
            for r in tweet_context["otherReplies"]
        ]
        other_replies_text = "\n".join(lines)

    # Check if this tweet touches one of our own posted threads
    all_visible_ids = [tweet_context.get("tweetId", "")]
    for p in tweet_context.get("parentChain") or []:
        if p.get("tweetId"):
            all_visible_ids.append(p["tweetId"])
    for t in tweet_context.get("threadContinuation") or []:
        if t.get("id"):
            all_visible_ids.append(t["id"])
    our_thread_note = lookup_our_thread([i for i in all_visible_ids if i])

    project_context = load_project_context()

    # Build author profile section
    author_profile_text = "No profile data available"
    profile = tweet_context.get("authorProfile")
    if profile:
        profile_lines = []
        if profile.get("bio"):
            profile_lines.append(f"**Bio:** {profile['bio']}")
        if profile.get("location"):
            profile_lines.append(f"**Location:** {profile['location']}")
        if profile.get("followersCount"):
            profile_lines.append(f"**Followers:** {profile['followersCount']:,}")
        if profile.get("recentTweets"):
            profile_lines.append("**Recent tweets (their interests):**")
            for i, t in enumerate(profile["recentTweets"][:5], 1):
                profile_lines.append(f"  {i}. {t[:100]}")
        author_profile_text = (
            "\n".join(profile_lines) if profile_lines else "No profile data"
        )

    prompt = f"""You are the voice of @DecentCloud_org on Twitter. Analyze this tweet and decide if/how to engage.

# Project & Strategy Context
{project_context}

# Tweet to Analyze
**Author:** @{tweet_context["author"]} ({tweet_context.get("authorName", "")})
**Tweet:** {tweet_context["text"]}
**Found via:** "{candidate.get("searchTerm", "N/A")}"
**Stats:** {tweet_context["stats"]["likes"]} likes, {tweet_context["stats"]["retweets"]} RTs, {tweet_context["stats"]["replies"]} replies

# Author Profile (determine if they're target audience)
{author_profile_text}

# Full Conversation Context (READ THIS CAREFULLY)

**Conversation ancestry (what this tweet is replying to):**
{parent_chain_text}

**Author's thread continuation (their own follow-up tweets):**
{json.dumps(tweet_context.get("threadContinuation"), indent=2) if tweet_context.get("threadContinuation") else "None (single tweet or no continuation yet)"}

**Other replies already in this thread (what others have said):**
{other_replies_text}

**Quoted tweet:**
{json.dumps(tweet_context.get("quotedTweet"), indent=2) if tweet_context.get("quotedTweet") else "None"}

**Our thread this tweet is part of (FULL NOTE — read every tweet before replying):**
{our_thread_note if our_thread_note else "Not part of one of our threads."}

# Our Recent Activity (for voice consistency — DO NOT repeat these angles)

**Our last 8 replies:**
{recent_our_replies or "  (none yet)"}

**Our recent original posts:**
{recent_our_posts or "  (none yet)"}

# Your Task
1. **Read the FULL conversation context above.** What is the author ACTUALLY saying?
   - Use parent chain + thread continuation to understand the full picture
   - Check other replies — don't duplicate what's already been said in the thread
   - Check our recent replies — don't repeat the same angle we already used recently
   - Check author profile — are they target audience? Do their interests align with Decent Cloud?
   - If it's spam/promo/crypto, SKIP IT
   - If it's vague/generic with no real substance, SKIP IT

2. **PRIORITIZE engagement opportunities (score 8-10 = must engage):**
   - Provider support horror stories ("cloud support terrible", "provider ghosted")
   - P2P marketplace trust complaints ("akash reliability", "decentralized compute issues")
   - "I tried [p2p service] and got ghosted/no response"
   - Questions about provider reliability/accountability
   - **Author is target audience** (DevOps, SRE, infra builder, self-hoster, p2p/marketplace builder)
   - **Author likely to engage back** (active account, replies to others, not a content farm)

   **GOOD (score 6-7):**
   - Cloud cost complaints (still relevant but not primary)
   - Infrastructure philosophy debates
   - GPU availability/demand
   - Author tangentially related to target audience

   **SKIP (score <5):**
   - Generic AWS dunking without specific pain
   - Crypto price speculation
   - Already overcrowded threads (>50 replies)
   - Author is content farm, bot, or unlikely to engage
   - Author's interests completely unrelated to cloud/infra/marketplace
   - Cynical rant/observation that everyone already agrees with — our only move is pure validation ("so true!"), author won't reply back
   - No specific hook: our reply would be generic and forgettable

3. **Rate conversation likelihood (1-10):**
   Ask yourself: if we reply, will the author actually write back to US specifically?

   **High (8-10):** Author asked a question (explicit or implicit); made a debatable claim we can push back on; expressed specific unresolved pain; wants an answer
   **Medium (5-7):** Author made an observation we can sharpen or extend with a non-obvious take; might engage if our reply is interesting
   **Low (1-4):** Author is broadcasting/venting, not looking for discussion; our reply would purely validate ("yeah support sucks") with nothing new; author seems done with the topic

   **The test:** "Would the author see our reply and think 'oh, interesting — I want to respond to that'?" If the honest answer is "they'd just like it and scroll on", that's a low score.

4. **If YES, draft a reply that gets engagement (likes, follows):**

   **Voice:**
   - 1 sentence. Borderline rude cynicism. Sounds like someone who's seen this a hundred times and is mildly annoyed you're only noticing now.
   - No setup, no punchline structure — just the plain thing, stated plainly
   - Lowercase preferred. No trailing punctuation if it feels unnatural.
   - The fact itself carries the cynicism — don't editorialize on top of it
   - NEVER use template anchors: "wild that", "funny how", "almost like", "turns out", "weird that" — these read as AI tells
   - Do NOT construct a clever observation. Just say the thing a tired infra engineer would fire off in 5 seconds.

   **What gets likes/follows:**
   - Dropping a specific fact that makes their situation sound worse than they described
   - Pointing out who actually holds the bag (hint: not the vendor)
   - Saying the quiet part out loud — no softening, no "to be fair"

   **What kills engagement:**
   - Generic validation ("so true", "yeah that's rough")
   - Clever sentence construction that takes effort to parse
   - Imperative framing ("make sure you...", "you should switch to...")
   - Sales pitches or product mentions — zero in Phase 1
   - Anchor phrases that signal AI: "wild that", "funny how", "almost like", "turns out"

   **Hard rules:**
   - 1-2 sentences max, under 280 chars
   - NO questions unless they asked one first
   - NO hashtags, NO links
   - NO "check us out", NO "follow for updates"
   - profileClickWorthy check: only reply if you're adding a specific fact or a cutting observation. Pure validation ("so true", "exactly", "been there", "this is real") fails this check regardless of conversation likelihood.

5. **If NO (not worth engaging), explain why**

# Example Replies (Study the Voice — Blunt, Cynical, 1-2 Sentences)

Tweet: "Tried Akash for GPU compute, provider just stopped responding mid-job"
-> "anonymous providers have zero skin in the game, which is fine until your job disappears mid-run"

Tweet: "Cloud support is terrible, been waiting 3 days for a response"
-> "nobody at AWS is losing sleep over your ticket"

Tweet: "Egress fees are such a scam"
-> "$0 in. $90/TB out. totally fine."

Tweet: "Accountability cannot be delegated. When a cloud provider manages your data, the liability stays with you."
-> "they take the contract, you take the fine"

Tweet: "Cloud egress fees are notoriously confusing"
-> "in: $0. out: $90/TB. not that confusing actually"

# Output Format (JSON)
{{
  "shouldEngage": true/false,
  "conversationLikelihood": 1-10,
  "profileClickWorthy": true/false,
  "reasoning": "brief explanation of decision",
  "reply": "draft reply text here" (or null if shouldEngage is false)
}}

Output ONLY valid JSON, nothing else.
"""

    try:
        response = call_llm(prompt, timeout=120)
        if not response:
            print("  LLM returned nothing", flush=True)
            return None

        json_str = extract_json(response)
        if json_str is None:
            print("  Could not extract JSON from LLM response", flush=True)
            response2 = call_llm(prompt, timeout=120)
            if response2:
                json_str = extract_json(response2)
            if json_str is None:
                return None

        decision = json.loads(json_str)

        conv_score = decision.get("conversationLikelihood", 5)
        profile_click_worthy = decision.get("profileClickWorthy", False)
        if not decision.get("shouldEngage") or conv_score < 6:
            reason = decision.get("reasoning", "no reason")
            if conv_score < 6:
                print(f"  Low conversation likelihood ({conv_score}/10): {reason}")
            else:
                print(f"  LLM decided NOT to engage: {reason}")
            decision["shouldEngage"] = False
            return decision

        if not profile_click_worthy:
            reason = decision.get("reasoning", "no reason")
            print(f"  Not profile-click-worthy (pure validation), skipping")
            decision["shouldEngage"] = False
            return decision

        reply = decision.get("reply")
        if not reply or len(reply) > 280:
            print("  LLM reply invalid (empty or too long)")
            return None

        return decision

    except Exception as e:
        print(f"  Exception in LLM analysis: {e}")
        return None


FOLLOW_CHURN_DAYS_MIN = 7
FOLLOW_CHURN_DAYS_MAX = 14
MAX_FOLLOWS_PER_RUN = 8  # match engagement cap — follow each account we reply to


def generate_dynamic_keywords(
    recent_engagements: list[dict], max_terms: int = 15
) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    recent = []

    for eng in recent_engagements:
        ts = eng.get("replied_at")
        if not ts:
            continue
        try:
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts > cutoff:
                recent.append(eng)
        except Exception:
            continue

    if not recent:
        print("  No recent engagements to analyze, using sensible defaults", flush=True)
        return [
            "cloud expensive",
            "aws pricing",
            "kubernetes costs",
            "serverless pricing",
            "cloud bill shock",
            "gpu expensive",
        ]

    keywords = set()
    all_words = []

    for eng in recent:
        search_term = eng.get("search_term", "")
        if search_term:
            search_term = search_term.lower().strip()
            keywords.add(search_term)
            words = re.findall(r"\b\w+\b", search_term)
            all_words.extend(words)

    new_terms = set()

    for eng in recent[:10]:
        term = (eng.get("search_term") or "").lower()
        if term and len(term.split()) > 1:
            new_terms.add(term)

    relevant_platforms = [
        p for p in PLATFORM_TERMS if p in keywords or any(p in k for k in keywords)
    ]
    relevant_problems = [
        p for p in PROBLEM_TERMS if any(word in p or p in keywords for word in keywords)
    ]

    if not relevant_platforms:
        relevant_platforms = ["cloud", "aws", "kubernetes", "serverless"]
    if not relevant_problems:
        relevant_problems = ["expensive", "costs", "pricing"]

    for platform in relevant_platforms[:4]:
        for problem in relevant_problems[:3]:
            new_terms.add(f"{platform} {problem}")
            if len(new_terms) >= max_terms * 2:
                break

    return list(new_terms)[:max_terms]


def main() -> int:
    print("=== AUTONOMOUS TWITTER ENGAGEMENT ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    # In-process LLM decision cache (not persisted to DB — lives only for this run)
    decision_cache: dict = {}

    try:
        with get_conn() as conn:
            # Load DB state
            engaged_ids = get_engaged_tweet_ids(conn)
            term_stats = get_search_term_stats(conn)

            # For voice consistency context
            recent_engagements = get_recent_engagements(conn, hours=168, limit=8)
            recent_posts = get_recent_posts(conn, limit=5)

            # Prefer the pre-fetched queue (filled hourly by search_queue.py via CDP).
            # Fall back to live CDP search only if the queue is empty.
            candidates = get_queued_candidates(conn, limit=100)
            using_queue = bool(candidates)

            if using_queue:
                print(
                    f"Using {len(candidates)} candidates from queue",
                    flush=True,
                )
            else:
                print("Queue empty — falling back to CDP search...", flush=True)
                from twitter_utils import search_candidates

                candidates = search_candidates(term_stats=term_stats)
                print(f"Found {len(candidates)} raw candidates from search", flush=True)

                if not candidates:
                    print("No candidates from search", flush=True)
                    print(
                        "  Generating fresh keywords from recent successful engagements...",
                        flush=True,
                    )

                    new_keywords = generate_dynamic_keywords(
                        recent_engagements, max_terms=15
                    )
                    print(f"  Generated {len(new_keywords)} new keywords", flush=True)

                    global SEARCH_TERMS
                    SEARCH_TERMS[:] = new_keywords

                    print("  Retrying search with fresh keywords...", flush=True)
                    candidates = search_candidates(terms=new_keywords)
                    print(
                        f"  Found {len(candidates)} candidates with new keywords",
                        flush=True,
                    )

                    if not candidates:
                        print(
                            "  Still no candidates - might just be quiet period",
                            flush=True,
                        )
                        return 0

            # Shuffle before filtering so all search terms get equal representation
            random.shuffle(candidates)

            selected = []
            for c in candidates:
                tid = c.get("tweetId")
                text = c.get("text", "")

                if not tid or str(tid) in engaged_ids:
                    continue
                if is_junk(text):
                    continue

                author = (c.get("author") or "").lower()
                if author in {a.lower() for a in BLOCKED_AUTHORS}:
                    print(f"  Skipping blocked author @{author}", flush=True)
                    continue

                selected.append(c)
                if len(selected) >= 50:
                    break

            if not selected:
                print("No suitable candidates after filtering")
                return 0

            print(f"Selected {len(selected)} for LLM analysis")

            # Track candidates per term for hit-rate stats (denominator)
            term_candidate_counts: Counter = Counter(
                c.get("searchTerm", "") for c in selected if c.get("searchTerm")
            )
            term_engaged_counts: Counter = Counter()

            results = []
            engaged_count = 0

            # ── Phase A: fetch context sequentially (browser/CDP ops) ──────────
            # Limit prefetch batch so we don't over-fetch when the cap is low.
            FETCH_BATCH = min(len(selected), 15)
            to_analyze: list[tuple[dict, dict]] = []

            for candidate in selected[:FETCH_BATCH]:
                tweet_id = candidate["tweetId"]
                author = candidate.get("author") or "unknown"

                # Skip if already engaged
                if is_engaged(conn, str(tweet_id)):
                    print(f"  Already engaged {tweet_id} in DB — skipping", flush=True)
                    if using_queue:
                        mark_queue_processed(conn, [str(tweet_id)])
                    continue

                print(f"\nFetching context for {tweet_id} (@{author})...")
                tweet_context = fetch_tweet_context(tweet_id)
                if not tweet_context:
                    print(f"  Skipping {tweet_id} - failed to fetch context", flush=True)
                    continue

                # Mark processed after successful context fetch so a browser failure
                # doesn't permanently consume the candidate.
                if using_queue:
                    mark_queue_processed(conn, [str(tweet_id)])

                # Fetch author profile for context (bio, recent tweets, interests)
                author_name = tweet_context.get("author", "")
                if author_name:
                    author_profile = get_user_profile(author_name)
                    if author_profile:
                        tweet_context["authorProfile"] = author_profile

                to_analyze.append((candidate, tweet_context))

            # ── Phase B: parallel LLM analysis ───────────────────────────────
            print(f"\nRunning LLM analysis on {len(to_analyze)} candidates in parallel...")

            def _analyze_one(args: tuple[dict, dict]) -> tuple[dict, dict, dict | None]:
                cand, ctx = args
                tid = cand["tweetId"]
                cached = get_cached_decision(decision_cache, tid)
                if cached is not None:
                    print(f"  [{tid}] Using cached LLM decision", flush=True)
                    return cand, ctx, cached
                print(f"  [{tid}] Asking LLM to analyze...", flush=True)
                dec = draft_reply_with_full_context(
                    cand, ctx, recent_engagements, recent_posts
                )
                if dec is not None:
                    cache_decision(decision_cache, tid, dec)
                return cand, ctx, dec

            max_workers = min(len(to_analyze), 4)
            analyzed: list[tuple[dict, dict, dict | None]] = []
            if to_analyze:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(_analyze_one, item): item for item in to_analyze
                    }
                    # Preserve submission order for deterministic posting
                    ordered = {item: None for item in to_analyze}
                    for future in as_completed(futures):
                        item = futures[future]
                        ordered[item] = future.result()
                    analyzed = list(ordered.values())

            # ── Phase C: sequential posting ───────────────────────────────────
            for candidate, tweet_context, decision in analyzed:
                if engaged_count >= 8:
                    break

                tweet_id = candidate["tweetId"]
                url = candidate["url"]
                author = candidate.get("author") or "unknown"
                search_term = candidate.get("searchTerm", "")

                print(f"\nProcessing {tweet_id} (@{author})...")

                # Save encountered thread regardless of decision
                if decision:
                    save_encountered_thread(
                        tweet_context, decision, tweet_id, search_term
                    )

                if not decision or not decision.get("shouldEngage"):
                    if decision:
                        print(
                            f"  LLM skipped: {decision.get('reasoning', 'no reason')}"
                        )
                    continue

                reply_text = decision["reply"]
                print(f"  LLM approved: {reply_text[:80]}...")
                print(f"  Reasoning: {decision.get('reasoning', 'N/A')}")

                try:
                    reply_text = humanize(reply_text)
                except Exception as e:
                    print(f"  Humanize failed: {e}")
                    continue

                jitter_sleep()

                # Snapshot our latest tweet ID *before* posting so we can verify
                # a genuinely new tweet appeared (false positives from CDP are possible).
                pre_post_id = get_latest_own_tweet_id("DecentCloud_org")

                if not post_reply(tweet_id, reply_text):
                    send_error_alert(f"Failed to post reply to {tweet_id} (@{author})")
                    continue

                # Verify the reply actually appeared by confirming a higher tweet ID.
                # Twitter Snowflake IDs are monotonically increasing, so a new reply
                # must have a strictly larger ID than anything posted before it.
                _time.sleep(4)
                our_reply_id = get_latest_own_tweet_id("DecentCloud_org")
                if our_reply_id and pre_post_id and int(our_reply_id) <= int(pre_post_id):
                    print(
                        f"  WARNING: reply ID {our_reply_id} is not newer than pre-post ID "
                        f"{pre_post_id} — tweet may not have been posted!",
                        flush=True,
                    )
                    send_error_alert(
                        f"post_reply false positive for {tweet_id} (@{author}): "
                        f"no new tweet ID observed (pre={pre_post_id}, post={our_reply_id})"
                    )
                    continue

                print("  Replied", flush=True)
                if our_reply_id:
                    print(f"  Captured ourReplyId: {our_reply_id}", flush=True)

                # Auto-follow the author after confirmed reply
                auto_follow_after_engagement(conn, author, tweet_id)

                # Insert engagement into DB
                stats = tweet_context.get("stats", {})
                insert_engagement(
                    conn,
                    tweet_id=str(tweet_id),
                    target_username=author,
                    our_reply_text=reply_text,
                    our_reply_id=our_reply_id,
                    source="search",
                    search_term=search_term,
                    conv_likelihood=decision.get("conversationLikelihood"),
                    profile_click_worthy=decision.get("profileClickWorthy"),
                    llm_reasoning=decision.get("reasoning"),
                    target_tweet_text=tweet_context.get("text"),
                    tweet_url=url,
                    tweet_likes=stats.get("likes"),
                    tweet_rts=stats.get("retweets"),
                    tweet_replies=stats.get("replies"),
                )

                engaged_ids.add(str(tweet_id))
                engaged_count += 1
                if search_term:
                    term_engaged_counts[search_term] += 1
                results.append(f"{tweet_id} | @{author}")

                # Update voice context in-memory so subsequent runs in this session
                # see our latest replies (posting is sequential so no race).
                recent_engagements = [
                    {
                        "target_username": author,
                        "our_reply_text": reply_text,
                        "replied_at": utc_now(),
                        "search_term": search_term,
                    }
                ] + recent_engagements[:7]

            # Accumulate per-term hit-rate stats
            for term, count in term_candidate_counts.items():
                upsert_search_term_stats(
                    conn,
                    term,
                    candidates_delta=count,
                    engaged_delta=term_engaged_counts.get(term, 0),
                )

        if results:
            print(f"\nCompleted {len(results)} engagements")
            for r in results:
                print(f"  {r}")
        else:
            print("\nNo engagements executed (LLM filtered all)")

        return 0

    except Exception as e:
        send_error_alert(f"Autonomous engagement failed: {e}")
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
