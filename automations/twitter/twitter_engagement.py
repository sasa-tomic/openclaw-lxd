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
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/projects/automations")
from prefect.concurrency.sync import concurrency
from lib.llm_utils import call_llm_simple as call_llm, extract_json
from engagement_triage import filter_candidates_for_engagement, llm_triage_candidates, reach_score
from db import (
    ensure_schema,
    get_conn,
    get_engaged_tweet_ids,
    get_engagements_with_user,
    get_our_thread_context,
    get_queued_candidates,
    get_recent_engagements,
    get_recent_posts,
    get_search_term_stats,
    get_top_reply_combos,
    get_bottom_reply_combos,
    insert_engagement,
    is_engaged,
    mark_queue_processed,
    upsert_search_term_stats,
    upsert_tweet_replies,
    TWITTER_ACCOUNT_USERNAME,
)
from twitter_utils import (
    BLOCKED_AUTHORS,
    auto_follow_after_engagement,
    fetch_tweet_context,
    get_user_profile,
    humanize,
    is_english_text,
    jitter_sleep,
    like_tweet,
    load_project_context,
    post_quote_with_retries,
    post_reply_with_retries,
    send_error_alert,
    utc_now,
)

LLM_CACHE_TTL_DAYS = 7


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
    top_combos: list[dict] | None = None,
    bottom_combos: list[dict] | None = None,
) -> dict | None:
    recent_our_replies, recent_our_posts = _build_voice_context(
        recent_engagements, recent_posts
    )

    # Detect whether we're already part of this thread (pre-compute for the LLM).
    our_handle_lower = "decentcloud_org"
    parent_chain = tweet_context.get("parentChain") or []
    other_replies = tweet_context.get("otherReplies") or []
    already_in_thread = (
        any((p.get("username") or "").lower() == our_handle_lower for p in parent_chain)
        or any((r.get("username") or "").lower() == our_handle_lower for r in other_replies)
    )
    immediate_parent = parent_chain[-1] if parent_chain else None
    replying_to_self = (
        immediate_parent is not None
        and (immediate_parent.get("username") or "").lower() == our_handle_lower
    )

    # Build full conversation context sections — oldest (root) first for clarity
    parent_chain_text = "None (original tweet, not a reply)"
    if parent_chain:
        parts = []
        for i, p in enumerate(parent_chain, 1):
            parts.append(f'  [{i}] @{p.get("username", "?")} said: "{p.get("text", "")}"')
        parent_chain_text = "\n".join(parts)
    elif tweet_context.get("replyTo"):
        rt = tweet_context["replyTo"]
        parent_chain_text = f'  [1] @{rt.get("username", "?")} said: "{rt.get("text", "")}"'

    other_replies_text = "None visible yet"
    if other_replies:
        lines = [
            f'  - @{r.get("username", "?")}: "{r.get("text", "")}"'
            for r in other_replies
        ]
        other_replies_text = "\n".join(lines)

    # Thread context pre-fetched in Phase A and stored on tweet_context
    our_thread_note = tweet_context.get("ourThreadContext")

    # Prior exchanges with this author (fetched from DB in Phase A)
    prior_exchanges_text = "None on record."
    prior_exchanges = tweet_context.get("priorExchanges") or []
    if prior_exchanges:
        lines = []
        for ex in prior_exchanges:
            their_tweet = (ex.get("target_tweet_text") or "")[:120]
            our_reply = (ex.get("our_reply_text") or "")[:120]
            got_reply = ex.get("got_reply_back", False)
            lines.append(
                f'  - They said: "{their_tweet}" | We replied: "{our_reply}"'
                + (" | They replied back ✓" if got_reply else "")
            )
        prior_exchanges_text = "\n".join(lines)

    project_context = load_project_context()

    # Build few-shot style anchors from real engagement data
    combos_section = ""
    if top_combos and len(top_combos) >= 3:
        top_lines = []
        for c in top_combos:
            tweet_snippet = (c.get("target_tweet_text") or "")[:120].replace("\n", " ")
            reply_snippet = (c.get("our_reply_text") or "")[:200].replace("\n", " ")
            likes = c.get("reply_likes", 0)
            rts = c.get("reply_rts", 0) or 0
            back = " + reply back" if c.get("got_reply_back") else ""
            top_lines.append(f'  [{likes}L {rts}RT{back}] Tweet: "{tweet_snippet}"\n  → Our reply: "{reply_snippet}"')

        combos_section = (
            "\n\n## Reply style — copy what works, avoid what doesn't\n\n"
            "**These got real engagement — match this style and voice (don't copy verbatim):**\n\n"
            + "\n\n".join(top_lines)
        )

        if bottom_combos:
            bottom_lines = []
            for c in bottom_combos:
                tweet_snippet = (c.get("target_tweet_text") or "")[:120].replace("\n", " ")
                reply_snippet = (c.get("our_reply_text") or "")[:200].replace("\n", " ")
                bottom_lines.append(f'  Tweet: "{tweet_snippet}"\n  → Our reply (0 engagement): "{reply_snippet}"')
            combos_section += (
                "\n\n**These got zero engagement — avoid this style:**\n\n"
                + "\n\n".join(bottom_lines)
            )

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
**Estimated reply visibility:** ~{tweet_context["stats"]["likes"] * 20 + tweet_context["stats"]["retweets"] * 50:,} impressions (higher = our reply gets more eyeballs)
**Have we already participated in this thread?** {"YES" if already_in_thread else "NO — this would be our first reply in this thread"}
**Is the immediate parent tweet from us (replying to ourselves)?** {"YES" if replying_to_self else "NO"}

# Author Profile (determine if they're target audience)
{author_profile_text}

# Full Conversation Context (read top-to-bottom — this is the full thread so far)

**Conversation ancestry — oldest first, leading up to the tweet above:**
{parent_chain_text}

**Other replies already in this thread (what others have said):**
{other_replies_text}

**Author's own follow-up tweets:**
{json.dumps(tweet_context.get("threadContinuation"), indent=2) if tweet_context.get("threadContinuation") else "None (single tweet or no continuation yet)"}

**Quoted tweet:**
{json.dumps(tweet_context.get("quotedTweet"), indent=2) if tweet_context.get("quotedTweet") else "None"}

**Our thread this tweet is part of (our own cached context):**
{our_thread_note if our_thread_note else "Not part of one of our threads."}

**Our prior exchanges with @{tweet_context["author"]} (full DB history — not just today):**
{prior_exchanges_text}

# Our Recent Activity (voice consistency — DO NOT repeat these angles)
**Our last 8 replies:**
{recent_our_replies or "  (none yet)"}

**Our recent original posts:**
{recent_our_posts or "  (none yet)"}{combos_section}

# COHERENCE GATE — Apply this FIRST, before everything else

Read the full thread above. Then answer internally:

1. **What is this conversation actually about?** State the topic in one sentence. If the topic is off our domain, don't skip yet — move to step 2 and check if the format/style is usable.
2. **What is the register?** Technical debate, casual banter, venting, sarcasm, humor, corporate speak, irony? You must match the register of the tweet — this is the key input for Style Mirror.
3. **What is the author SPECIFICALLY saying?** Reply to their exact point, not to your interpretation of the broader theme.
4. **Have we talked to this person before?** If so, did the prior exchange go badly (they pushed back, corrected us, seemed frustrated)? If yes, apply maximum scrutiny.
5. **Check other replies in the thread.** Don't repeat a point already made by someone else.

**Mode selection (hard rule — not a judgment call):**

- **"Have we already participated in this thread?" = YES → Mode B ONLY.**
  We are mid-conversation. Mode A and Style Mirror are not available.

- **"Have we already participated in this thread?" = NO → Mode A, Style Mirror, or Mode B.**
  Try them in this order: (1) direct Mode A, (2) Style Mirror, (3) Mode B. Use whichever gives the sharpest result.

**Mode A — Direct take:** A specific fact, honest observation, or genuinely funny/cynical line that addresses what the author actually said. Must be a direct response, not a tangent on the general topic.

**Mode A (Style Mirror) — Last resort before skipping:** When no direct content hook exists, use the tweet's *form* instead. Identify its rhythm, rhetorical move, or format — and deliver a real cloud/infrastructure truth in exactly that style. The reader gets the parallel because you used their own register. Style is the hook; a specific, true fact is the payload.
- Sarcastic one-liner → sarcastic one-liner about cloud pricing
- List of clichés → infrastructure clichés in the same format
- Mock-serious corporate speak → mock-serious corporate speak about egress or lock-in
- Cynical observation about human nature → same cynicism redirected at cloud vendors
- Ironic/deadpan → match the deadpan exactly, swap in the cloud truth
Do not use in mid-thread (Mode B only there). The fact must be real and specific — vague observations don't land.

**Mode B — Coherent continuation:** Your reply clearly follows from and advances this specific conversation. A natural next step — not a tangent, not a lecture dropped into casual banter.

**Replying to ourselves:** "Is the immediate parent tweet from us?" = YES — only engage if directly extending what we already said.

**DEFAULT TO ENGAGING.** The goal is views and follows, not topic purity. Before marking shouldEngage: false, ask: "Can I use this tweet's format to land a specific cloud truth in the same voice?" If yes, use Style Mirror and engage. The only valid reasons to skip: genuinely nothing to say in any mode, tone-deaf to the thread, or repeating ourselves.

**Contrarian advantage:** Disagreeing with a popular take drives more engagement than agreeing. Name the flaw specifically — "X is wrong because Y" beats validation every time. Readers follow accounts that hold a real position.

# Engagement Scoring
**PRIORITIZE (score 8-10):**
- You can land a witty, funny, or cynical punchline that makes readers want to follow us — this is the highest priority signal
- Provider horror stories, marketplace trust pain, matching problem, manual work that should be automated
- Style Mirror opportunity: viral or high-engagement format (sarcasm, irony, listicle) that lets you drop a real cloud truth in the same voice — score this 7+ even if the underlying topic is off-domain
- Tweet has zero replies — being first pins us at the top
- AWS egress gotchas, GCP Spanner pricing, Azure Reserved Instance complexity — name the platform specifically

**GOOD (score 6-7):**
- Cloud cost complaints, infra philosophy debates, GPU availability
- Any thread where a sharp fact or honest take earns likes from readers
- Style Mirror on a moderately engaging format

**BORDERLINE (score 3–5): default to Mode L (like only).**
If the tweet is on-topic or the author has followers, never set shouldEngage: false — use like instead. A like is always better than a skip for on-topic tweets: it registers us with the author and their audience at zero cost. Reserve shouldEngage: false strictly for the SKIP cases below.

**SKIP (score ≤2) — the ONLY valid reasons to set shouldEngage: false:**
- Author has ≤10 followers AND no Style Mirror angle AND topic is completely off-domain
- Our only move is pure validation with nothing specific to add AND the author is low-reach
- No Style Mirror opportunity exists AND topic has zero connection to our domain AND author is low-reach

**Audience reach test:** "Would someone reading this thread see our reply and think 'who is this — I should follow'?" Likes from readers matter far more than a reply from the author. High-follower accounts rarely reply back — their AUDIENCE is the prize.

**Mode L — Like only (lightweight signal, LAST STOP BEFORE SKIP):**
Before setting shouldEngage: false, ask: "Is this tweet on-topic or is the author someone we want to register with?" If yes → like instead of skip. Always.
A like registers us with the author and their audience at zero cost. Likes are strictly better than skips for any tweet that isn't completely irrelevant.
Set engagementType: "like" and reply: null. No audienceEngagementPotential requirement — likes are cheap.
Use this when: tweet is on-topic, author is target audience, but you'd be forcing a reply.

**Mode QT — Quote-tweet (high-reach only):**
Quote instead of reply ONLY when:
1. Thread already has 50+ replies — quote breaks out of the pile with its own reach
2. Your take directly contradicts the original author and making the disagreement visible matters
3. The original has 1K+ likes — their audience is bigger than the thread's

A quote needs at least 1 full sentence to stand on its own — it's shown to a wider audience independently. Keep it direct. Vary your openers using the same list as replies (see Reply Rules). Set engagementType: "quote".
Default to reply unless one of the above conditions applies.

# Reply Rules (when engaging)
- Length: 1 word to 2 sentences, under 280 chars. Short is often better — "Agreed.", "Hard disagree.", "Interesting angle." are valid replies when they're a genuine, direct response to what was said.
- The reply MUST directly answer or follow up on the original post or the latest message in the thread. React to what they actually said.
- Agreement, disagreement, or any other reaction is fine — what matters is that it's direct and specific.
- Follow-up: add one when it naturally extends the conversation — "And how would you handle the consistency problem?", "What's your take on the latency tradeoff?", "Does that still hold at scale?" — only when you have something specific to ask, not just to seem engaged.
- Vary your openers — check "Our last 8 replies" above and never reuse a word or phrase you opened with recently. Draw from: Agreed. / Fair point. / That tracks. / Exactly. / Disagree. / Hard disagree. / Not quite. / Depends. / Interesting angle. / Worth noting: / The tradeoff is… / The catch is… / Only partially. / Curious — / True, but…
- Write like a human expert tweets — direct, specific, no performance
- Standard sentence capitalization; no trailing punctuation if it feels unnatural
- The fact carries the weight — don't editorialize on top of it
- NEVER: "wild that", "funny how", "almost like", "turns out", "weird that" — AI tells
- NO hashtags, NO links, NO product mentions, NO "check us out"
- Name AWS, GCP, Azure, Stripe specifically when the point applies to them. "Cloud providers" is vague; "AWS" is interesting.
- profileClickWorthy: true if the reply adds a specific fact, non-obvious angle, or is genuinely funny/cynical/sharp enough that a reader would want to see who said it. Pure validation ("so true", "exactly", "been there") fails this check. When in doubt, mark true — we want to engage.

# Example Replies (voice reference — direct, specific, human)
Tweet: "Tried Akash for GPU compute, provider just stopped responding mid-job"
→ "Anonymous providers have zero skin in the game, which is fine until your job disappears mid-run"

Tweet: "Cloud support is terrible, been waiting 3 days for a response"
→ "Nobody at AWS is losing sleep over your ticket"

Tweet: "Egress fees are such a scam"
→ "$0 in. $90/TB out. Totally fine."

Tweet: "Accountability cannot be delegated. When a cloud provider manages your data, the liability stays with you."
→ "They take the contract, you take the fine"

Tweet: "Everyone needs Kubernetes to scale"
→ "Kubernetes trades operational simplicity for platform complexity. Most teams pay the tax and never need the scale."

Tweet: "Cloud is always cheaper than on-prem"
→ "Only if you don't count the engineering tax of cloud-native patterns. AWS margins are high for a reason."

# Style Mirror Examples (topic is off-domain but format is borrowed)
Tweet: "Move fast. Break things. Disrupt. Empower. Synergize."  ← mock-inspirational list
→ "Ingest free. Egress expensive. Scale fast. Pay quarterly. Regret annually."

Tweet: "Politicians say one thing, do another. Shocking."  ← deadpan sarcasm
→ "Cloud vendor publishes pricing page. Actual bill is different. Shocking."

Tweet: "We spend more time optimizing our morning routine than actually working"  ← ironic observation
→ "We spend more time optimizing Kubernetes configs than the app running on them"

Tweet: "Work smarter not harder. Think outside the box. Leverage synergies."  ← cliché list
→ "Lift and shift. Cloud-native rewrite. Hybrid approach. (All cost more than you modeled.)"

# Output Format (JSON)
{{
  "shouldEngage": true/false,
  "engagementType": "reply" or "like" or "quote" (default "reply" if omitted),
  "audienceEngagementPotential": 1-10,
  "profileClickWorthy": true/false,
  "mode": "A" or "B" (use "A" for both direct Mode A and Style Mirror; "B" for continuation; null if not engaging),
  "threadSummary": "one sentence: what is this conversation actually about",
  "reasoning": "brief explanation of decision",
  "reply": "draft reply text here" (or null if engagementType is "like" or shouldEngage is false)
}}

IMPORTANT: "audienceEngagementPotential" measures how many people will see and like our reply, NOT whether the author will reply. Large accounts score 8-9 because of audience reach even if they never reply directly.

Output ONLY valid JSON, nothing else.
"""

    try:
        response = call_llm(prompt, timeout=120, json_mode=True)
        if not response:
            print("  LLM returned nothing", flush=True)
            return None

        json_str = extract_json(response)
        if json_str is None:
            print("  Could not extract JSON from LLM response", flush=True)
            return None

        decision = json.loads(json_str)

        audience_score = decision.get("audienceEngagementPotential", 5)
        profile_click_worthy = decision.get("profileClickWorthy", False)
        if not decision.get("shouldEngage") or audience_score < 3:
            tweet_id = candidate.get("tweetId", "?")
            tweet_text = (tweet_context.get("text") or "")[:120]
            print(f"  LLM skipped [{tweet_id}]: {tweet_text!r}\n  Decision: {json.dumps(decision)}")
            decision["shouldEngage"] = False
            return decision

        engagement_type = decision.get("engagementType", "reply")

        reply = decision.get("reply")
        if engagement_type != "like" and (not reply or len(reply) > 280):
            print("  LLM reply invalid (empty or too long)")
            return None

        return decision

    except Exception as e:
        print(f"  Exception in LLM analysis: {e}")
        return None


def main() -> int:
    print("=== AUTONOMOUS TWITTER ENGAGEMENT ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    # In-process LLM decision cache (not persisted to DB — lives only for this run)
    decision_cache: dict = {}

    try:
        with get_conn() as conn:
            ensure_schema(conn)

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

                with concurrency("twitter-browser", occupy=1):
                    candidates = search_candidates(term_stats=term_stats)
                print(f"Found {len(candidates)} raw candidates from search", flush=True)

                if not candidates:
                    print("No candidates from search", flush=True)
                    print("  Quiet period likely — exiting.", flush=True)
                    return 0

            top_combos = get_top_reply_combos(conn, limit=20)
            bottom_combos = get_bottom_reply_combos(conn, limit=10)

            # ── Candidate filtering (hard rules only — no keyword content filter) ──
            eligible, discard_ids = filter_candidates_for_engagement(
                candidates,
                engaged_ids={str(i) for i in engaged_ids},
                blocked_authors=set(BLOCKED_AUTHORS),
            )

            if using_queue and discard_ids:
                mark_queue_processed(conn, discard_ids)

            if not eligible:
                print("No suitable candidates after filtering")
                return 0

            # ── Layer 1: reach-sort — highest-visibility tweets first ─────────
            eligible.sort(key=reach_score, reverse=True)

            # ── Layer 2: LLM batch triage — rank top 30, fetch context for all 30 ─
            TRIAGE_POOL = min(len(eligible), 30)
            triage_pool = eligible[:TRIAGE_POOL]

            print(
                f"Triaging {TRIAGE_POOL} reach-sorted candidates (of {len(eligible)} total)...",
                flush=True,
            )
            ranked_ids = llm_triage_candidates(triage_pool, top_n=TRIAGE_POOL)

            # Reconstruct in triage-ranked order; fallback preserves reach-sort order
            id_to_candidate = {str(c["tweetId"]): c for c in triage_pool}
            selected = [id_to_candidate[tid] for tid in ranked_ids if tid in id_to_candidate]
            # Append any the LLM omitted (shouldn't happen with top_n=TRIAGE_POOL)
            seen = {str(c["tweetId"]) for c in selected}
            for c in triage_pool:
                if str(c["tweetId"]) not in seen:
                    selected.append(c)

            print(f"Selected {len(selected)} candidates for context fetch")

            # Track candidates per term for hit-rate stats (denominator)
            term_candidate_counts: Counter = Counter(
                c.get("searchTerm", "") for c in selected if c.get("searchTerm")
            )
            term_engaged_counts: Counter = Counter()

            results = []
            engaged_count = 0

            # ── Phase A: fetch context sequentially (browser/CDP ops) ──────────
            to_analyze: list[tuple[dict, dict]] = []
            print(f"Fetching tweet context for {len(selected)} candidates...", flush=True)

            with concurrency("twitter-browser", occupy=1):
                for candidate in selected:
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
                        print(
                            f"  Skipping {tweet_id} - failed to fetch context", flush=True
                        )
                        continue
                    if not is_english_text(tweet_context.get("text")):
                        print(f"  Skipping {tweet_id} - non-English after context fetch", flush=True)
                        continue

                    # Check if this tweet is part of one of our own threads (DB lookup).
                    # Done here (conn in scope) so draft_reply_with_full_context can run
                    # safely in the thread pool without needing a DB connection.
                    visible_ids = [str(tweet_id)]
                    for p in tweet_context.get("parentChain") or []:
                        if p.get("tweetId"):
                            visible_ids.append(str(p["tweetId"]))
                    for t in tweet_context.get("threadContinuation") or []:
                        if t.get("id"):
                            visible_ids.append(str(t["id"]))
                    tweet_context["ourThreadContext"] = get_our_thread_context(conn, visible_ids)

                    # Persist high-engagement replies we observed while fetching context.
                    other_replies = tweet_context.get("otherReplies") or []
                    if other_replies:
                        saved = upsert_tweet_replies(conn, str(tweet_id), other_replies)
                        if saved:
                            print(f"  Persisted {saved} replies for {tweet_id}", flush=True)

                    # Mark processed after successful context fetch so a browser failure
                    # doesn't permanently consume the candidate.
                    if using_queue:
                        mark_queue_processed(conn, [str(tweet_id)])

                    # Fetch author profile for context (bio, recent tweets, interests)
                    author_name = tweet_context.get("author", "")

                    # Prior exchanges with this author (DB only — no API calls)
                    tweet_context["priorExchanges"] = get_engagements_with_user(conn, author_name or author)
                    if author_name:
                        author_profile = get_user_profile(author_name)
                        if author_profile:
                            tweet_context["authorProfile"] = author_profile

                    to_analyze.append((candidate, tweet_context))
            # ── Phase B runs with browser lock released (LLM-only, no CDP) ──────

            # ── Phase B: parallel LLM analysis ───────────────────────────────
            print(
                f"\nRunning LLM analysis on {len(to_analyze)} candidates in parallel..."
            )

            def _analyze_one(args: tuple[dict, dict]) -> tuple[dict, dict, dict | None]:
                cand, ctx = args
                tid = cand["tweetId"]
                cached = get_cached_decision(decision_cache, tid)
                if cached is not None:
                    print(f"  [{tid}] Using cached LLM decision", flush=True)
                    return cand, ctx, cached
                tweet_text = (ctx.get("text") or "")[:120]
                print(f"  [{tid}] Asking LLM to analyze: {tweet_text!r}", flush=True)
                dec = draft_reply_with_full_context(
                    cand, ctx, recent_engagements, recent_posts, top_combos, bottom_combos
                )
                if dec is not None:
                    cache_decision(decision_cache, tid, dec)
                return cand, ctx, dec

            max_workers = min(len(to_analyze), 3)  # GLM-5 parallelism limit
            analyzed: list[tuple[dict, dict, dict | None]] = []
            if to_analyze:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    # Key futures by index so we can reassemble in submission order.
                    futures = {
                        executor.submit(_analyze_one, item): i
                        for i, item in enumerate(to_analyze)
                    }
                    results_by_idx: dict[int, tuple] = {}
                    for future in as_completed(futures):
                        results_by_idx[futures[future]] = future.result()
                    analyzed = [results_by_idx[i] for i in range(len(to_analyze))]

            # ── Phase C: sequential posting (browser lock re-acquired) ───────────
            with concurrency("twitter-browser", occupy=1):
                for candidate, tweet_context, decision in analyzed:
                    if engaged_count >= 8:
                        break

                    tweet_id = candidate["tweetId"]
                    url = candidate["url"]
                    author = candidate.get("author") or "unknown"
                    search_term = candidate.get("searchTerm", "")

                    print(f"\nProcessing {tweet_id} (@{author})...")

                    if not decision or not decision.get("shouldEngage"):
                        if decision:
                            tweet_text = (tweet_context.get("text") or "")[:120]
                            print(f"  LLM skipped [{tweet_id}]: {tweet_text!r}\n  Decision: {json.dumps(decision)}")
                        continue

                    engagement_type = decision.get("engagementType", "reply")
                    reply_text = decision.get("reply")
                    print(f"  LLM approved [{engagement_type}]: {(reply_text or '')[:80]}...")
                    print(f"  Reasoning: {decision.get('reasoning', 'N/A')}")

                    jitter_sleep()

                    posted = False
                    our_reply_id = None
                    engagement_source = "search"

                    if engagement_type == "like":
                        posted = like_tweet(tweet_id)
                        engagement_source = "like"
                    elif engagement_type == "quote":
                        if not reply_text:
                            print("  Quote-tweet requires reply text — skipping")
                            continue
                        try:
                            reply_text = humanize(reply_text)
                        except Exception as e:
                            print(f"  Humanize failed: {e}")
                            continue
                        MAX_REPLY_ATTEMPTS = 3
                        posted, our_reply_id = post_quote_with_retries(
                            tweet_id,
                            reply_text,
                            attempts=MAX_REPLY_ATTEMPTS,
                            retry_delay_sec=5,
                            our_username=TWITTER_ACCOUNT_USERNAME,
                        )
                        if not posted:
                            send_error_alert(f"Failed to quote-tweet {tweet_id} (@{author}) after {MAX_REPLY_ATTEMPTS} attempts")
                            continue
                        engagement_source = "quote"
                    else:  # "reply"
                        if not reply_text:
                            print("  Reply requires reply text — skipping")
                            continue
                        try:
                            reply_text = humanize(reply_text)
                        except Exception as e:
                            print(f"  Humanize failed: {e}")
                            continue
                        MAX_REPLY_ATTEMPTS = 3
                        posted, our_reply_id = post_reply_with_retries(
                            tweet_id,
                            reply_text,
                            attempts=MAX_REPLY_ATTEMPTS,
                            retry_delay_sec=5,
                            our_username=TWITTER_ACCOUNT_USERNAME,
                        )
                        if not posted:
                            send_error_alert(f"Failed to post reply to {tweet_id} (@{author}) after {MAX_REPLY_ATTEMPTS} attempts")
                            continue

                    if not posted:
                        continue

                    print(f"  {engagement_type.capitalize()}d", flush=True)
                    if our_reply_id:
                        print(f"  Captured ourReplyId: {our_reply_id}", flush=True)

                    # Auto-follow the author after confirmed engagement
                    if engagement_type != "like":
                        auto_follow_after_engagement(
                            conn, author, tweet_id, source=engagement_source
                        )

                    # Insert engagement into DB
                    stats = tweet_context.get("stats", {})
                    insert_engagement(
                        conn,
                        tweet_id=str(tweet_id),
                        target_username=author,
                        our_reply_text=reply_text,
                        our_reply_id=our_reply_id,
                        source=engagement_source,
                        search_term=search_term,
                        conv_likelihood=decision.get("audienceEngagementPotential") or decision.get("conversationLikelihood"),
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
                    results.append(f"{tweet_id} | @{author} [{engagement_type}]")

                    # Update voice context in-memory so subsequent runs in this session
                    # see our latest replies (posting is sequential so no race).
                    if reply_text:
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

    except KeyboardInterrupt:
        print("\nEngagement interrupted by signal — exiting cleanly.", file=sys.stderr)
        return 1
    except Exception as e:
        send_error_alert(f"Autonomous engagement failed: {e}")
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
