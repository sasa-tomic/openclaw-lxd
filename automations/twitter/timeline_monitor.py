#!/usr/bin/env python3
"""Following-feed timeline monitor for @DecentCloud_org.

Polls x.com/home (the Following feed) every 5 minutes (via Prefect schedule).
Finds infra-relevant tweets from tracked practitioners within the first
20-30 minutes of their life and replies immediately — shorter jitter than
the engagement script so we land in the first few replies.

Flow:
1. Load DB state — get timeline:last_seen_id and tracked accounts
2. Navigate to {TWITTER_BASE_URL}/home via CDP, then click the Following tab
3. Scrape visible tweets (tweetId, text, author, url, timestamp)
4. Filter to only tweets newer than last_seen_id
5. Keyword pre-filter: only pass infra/cloud signals to LLM
6. For each passing tweet: fetch thread context, LLM decision, reply if approved
7. Update timeline:last_seen_id to newest tweet seen
8. Save state to DB
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/projects/automations")
from lib.config import TWITTER_BASE_URL
from lib.llm_utils import call_llm_simple as call_llm, extract_json
from db import (
    get_conn,
    get_engaged_tweet_ids,
    get_all_followed_usernames,
    get_our_thread_context,
    get_recent_engagements,
    get_recent_posts,
    increment_engagement_count,
    insert_engagement,
    is_engaged,
    kv_get,
    kv_set,
    set_account_last_seen_tweet_at,
    TWITTER_ACCOUNT_USERNAME,
    upsert_account,
)
from twitter_utils import (
    BLOCKED_AUTHORS,
    auto_follow_after_engagement,
    cdp_tab,
    fetch_tweet_context,
    humanize,
    is_english_text,
    jitter_sleep,
    load_project_context,
    post_reply_with_retries,
    send_error_alert,
    utc_now,
)

OUR_HANDLE = TWITTER_ACCOUNT_USERNAME
OUR_HANDLE_LOWER = OUR_HANDLE.lower()

# Max tweets to process per run (keeps each run within 5 min budget)
MAX_TWEETS_PER_RUN = 5

# Maximum tweet age to consider (first-run guard)
MAX_TWEET_AGE_MIN = 60

# Shorter jitter — we want to land in the first few replies
JITTER_MIN_SEC = 10
JITTER_MAX_SEC = 30

# Minimum conversation likelihood required (alongside profileClickWorthy)
CONV_LIKELIHOOD_THRESHOLD = 6

# ---------------------------------------------------------------------------
# Age filter
# ---------------------------------------------------------------------------


def is_recent(tweet: dict, max_age_min: int = MAX_TWEET_AGE_MIN) -> bool:
    """Return True if the tweet is within max_age_min minutes old.

    If no timestamp is available or parsing fails, allow the tweet through so
    we do not silently drop tweets due to missing data.
    """
    ts = tweet.get("timestamp")
    if not ts:
        return True  # no timestamp, don't filter it out
    try:
        tweet_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - tweet_time).total_seconds() / 60
        return age_min <= max_age_min
    except Exception:
        return True  # parse failure, don't filter


def parse_tweet_timestamp(ts: str | None) -> datetime | None:
    """Parse an ISO tweet timestamp, returning timezone-aware datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# CDP: scrape home/following feed
# ---------------------------------------------------------------------------

TIMELINE_JS = """(() => {
  const articles = document.querySelectorAll('article[data-testid="tweet"]');
  const results = [];
  const seen = new Set();

  for (const article of articles) {
    // Author: first profile link that isn't a reserved path
    const userLinks = article.querySelectorAll('a[role="link"]');
    let author = null;
    for (const ul of userLinks) {
      const m = ul.href.match(/^https:\\/\\/x\\.com\\/([^/?#]+)$/);
      if (m && !['search','explore','i','notifications','home'].includes(m[1])) {
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

    // URL (full permalink)
    const url = statusLink ? statusLink.href : null;

    // Timestamp
    const timeEl = article.querySelector("time");
    const timestamp = timeEl ? timeEl.getAttribute("datetime") : null;

    if (tweetId && author && !seen.has(tweetId)) {
      seen.add(tweetId);
      results.push({ tweetId, author, text, url, timestamp });
    }
  }

  // Home feed renders newest-first; return up to 25 articles
  return JSON.stringify(results.slice(0, 25));
})()"""


CLICK_FOLLOWING_TAB_JS = """
(function() {
    const tabs = document.querySelectorAll('[role="tab"]');
    for (const tab of tabs) {
        if (tab.textContent.trim().toLowerCase() === 'following') {
            tab.click();
            return 'clicked';
        }
    }
    return 'not_found';
})()
"""


def scrape_timeline() -> list[dict]:
    """Navigate to /home and extract tweet articles from the Following feed."""
    import time

    url = f"{TWITTER_BASE_URL}/home"
    print(f"  CDP: navigating to {url}", flush=True)
    try:
        with cdp_tab() as cdp:
            if not cdp.navigate(url, wait_sec=4):
                print("  CDP: navigation failed", flush=True)
                return []

            # Try to click the Following tab so we get the chronological feed
            tab_result = cdp.evaluate(CLICK_FOLLOWING_TAB_JS)
            if tab_result == "clicked":
                print("  CDP: clicked Following tab — waiting for feed update", flush=True)
                time.sleep(2)
            else:
                print(
                    "  CDP: Following tab not found — proceeding with default feed",
                    flush=True,
                )

            raw = cdp.evaluate(TIMELINE_JS, timeout=20)
    except Exception as e:
        print(f"  CDP: scrape_timeline failed: {e}", flush=True)
        return []

    if not raw:
        print("  CDP: JS evaluation returned nothing", flush=True)
        return []

    try:
        tweets = json.loads(raw) if isinstance(raw, str) else raw
        print(f"  CDP: found {len(tweets)} tweet article(s)", flush=True)
        return tweets
    except (json.JSONDecodeError, TypeError) as e:
        print(f"  CDP: failed to parse timeline JSON: {e}", flush=True)
        return []


# ---------------------------------------------------------------------------
# LLM: draft reply for a timeline tweet
# ---------------------------------------------------------------------------


def draft_timeline_reply(
    tweet: dict,
    tweet_context: dict,
    recent_engagements: list[dict],
    recent_posts: list[dict],
    conn=None,
) -> dict | None:
    """Draft a reply to a Following-feed tweet via LLM.

    Uses a slightly lower threshold than search-based engagement: we don't
    need explicit pain, just a relevant topic we can add non-obvious value to.
    Requires BOTH conversationLikelihood >= 6 AND profileClickWorthy == true.
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

    # Conversation ancestry
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

    # Thread context (if we've interacted with this thread before)
    all_visible_ids = [tweet_context.get("tweetId", "")]
    for p in tweet_context.get("parentChain") or []:
        if p.get("tweetId"):
            all_visible_ids.append(p["tweetId"])
    for t in tweet_context.get("threadContinuation") or []:
        if t.get("id"):
            all_visible_ids.append(t["id"])
    our_thread_note = get_our_thread_context(conn, [i for i in all_visible_ids if i])

    project_context = load_project_context()

    prompt = f"""You are the voice of @DecentCloud_org on Twitter. Analyze this tweet and decide if/how to engage.

# Project & Strategy Context
{project_context}

# Tweet to Analyze
**Author:** @{tweet_context["author"]} ({tweet_context.get("authorName", "")})
**Tweet:** {tweet_context["text"]}
**Stats:** {tweet_context["stats"]["likes"]} likes, {tweet_context["stats"]["retweets"]} RTs, {tweet_context["stats"]["replies"]} replies

# Full Conversation Context

**Conversation ancestry (what this tweet is replying to):**
{parent_chain_text}

**Author's thread continuation (their own follow-up tweets):**
{json.dumps(tweet_context.get("threadContinuation"), indent=2) if tweet_context.get("threadContinuation") else "None (single tweet or no continuation yet)"}

**Other replies already in this thread:**
{other_replies_text}

**Quoted tweet:**
{json.dumps(tweet_context.get("quotedTweet"), indent=2) if tweet_context.get("quotedTweet") else "None"}

**Our cached thread context (previous exchanges in this thread, if any):**
{our_thread_note if our_thread_note else "No cached history — first time seeing this conversation."}

# Our Recent Activity (for voice consistency — DO NOT repeat these angles)
**Our last 8 replies:**
{recent_our_replies or "  (none yet)"}

**Our recent original posts:**
{recent_our_posts_text or "  (none yet)"}

# Engagement Decision

This is a tweet from someone we follow — a tracked practitioner. We chose to follow them because they're target audience. Apply a slightly lower threshold: we don't need explicit pain, just a relevant topic we can add non-obvious value to.

**ENGAGE if:**
- The tweet touches cloud/infra/compute/marketplace in a way we can sharpen with a specific angle
- We can name a mechanic they haven't named, or push back with facts they probably don't have
- The author is likely to see and respond to a precise, non-obvious observation

**SKIP if:**
- The topic is entirely off-topic for cloud/infra/compute (personal, politics, sports, etc.)
- Our reply would only validate ("so true!", "exactly", "been there") — pure agreement adds nothing
- The thread already has >50 replies and our voice would be drowned out
- We have nothing non-obvious to say

**Rate conversation likelihood (1-10):**
Ask: if we reply with something genuinely sharp, will they write back?
- High (8-10): they asked a question; made a debatable claim; expressed unresolved pain; they engage in replies
- Medium (5-7): we can add a non-obvious angle that makes them think "huh — interesting"
- Low (1-4): they're broadcasting, not conversing; our reply is just noise

**profileClickWorthy check:**
A reply is profileClickWorthy ONLY if it contains at least one of:
- A specific fact or number the author likely doesn't know
- A non-obvious angle on the mechanic they're describing
- A well-supported pushback on something they said

Pure validation ("so true", "exactly", "yeah exactly", "been there", "this is real") fails this check regardless of conversation likelihood.

# Hard rules (Phase 1)
- Length: 1 word to 2 sentences, under 280 chars. Short is often better — "Agreed.", "Hard disagree.", "Interesting angle." are valid replies when they're a genuine, direct response to what was said.
- The reply MUST directly answer or follow up on the original post or the latest message in the thread. React to what they actually said.
- Agreement, disagreement, or any other reaction is fine — what matters is that it's direct and specific.
- Follow-up: add one when it naturally extends the conversation — "And how would you handle X?", "Does that still hold at scale?", "What's your take on the tradeoff?" — only when you have something specific to ask.
- Vary your openers — check "Our last 8 replies" above and never reuse a word or phrase you opened with recently. Draw from: Agreed. / Fair point. / That tracks. / Exactly. / Disagree. / Hard disagree. / Not quite. / Depends. / Interesting angle. / Worth noting: / The tradeoff is… / The catch is… / Only partially. / Curious — / True, but…
- Observational voice — describe what happens, don't tell them what to do
- Name the underlying mechanic with specifics: numbers, timeframes, concrete details
- Peer voice — knowledgeable friend, not a brand account
- NO product mentions (DecentCloud, our product)
- NO hashtags, NO links
- NO imperative framing ("make sure you...", "you should...", "switch to...")
- Standard sentence capitalization: capitalize the first word and proper nouns (AWS, GCP, Stripe, etc.)
- profileClickWorthy check: only reply if you're adding a specific fact, a non-obvious angle, or a well-supported pushback. Pure validation ("so true", "exactly", "been there", "this is real") fails this check regardless of conversation likelihood.

# Output Format (JSON)
{{
  "shouldEngage": true/false,
  "conversationLikelihood": 1-10,
  "profileClickWorthy": true/false,
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

        if not decision.get("shouldEngage"):
            reason = decision.get("reasoning", "no reason")
            print(f"  LLM decided NOT to engage: {reason}", flush=True)
            decision["shouldEngage"] = False
            return decision

        if conv_score < CONV_LIKELIHOOD_THRESHOLD:
            reason = decision.get("reasoning", "no reason")
            print(
                f"  Low conversation likelihood ({conv_score}/10): {reason}",
                flush=True,
            )
            decision["shouldEngage"] = False
            return decision

        if not profile_click_worthy:
            reason = decision.get("reasoning", "no reason")
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
    print("=== TIMELINE MONITOR ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    try:
        with get_conn() as conn:
            # Load DB state
            last_seen_id = kv_get(conn, "timeline:last_seen_id")
            engaged_ids = get_engaged_tweet_ids(conn)

            tracked_usernames = get_all_followed_usernames(conn)
            tracked_usernames_lc = {u.lower() for u in tracked_usernames}

            # Voice consistency context
            recent_engagements = get_recent_engagements(conn, hours=168, limit=8)
            recent_posts = get_recent_posts(conn, limit=5)

            # Scrape the Following chronological feed
            tweets = scrape_timeline()
            if not tweets:
                print("No tweets found on timeline", flush=True)
                return 0

            # Determine newest tweet seen this run (tweets[0] is newest-first)
            newest_id_this_run = tweets[0]["tweetId"] if tweets else None

            # Filter to only tweets newer than last_seen_id.
            # Tweet IDs are Twitter snowflakes — higher ID = more recent.
            new_tweets = []
            for t in tweets:
                tid = str(t.get("tweetId", ""))
                if not tid:
                    continue
                if last_seen_id:
                    try:
                        if int(tid) <= int(last_seen_id):
                            break
                    except (ValueError, TypeError):
                        continue
                new_tweets.append(t)

            # Age filter
            new_tweets = [t for t in new_tweets if is_recent(t)]

            if not new_tweets:
                print("No new tweets since last run", flush=True)
                if newest_id_this_run:
                    kv_set(conn, "timeline:last_seen_id", newest_id_this_run)
                return 0

            print(f"Found {len(new_tweets)} new tweet(s) to evaluate", flush=True)

            # Update last_seen_id now (before processing) so crashes don't re-scan
            if newest_id_this_run:
                kv_set(conn, "timeline:last_seen_id", newest_id_this_run)

            processed = 0
            for tweet in new_tweets:
                if processed >= MAX_TWEETS_PER_RUN:
                    break

                tid = str(tweet["tweetId"])
                author = tweet.get("author", "unknown")
                text = tweet.get("text", "")
                author_lc = author.lower()

                # Skip our own tweets
                if author_lc == OUR_HANDLE_LOWER:
                    continue

                # Skip blocked authors
                if author_lc in {a.lower() for a in BLOCKED_AUTHORS}:
                    print(f"  Skipping blocked author @{author}", flush=True)
                    continue

                # Following feed can include recommendations/ads; process only tracked accounts
                if author_lc not in tracked_usernames_lc:
                    continue

                seen_at = parse_tweet_timestamp(tweet.get("timestamp"))
                if seen_at:
                    set_account_last_seen_tweet_at(conn, author, seen_at)

                # Skip already-engaged tweets
                if tid in engaged_ids:
                    print(f"  Already engaged with {tid} — skipping", flush=True)
                    continue

                print(f"\nProcessing tweet {tid} (@{author})...", flush=True)
                print(f"  Text: {text[:100]}...", flush=True)
                if not is_english_text(text):
                    print(f"  Skipping non-English tweet {tid}", flush=True)
                    continue

                # Fetch full thread context
                print("  Fetching tweet context...", flush=True)
                tweet_context = fetch_tweet_context(tid)
                if not tweet_context:
                    print(f"  Failed to fetch context for {tid}", flush=True)
                    continue
                if not is_english_text(tweet_context.get("text")):
                    print(f"  Context confirms non-English tweet {tid} — skipping", flush=True)
                    continue

                # Guard: skip our own tweets (scraping can misidentify)
                if (tweet_context.get("author") or "").lower() == OUR_HANDLE_LOWER:
                    print(f"  Tweet {tid} is our own — skipping", flush=True)
                    continue

                # Guard: skip if we already replied (ground-truth check via thread context)
                already_replied = any(
                    (r.get("username") or "").lower() == OUR_HANDLE_LOWER
                    for r in (tweet_context.get("otherReplies") or [])
                )
                if already_replied:
                    print(f"  Already replied to {tid} — skipping", flush=True)
                    continue

                # LLM decision
                print("  Asking LLM to draft reply...", flush=True)
                decision = draft_timeline_reply(
                    tweet, tweet_context, recent_engagements, recent_posts, conn=conn
                )

                if not decision or not decision.get("shouldEngage"):
                    continue

                reply_text = decision["reply"]
                conv_score = decision.get("conversationLikelihood", 0)
                print(
                    f"  LLM approved (conv={conv_score}/10, "
                    f"profileClickWorthy={decision.get('profileClickWorthy')}): "
                    f"{reply_text[:80]}...",
                    flush=True,
                )
                print(f"  Reasoning: {decision.get('reasoning', 'N/A')}", flush=True)

                try:
                    reply_text = humanize(reply_text)
                except Exception as e:
                    print(f"  Humanize failed: {e}", flush=True)
                    continue

                jitter_sleep(min_sec=JITTER_MIN_SEC, max_sec=JITTER_MAX_SEC)

                url = tweet.get("url") or f"{TWITTER_BASE_URL}/i/web/status/{tid}"

                posted, our_reply_id = post_reply_with_retries(
                    tid, reply_text, attempts=1, retry_delay_sec=5, our_username=OUR_HANDLE
                )
                if not posted:
                    send_error_alert(
                        f"Timeline monitor: failed to post reply to {tid} (@{author})"
                    )
                    continue

                print("  Reply posted", flush=True)
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
                    source="timeline",
                    conv_likelihood=conv_score,
                    profile_click_worthy=decision.get("profileClickWorthy"),
                    llm_reasoning=decision.get("reasoning"),
                )

                engaged_ids.add(tid)

                # Increment engagement count for tracked accounts
                increment_engagement_count(conn, author)

                # If author is not in accounts, add as engaged
                upsert_account(
                    conn,
                    author,
                    discovery_source="engagement",
                    stage="engaged",
                )

                # Update voice context in-memory
                recent_engagements = [
                    {
                        "target_username": author,
                        "our_reply_text": reply_text,
                        "replied_at": utc_now(),
                    }
                ] + recent_engagements[:7]

                processed += 1

            # Final run timestamp
            kv_set(conn, "timeline:last_run", utc_now())

        if processed:
            print(f"\nProcessed {processed} timeline tweet(s)", flush=True)
        else:
            print("\nNo timeline tweets engaged (keyword/LLM filtered all)", flush=True)

        return 0

    except Exception as e:
        send_error_alert(f"Timeline monitor failed: {e}")
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
