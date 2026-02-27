#!/usr/bin/env python3
"""Real-time reply and mention monitor for @DecentCloud_org.

Polls x.com/notifications/mentions every 5 minutes (via Prefect schedule),
detects new replies/mentions, and auto-replies to build back-and-forth
conversations that Twitter's algorithm rewards.

Flow:
1. Load DB state
2. Navigate to x.com/notifications/mentions via CDP
3. Extract tweet IDs + authors from notification articles
4. Filter: skip already-seen, already-engaged, blocked authors, own account
5. For each new mention (cap: 5 per run):
   a. mark_mention_seen via kv_state (before fetch — crashes won't reprocess)
   b. fetch_tweet_context
   c. Cross-ref parentChain IDs with our own post tweet IDs → is_direct_reply flag
   d. draft_mention_reply via LLM (mention-aware framing)
   e. humanize() + jitter_sleep(5-30s)
   f. post_reply() + insert_engagement + auto_follow
6. Final kv_state update
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, "/projects/automations")
from lib.config import TWITTER_BASE_URL
from lib.llm_utils import call_llm_simple as call_llm, extract_json
from db import (
    get_conn,
    get_engaged_tweet_ids,
    get_engagements_with_user,
    get_our_thread_context,
    get_recent_engagements,
    get_recent_posts,
    insert_engagement,
    is_engaged,
    kv_get,
    kv_set,
)
from twitter_utils import (
    BLOCKED_AUTHORS,
    auto_follow_after_engagement,
    cdp_tab,
    fetch_tweet_context,
    humanize,
    jitter_sleep,
    load_project_context,
    post_reply,
    send_error_alert,
    utc_now,
)

OUR_HANDLE = "DecentCloud_org"
OUR_HANDLE_LOWER = OUR_HANDLE.lower()

# How many mentions to process per run (keeps runs fast — 5 min budget)
MAX_MENTIONS_PER_RUN = 5

# Prune seenMentionIds older than this
SEEN_TTL_DAYS = 2

# KV state key for seen mention IDs (stored as JSON string)
KV_SEEN_MENTIONS = "reply_monitor:seen_mentions"


# ---------------------------------------------------------------------------
# Seen-mention helpers (backed by kv_state)
# ---------------------------------------------------------------------------


def load_seen_mentions(conn) -> dict[str, str]:
    """Load seen mention IDs from kv_state. Returns {tweet_id: timestamp}."""
    raw = kv_get(conn, KV_SEEN_MENTIONS)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def save_seen_mentions(conn, seen: dict[str, str]) -> None:
    """Save seen mention IDs to kv_state."""
    kv_set(conn, KV_SEEN_MENTIONS, json.dumps(seen))


def mark_mention_seen(conn, seen: dict[str, str], tweet_id: str) -> None:
    """Record a mention as seen (timestamped). Call BEFORE fetching context."""
    seen[str(tweet_id)] = utc_now()
    save_seen_mentions(conn, seen)


def prune_seen_mentions(conn, seen: dict[str, str]) -> int:
    """Remove seenMentionIds older than SEEN_TTL_DAYS. Returns count pruned."""
    if not seen:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_TTL_DAYS)
    expired = []
    for tid, ts_str in seen.items():
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts < cutoff:
                expired.append(tid)
        except (ValueError, TypeError):
            expired.append(tid)
    for tid in expired:
        del seen[tid]
    if expired:
        save_seen_mentions(conn, seen)
    return len(expired)


# ---------------------------------------------------------------------------
# CDP: scrape mentions page
# ---------------------------------------------------------------------------

MENTIONS_JS = """(() => {
  const articles = document.querySelectorAll('article[data-testid="tweet"]');
  const results = [];
  for (const article of articles) {
    // Author first — we need it to find the correct status link below.
    const userLinks = article.querySelectorAll('a[role="link"]');
    let author = null;
    for (const ul of userLinks) {
      const m = ul.href.match(/^https:\\/\\/x\\.com\\/([^/?#]+)$/);
      if (m && !['search','explore','i','notifications'].includes(m[1])) {
        author = m[1];
        break;
      }
    }

    // Tweet ID: prefer the status link that belongs to the author.
    const links = Array.from(article.querySelectorAll('a[href*="/status/"]'));
    let statusLink = null;
    if (author) {
      statusLink = links.find(l => {
        const m = l.href.match(/^\\/\\/x\\.com\\/([^/]+)\\/status\\/(\\d+)$/) ||
                  l.href.match(/^https:\\/\\/x\\.com\\/([^/]+)\\/status\\/(\\d+)$/);
        return m && m[1].toLowerCase() === author.toLowerCase();
      });
    }
    if (!statusLink) {
      statusLink = links.find(l => /\\/status\\/\\d+$/.test(l.href));
    }
    const tweetId = statusLink ? statusLink.href.match(/\\/status\\/(\\d+)/)?.[1] : null;

    // Text
    const textEl = article.querySelector('[data-testid="tweetText"]');
    const text = textEl ? textEl.textContent.slice(0, 500) : '';

    // URL
    const url = statusLink ? statusLink.href : null;

    if (tweetId && author) {
      results.push({ tweetId, author, text, url });
    }
  }
  // Page is newest-first; return up to 20
  return JSON.stringify(results.slice(0, 20));
})()"""


def scrape_mentions() -> list[dict]:
    """Navigate to /notifications/mentions and extract recent mention articles."""
    url = f"{TWITTER_BASE_URL}/notifications/mentions"
    print(f"  CDP: navigating to {url}", flush=True)
    try:
        with cdp_tab() as cdp:
            if not cdp.navigate(url, wait_sec=4):
                print("  CDP: navigation failed", flush=True)
                return []
            raw = cdp.evaluate(MENTIONS_JS, timeout=20)
    except Exception as e:
        print(f"  CDP: scrape_mentions failed: {e}", flush=True)
        return []

    if not raw:
        print("  CDP: JS evaluation returned nothing", flush=True)
        return []

    try:
        mentions = json.loads(raw) if isinstance(raw, str) else raw
        print(f"  CDP: found {len(mentions)} mention article(s)", flush=True)
        return mentions
    except (json.JSONDecodeError, TypeError) as e:
        print(f"  CDP: failed to parse mentions JSON: {e}", flush=True)
        return []


# ---------------------------------------------------------------------------
# Our tweet IDs: load from DB posts table
# ---------------------------------------------------------------------------


def get_our_post_tweet_ids(conn) -> set[str]:
    """Get all tweet IDs from our posts table."""
    with conn.cursor() as cur:
        cur.execute("SELECT tweet_id FROM posts")
        return {row[0] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# LLM: draft reply for a mention
# ---------------------------------------------------------------------------


def draft_mention_reply(
    mention: dict,
    tweet_context: dict,
    recent_engagements: list[dict],
    recent_posts: list[dict],
    is_direct_reply: bool,
    prior_exchanges: list[dict] | None = None,
) -> dict | None:
    """Draft a reply to a mention/reply via LLM.

    is_direct_reply=True  -> someone replied to one of our tweets (high priority,
                             "continue this conversation" framing)
    is_direct_reply=False -> cold mention of @DecentCloud_org
    """
    # Build recent activity for voice consistency
    recent_our_replies = ""
    recent_our_posts_text = ""

    if recent_engagements:
        lines = [
            f'  - @{r.get("target_username", "?")} <- "{(r.get("our_reply_text") or "")[:120]}"'
            for r in recent_engagements
        ]
        recent_our_replies = "\n".join(lines)

    posts = [
        p for p in recent_posts
        if p.get("type") in ("post", "value-drop", "dev-update", "thread")
    ]
    if posts:
        lines = [f'  - "{(p.get("text") or "")[:120]}"' for p in posts]
        recent_our_posts_text = "\n".join(lines)

    # Conversation ancestry — read from oldest (root) to newest so the LLM sees the
    # full arc in chronological order, not reversed.
    parent_chain_text = "None (original tweet, not a reply)"
    if tweet_context.get("parentChain"):
        parts = []
        for i, p in enumerate(tweet_context["parentChain"], 1):
            parts.append(f'  [{i}] @{p.get("username", "?")} said: "{p.get("text", "")}"')
        parent_chain_text = "\n".join(parts)

    # Other replies in the thread
    other_replies_text = "None visible yet"
    if tweet_context.get("otherReplies"):
        lines = [
            f'  - @{r.get("username", "?")}: "{r.get("text", "")}"'
            for r in tweet_context["otherReplies"]
        ]
        other_replies_text = "\n".join(lines)

    # Check if this is part of one of our posted threads
    all_visible_ids = [tweet_context.get("tweetId", "")]
    for p in tweet_context.get("parentChain") or []:
        if p.get("tweetId"):
            all_visible_ids.append(p["tweetId"])
    for t in tweet_context.get("threadContinuation") or []:
        if t.get("id"):
            all_visible_ids.append(t["id"])
    our_thread_note = get_our_thread_context(conn, [i for i in all_visible_ids if i])

    # Prior exchanges with this specific person (from DB — no API calls)
    prior_exchanges_text = "None on record."
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

    if is_direct_reply:
        engagement_threshold = """This person DIRECTLY REPLIED to one of our tweets. High priority to continue the conversation — but only if the reply makes sense in context."""
    else:
        engagement_threshold = """This is a cold mention of @DecentCloud_org. Apply normal quality filter: engage only if we can add something specific and genuinely useful to the discussion."""

    prompt = f"""You are the voice of @DecentCloud_org on Twitter. Someone mentioned or replied to us.

# Project & Strategy Context
{project_context}

# The Mention / Reply
**Author:** @{tweet_context["author"]} ({tweet_context.get("authorName", "")})
**Their message:** {tweet_context["text"]}
**Stats:** {tweet_context["stats"]["likes"]} likes, {tweet_context["stats"]["retweets"]} RTs, {tweet_context["stats"]["replies"]} replies
**Is this a direct reply to one of our tweets?** {"YES" if is_direct_reply else "NO (cold mention)"}

# Full Thread Context (read top-to-bottom — this is the conversation so far)

**Conversation ancestry — oldest first, newest just before the message above:**
{parent_chain_text}

**Other replies visible in this thread:**
{other_replies_text}

**Author's own follow-up tweets (thread continuation):**
{json.dumps(tweet_context.get("threadContinuation"), indent=2) if tweet_context.get("threadContinuation") else "None"}

**Quoted tweet:**
{json.dumps(tweet_context.get("quotedTweet"), indent=2) if tweet_context.get("quotedTweet") else "None"}

**Our cached thread history (if we're part of this thread already):**
{our_thread_note if our_thread_note else "Not part of one of our threads."}

**Our prior exchanges with @{tweet_context["author"]} (from DB — full history):**
{prior_exchanges_text}

# Our Recent Activity (voice consistency — DO NOT repeat these angles)
**Our last 8 replies:**
{recent_our_replies or "  (none yet)"}

**Our recent original posts:**
{recent_our_posts_text or "  (none yet)"}

# COHERENCE GATE — Apply this BEFORE deciding to engage

Read everything above. Then answer internally:
1. **What is this conversation actually about?** Reconstruct the topic from the full thread (ancestry + other replies + their message). If you cannot state a clear topic in one sentence, do not engage.
2. **What is the register?** Is this a technical debate, a casual joke exchange, venting, sarcasm, banter? Your reply must match that register. A deadpan expert take into a joke thread is as bad as a joke into a serious debate.
3. **What did WE say before, and how did it land?** Check "Prior exchanges with this person" above. If we've already had one or more exchanges in this thread and the tone suggests we were called out, corrected, or the person is frustrated with us, apply maximum scrutiny — it is usually better to stay silent than to double down.
4. **What exactly are they saying to us?** Not the topic in general — specifically, what is their message in the context of the full thread? Reply to THAT, not to your own interpretation of the general topic.

You MUST qualify for one of these two modes to engage:

**Mode A — Sharp standalone take:** You have a specific fact, honest observation, or a genuinely funny/cynical line that works even for someone who hasn't read the whole thread. It earns likes from the audience on its own merits. It does NOT contradict or miss the point of what was actually said.

**Mode B — Coherent continuation:** You understand exactly what was said and your reply clearly follows from and advances this specific conversation. Someone reading the thread would see your reply as a natural, on-point next step — not a tangent, not a repeat of something already said, not an expert monologue into casual banter.

If you cannot confidently qualify for Mode A or Mode B, set shouldEngage: false.

# Engagement Threshold
{engagement_threshold}

# Reply Rules (always apply when engaging)
- 1-2 sentences max, under 280 chars
- Observational voice, not imperative — describe what's happening, don't tell them what to do
- Peer voice — knowledgeable friend, not a brand account
- NO product mentions ("DecentCloud", our product) in Phase 1
- NO hashtags, NO links, NO "check us out", NO "follow for updates"
- If they asked a question, answer it directly and specifically
- profileClickWorthy: only true if you're adding a specific fact, a non-obvious angle, or a well-supported pushback. Pure validation fails this check regardless.

# Output Format (JSON)
{{
  "shouldEngage": true/false,
  "conversationLikelihood": 1-10,
  "profileClickWorthy": true/false,
  "mode": "A" or "B" (which mode qualifies this reply, or null if not engaging),
  "threadSummary": "one sentence: what is this conversation actually about",
  "reasoning": "brief explanation of decision",
  "reply": "draft reply text here" (or null if shouldEngage is false)
}}

Output ONLY valid JSON, nothing else."""

    try:
        response = call_llm(prompt, timeout=120)
        if not response:
            print("  LLM returned nothing", flush=True)
            return None

        json_str = extract_json(response)
        if json_str is None:
            print("  Could not extract JSON from LLM response, retrying...", flush=True)
            response2 = call_llm(prompt, timeout=120)
            if response2:
                json_str = extract_json(response2)
            if json_str is None:
                return None

        decision = json.loads(json_str)

        conv_score = decision.get("conversationLikelihood", 5)
        profile_click_worthy = decision.get("profileClickWorthy", False)
        # Direct replies always get a response; cold mentions need a score >= 5
        if not decision.get("shouldEngage") or (not is_direct_reply and conv_score < 5):
            reason = decision.get("reasoning", "no reason")
            if decision.get("shouldEngage") and conv_score < 5:
                print(
                    f"  Low conversation likelihood ({conv_score}/10): {reason}",
                    flush=True,
                )
            else:
                print(f"  LLM decided NOT to engage: {reason}", flush=True)
            decision["shouldEngage"] = False
            return decision

        # For cold mentions: also require profileClickWorthy (direct replies bypass this)
        if not is_direct_reply and not profile_click_worthy:
            print(
                f"  Not profile-click-worthy (pure validation), skipping",
                flush=True,
            )
            decision["shouldEngage"] = False
            return decision

        reply = decision.get("reply")
        if not reply or len(reply) > 280:
            print("  LLM reply invalid (empty or too long)", flush=True)
            return None

        return decision

    except Exception as e:
        print(f"  Exception in LLM analysis: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=== REPLY MONITOR ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    try:
        with get_conn() as conn:
            # Load state from DB
            seen_mentions = load_seen_mentions(conn)

            # Prune stale seen IDs
            pruned = prune_seen_mentions(conn, seen_mentions)
            if pruned:
                print(f"Pruned {pruned} expired seenMentionIds", flush=True)

            seen_ids = set(seen_mentions.keys())
            engaged_ids = get_engaged_tweet_ids(conn)
            our_tweet_ids = get_our_post_tweet_ids(conn)

            # Voice consistency context
            recent_engagements = get_recent_engagements(conn, hours=168, limit=8)
            recent_posts = get_recent_posts(conn, limit=5)

            # Scrape notifications/mentions page
            mentions = scrape_mentions()
            if not mentions:
                print("No mentions found on page", flush=True)
                return 0

            # Filter to new, actionable mentions
            new_mentions = []
            for m in mentions:
                tid = str(m.get("tweetId", ""))
                author = (m.get("author") or "").lower()
                text = m.get("text", "")

                if not tid:
                    continue
                if tid in seen_ids:
                    continue
                if tid in engaged_ids:
                    # Mark as seen so we don't keep checking it
                    mark_mention_seen(conn, seen_mentions, tid)
                    seen_ids.add(tid)
                    continue
                if author == OUR_HANDLE_LOWER:
                    # Skip our own posts
                    continue
                if author in {a.lower() for a in BLOCKED_AUTHORS}:
                    print(f"  Skipping blocked author @{author}", flush=True)
                    mark_mention_seen(conn, seen_mentions, tid)
                    seen_ids.add(tid)
                    continue
                new_mentions.append(m)

            if not new_mentions:
                print("No new actionable mentions", flush=True)
                return 0

            print(f"Found {len(new_mentions)} new mention(s) to process", flush=True)

            processed = 0
            for mention in new_mentions:
                if processed >= MAX_MENTIONS_PER_RUN:
                    break

                tid = str(mention["tweetId"])
                author = mention.get("author", "unknown")

                # Re-check the live dict — guards against duplicates
                if tid in seen_mentions:
                    continue

                print(f"\nProcessing mention {tid} (@{author})...", flush=True)

                # Mark seen BEFORE fetching context — crash safety
                mark_mention_seen(conn, seen_mentions, tid)
                seen_ids.add(tid)

                # Fetch full thread context
                print("  Fetching tweet context...", flush=True)
                tweet_context = fetch_tweet_context(tid)
                if not tweet_context:
                    print(f"  Failed to fetch context for {tid}", flush=True)
                    continue

                # Guard: skip if the tweet itself is ours
                if (tweet_context.get("author") or "").lower() == OUR_HANDLE_LOWER:
                    print(f"  Tweet {tid} is our own — skipping", flush=True)
                    continue

                # Guard: skip if we've already replied to this tweet
                already_replied = any(
                    (r.get("username") or "").lower() == OUR_HANDLE_LOWER
                    for r in (tweet_context.get("otherReplies") or [])
                )
                if already_replied:
                    print(f"  Already replied to {tid} — skipping", flush=True)
                    continue

                # Determine if this is a direct reply to one of our tweets
                parent_ids = {
                    str(p.get("tweetId", ""))
                    for p in (tweet_context.get("parentChain") or [])
                    if p.get("tweetId")
                }
                is_direct_reply = bool(parent_ids & our_tweet_ids)
                print(
                    f"  is_direct_reply={is_direct_reply} "
                    f"(parent_ids={parent_ids & our_tweet_ids or '{}'})",
                    flush=True,
                )

                # Fetch prior exchanges with this author from DB (no API calls)
                prior_exchanges = get_engagements_with_user(conn, author)

                # LLM drafts the reply
                print("  Asking LLM to draft reply...", flush=True)
                decision = draft_mention_reply(
                    mention, tweet_context, recent_engagements, recent_posts,
                    is_direct_reply, prior_exchanges
                )

                if not decision or not decision.get("shouldEngage"):
                    continue

                reply_text = decision["reply"]
                print(f"  LLM approved: {reply_text[:80]}...", flush=True)
                print(f"  Reasoning: {decision.get('reasoning', 'N/A')}", flush=True)

                try:
                    reply_text = humanize(reply_text)
                except Exception as e:
                    print(f"  Humanize failed: {e}", flush=True)
                    continue

                jitter_sleep(min_sec=5, max_sec=30)

                url = mention.get("url") or f"{TWITTER_BASE_URL}/i/web/status/{tid}"

                posted, our_reply_id = post_reply(tid, reply_text)
                if not posted:
                    send_error_alert(
                        f"Reply monitor: failed to post reply to {tid} (@{author})"
                    )
                    continue

                print("  Replied", flush=True)
                if our_reply_id:
                    print(f"  Captured ourReplyId: {our_reply_id}", flush=True)

                # Auto-follow the author
                auto_follow_after_engagement(conn, author, tid)

                # Insert engagement record
                insert_engagement(
                    conn,
                    tweet_id=tid,
                    target_username=author,
                    our_reply_text=reply_text,
                    our_reply_id=our_reply_id,
                    source="mention" if not is_direct_reply else "direct_reply",
                    conv_likelihood=decision.get("conversationLikelihood"),
                    profile_click_worthy=decision.get("profileClickWorthy"),
                    llm_reasoning=decision.get("reasoning"),
                )

                engaged_ids.add(tid)

                # Update voice context in-memory
                recent_engagements = [
                    {
                        "target_username": author,
                        "our_reply_text": reply_text,
                        "replied_at": utc_now(),
                    }
                ] + recent_engagements[:7]

                processed += 1

            kv_set(conn, "reply_monitor:last_run", utc_now())

        if processed:
            print(f"\nProcessed {processed} mention(s)", flush=True)
        else:
            print("\nNo mentions engaged (LLM filtered all)", flush=True)

        return 0

    except Exception as e:
        send_error_alert(f"Reply monitor failed: {e}")
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
