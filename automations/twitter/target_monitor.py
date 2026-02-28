#!/usr/bin/env python3
"""Target account monitor for @DecentCloud_org.

Strategy: Reply hijacking (precision, not spam) -- curated list of cloud/infra
accounts with 5k-100k followers. Reply within 2 minutes of a new tweet with
something actually useful.

Flow:
1. Load DB state
2. Check if current time is in US peak hours (13:00-03:00 UTC)
3. For each target account (randomized order each run):
   a. Skip if checked within last 25 minutes (per-account kv_state)
   b. Navigate to x.com/{username}/with_replies
   c. JS: extract first article tweet ID and timestamp
   d. If tweet ID changed and tweet is < 30 min old:
      - fetch_tweet_context(new_tweet_id)
      - get_user_profile(username)
      - LLM: draft reply (conv likelihood threshold >= 7)
      - If shouldEngage AND conv likelihood >= 7: post_reply()
      - Insert engagement to DB
   e. Update lastCheckedAt, lastTweetId in kv_state
4. kv_set last run time
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/projects/automations")
from lib.llm_utils import call_llm_simple as call_llm, extract_json
from db import (
    get_conn,
    get_our_thread_context,
    get_recent_engagements,
    get_recent_posts,
    insert_engagement,
    is_engaged,
    kv_get,
    kv_set,
    upsert_account,
)
from twitter_utils import (
    auto_follow_after_engagement,
    cdp_tab,
    fetch_tweet_context,
    get_user_profile,
    humanize,
    jitter_sleep,
    load_project_context,
    post_reply,
    send_error_alert,
    utc_now,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# US peak hours: 8am-11pm EST = 13:00-03:00 UTC (spans midnight)
PEAK_START_UTC = 13  # 13:00 UTC = 8am EST
PEAK_END_UTC = 3     # 03:00 UTC = 11pm EST

# How old a cached check can be before we re-check (25 min to fit 30 min window)
MIN_CHECK_INTERVAL_MIN = 25

# Tweet must be less than this many minutes old to qualify for fast reply
MAX_TWEET_AGE_MIN = 30

# Conversation likelihood threshold -- higher bar for targeted accounts
CONV_LIKELIHOOD_THRESHOLD = 7

# Max replies per run across all target accounts
MAX_REPLIES_PER_RUN = 3

# KV key prefix for per-account state
KV_PREFIX = "target_monitor:account:"

# KV key for global replied IDs (stored as JSON list, capped)
KV_REPLIED_IDS = "target_monitor:replied_ids"
MAX_REPLIED_IDS = 500

TARGET_ACCOUNTS = [
    # Cloud cost critics (high follower, active on cost topics)
    "forrestbrazeal",   # Cloud economist, AWS skeptic
    "kelseyhightower",  # Kubernetes pioneer
    "jbeda",            # k8s co-creator
    "mitchellh",        # HashiCorp founder
    "copyconstruct",    # Charity Majors - observability
    "dhh",              # Rails creator, cloud skeptic
    "levelsio",         # Indie hacker, self-hosting
    "simonw",           # datasette, cloud tools
    "apenwarr",         # Google engineer, infra blog
    "patio11",          # stripe advisor, infra business
    # GPU/AI compute critics
    "swyx",             # AI infra, cost discussions
    "karpathy",         # AI researcher
    "goodside",         # AI practitioner
    # DevOps/SRE
    "mipsytipsy",       # Honeycomb CTO
    "lizthegrey",       # SRE, cloud infra
    "lolwut666",        # Infra practitioner
    "jesseplusplus",    # Cloud engineer
    "nkdobbins",        # Platform engineering
    # P2P/decentralized compute
    "jasonlk",          # SaaStr, cloud buyer frustration
    "benedictevans",    # Tech analyst
    "swardley",         # Wardley mapping, cloud strategy
    # Cloud pricing critics
    "cloudeconomist",   # AWS pricing
    "cloudoptimizer",   # Cloud cost
]

# File-based state path (used by file-based helpers below)
STATE_PATH = Path("/home/openclaw/clawd/memory/target-monitor-state.json")

# ---------------------------------------------------------------------------
# File-based state helpers (used by tests and optional file-backed operation)
# ---------------------------------------------------------------------------


def load_monitor_state() -> dict:
    """Load monitor state from STATE_PATH JSON file, or return empty defaults."""
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text())
    except Exception:
        pass
    return {"accounts": {}, "repliedToIds": [], "lastRunAt": None}


def save_monitor_state(state: dict) -> None:
    """Save monitor state dict to STATE_PATH as JSON."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def get_account_state(state: dict, username: str) -> dict:
    """Return (and initialize if missing) per-account state in the state dict."""
    accounts = state.setdefault("accounts", {})
    if username not in accounts:
        accounts[username] = {"lastCheckedAt": None, "lastTweetId": None, "lastTweetAt": None}
    return accounts[username]


def add_replied_id(state: dict, tweet_id) -> None:
    """Add tweet_id to state['repliedToIds'] list, deduped, capped at MAX_REPLIED_IDS."""
    tid = str(tweet_id)
    ids = state.setdefault("repliedToIds", [])
    if tid not in ids:
        ids.append(tid)
    if len(ids) > MAX_REPLIED_IDS:
        state["repliedToIds"] = ids[-MAX_REPLIED_IDS:]


# ---------------------------------------------------------------------------
# Peak hours check
# ---------------------------------------------------------------------------


def is_peak_hours(now: datetime | None = None) -> bool:
    """Return True if current UTC time is within US peak hours (13:00-03:00 UTC).

    13:00 UTC = 8am EST  (start of US workday)
    03:00 UTC = 11pm EST (end of US evening)

    Because the window spans midnight UTC we check:
      hour >= 13  OR  hour < 3
    """
    if now is None:
        now = datetime.now(timezone.utc)
    hour = now.hour
    return hour >= PEAK_START_UTC or hour < PEAK_END_UTC


# ---------------------------------------------------------------------------
# Per-account state helpers (backed by kv_state)
# ---------------------------------------------------------------------------


def get_account_kv(conn, username: str) -> dict:
    """Get per-account state dict from kv_state."""
    key = f"{KV_PREFIX}{username.lower()}"
    raw = kv_get(conn, key)
    if not raw:
        return {"lastCheckedAt": None, "lastTweetId": None, "lastTweetAt": None}
    try:
        return json.loads(raw)
    except Exception:
        return {"lastCheckedAt": None, "lastTweetId": None, "lastTweetAt": None}


def set_account_kv(conn, username: str, data: dict) -> None:
    """Save per-account state dict to kv_state."""
    key = f"{KV_PREFIX}{username.lower()}"
    kv_set(conn, key, json.dumps(data))


def load_replied_ids(conn) -> set[str]:
    """Load the set of replied tweet IDs from kv_state."""
    raw = kv_get(conn, KV_REPLIED_IDS)
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except Exception:
        return set()


def save_replied_ids(conn, replied_ids: set[str]) -> None:
    """Save replied tweet IDs to kv_state (capped at MAX_REPLIED_IDS)."""
    lst = list(replied_ids)
    if len(lst) > MAX_REPLIED_IDS:
        lst = lst[-MAX_REPLIED_IDS:]
    kv_set(conn, KV_REPLIED_IDS, json.dumps(lst))


def _persist_replied_id(conn, replied_ids: set[str], tweet_id: str) -> None:
    """Add a tweet ID to the in-memory replied set and persist to DB."""
    replied_ids.add(str(tweet_id))
    save_replied_ids(conn, replied_ids)


# ---------------------------------------------------------------------------
# CDP: scrape latest tweet from a profile page
# ---------------------------------------------------------------------------

# JS to extract the first tweet article from a /with_replies page.
PROFILE_TWEET_JS = r"""(() => {
  const articles = document.querySelectorAll('article[data-testid="tweet"]');
  for (const article of articles) {
    // Find status link containing tweet ID
    const links = Array.from(article.querySelectorAll('a[href*="/status/"]'));
    let tweetId = null;
    let href = null;
    for (const link of links) {
      const m = link.href.match(/\/status\/(\d+)/);
      if (m) { tweetId = m[1]; href = link.href; break; }
    }
    if (!tweetId) continue;

    // Get timestamp from <time> element (Twitter uses ISO datetime attr)
    const timeEl = article.querySelector('time');
    let timestamp = null;
    if (timeEl) {
      timestamp = timeEl.getAttribute('datetime') || timeEl.getAttribute('title') || null;
    }

    // Text content
    const textEl = article.querySelector('[data-testid="tweetText"]');
    const text = textEl ? textEl.textContent.slice(0, 500) : '';

    return JSON.stringify({ tweetId, text, timestamp, href });
  }
  return null;
})()"""


def get_latest_profile_tweet(username: str) -> dict | None:
    """Navigate to user's /with_replies page and extract the latest tweet.

    Returns dict with keys: tweetId, text, timestamp, href
    Returns None on any failure.
    """
    url = f"https://x.com/{username}/with_replies"
    print(f"    CDP: navigating to {url}", flush=True)
    try:
        with cdp_tab() as cdp:
            if not cdp.navigate(url, wait_sec=4):
                print(f"    CDP: navigation failed for @{username}", flush=True)
                return None
            raw = cdp.evaluate(PROFILE_TWEET_JS, timeout=20)
    except Exception as e:
        print(f"    CDP: get_latest_profile_tweet failed for @{username}: {e}", flush=True)
        return None

    if not raw or raw == "null":
        print(f"    CDP: no tweet articles found for @{username}", flush=True)
        return None

    try:
        result = json.loads(raw) if isinstance(raw, str) else raw
        return result
    except (json.JSONDecodeError, TypeError) as e:
        print(f"    CDP: failed to parse tweet JSON for @{username}: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Parse tweet timestamp
# ---------------------------------------------------------------------------


def parse_tweet_timestamp(ts_str: str | None) -> datetime | None:
    """Parse a tweet timestamp from the ISO 8601 format Twitter uses."""
    if not ts_str:
        return None
    try:
        ts_clean = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# LLM: draft targeted reply
# ---------------------------------------------------------------------------


def draft_target_reply(
    username: str,
    tweet_context: dict,
    follower_count: int | None,
    recent_engagements: list[dict],
    recent_posts: list[dict],
) -> dict | None:
    """Draft a reply for a target account tweet via LLM."""
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
        parent_chain_text = (
            f'@{rt.get("username", "?")} said: "{rt.get("text", "")}"'
        )

    other_replies_text = "None visible yet"
    if tweet_context.get("otherReplies"):
        lines = [
            f'  - @{r.get("username", "?")}: "{r.get("text", "")}"'
            for r in tweet_context["otherReplies"]
        ]
        other_replies_text = "\n".join(lines)

    # Thread context
    all_visible_ids = [tweet_context.get("tweetId", "")]
    for p in tweet_context.get("parentChain") or []:
        if p.get("tweetId"):
            all_visible_ids.append(p["tweetId"])
    for t in tweet_context.get("threadContinuation") or []:
        if t.get("id"):
            all_visible_ids.append(t["id"])
    our_thread_note = get_our_thread_context(conn, [i for i in all_visible_ids if i])

    # Author profile section
    author_profile_text = "No profile data available"
    profile = tweet_context.get("authorProfile")
    if profile:
        profile_lines = []
        if profile.get("bio"):
            profile_lines.append(f"**Bio:** {profile['bio']}")
        if profile.get("location"):
            profile_lines.append(f"**Location:** {profile['location']}")
        if profile.get("followersCount"):
            fc = profile['followersCount']
            try:
                fc = int(fc)
                profile_lines.append(f"**Followers:** {fc:,}")
            except (ValueError, TypeError):
                profile_lines.append(f"**Followers:** {fc}")
        if profile.get("recentTweets"):
            profile_lines.append("**Recent tweets (their interests):**")
            for i, t in enumerate(profile["recentTweets"][:5], 1):
                profile_lines.append(f"  {i}. {t[:100]}")
        author_profile_text = (
            "\n".join(profile_lines) if profile_lines else "No profile data"
        )

    follower_note = f"{follower_count:,}" if follower_count else "unknown"
    project_context = load_project_context()

    prompt = f"""You are the voice of @DecentCloud_org on Twitter. Analyze this tweet and decide if/how to engage.

IMPORTANT: This is a TARGET ACCOUNT -- reply quickly and be exceptional. This person has {follower_note} followers. Make the reply worth their attention. We have deliberately curated this account as a high-value target in cloud/infra/DevOps. Your reply will be seen by their entire audience. Only engage if you can say something truly non-obvious and valuable.

# Project & Strategy Context
{project_context}

# Tweet to Analyze
**Author:** @{tweet_context["author"]} ({tweet_context.get("authorName", "")})
**Tweet:** {tweet_context["text"]}
**Stats:** {tweet_context["stats"]["likes"]} likes, {tweet_context["stats"]["retweets"]} RTs, {tweet_context["stats"]["replies"]} replies

# Author Profile
{author_profile_text}

# Full Conversation Context

**Conversation ancestry (what this tweet is replying to):**
{parent_chain_text}

**Author's thread continuation (their own follow-up tweets):**
{json.dumps(tweet_context.get("threadContinuation"), indent=2) if tweet_context.get("threadContinuation") else "None (single tweet or no continuation yet)"}

**Other replies already in this thread:**
{other_replies_text}

**Quoted tweet:**
{json.dumps(tweet_context.get("quotedTweet"), indent=2) if tweet_context.get("quotedTweet") else "None"}

**Our cached thread context:**
{our_thread_note if our_thread_note else "Not part of one of our threads."}

# Our Recent Activity (for voice consistency -- DO NOT repeat these angles)
**Our last 8 replies:**
{recent_our_replies or "  (none yet)"}

**Our recent original posts:**
{recent_our_posts_text or "  (none yet)"}

# Your Task

1. **Read the full conversation carefully.** What is the author ACTUALLY saying?

2. **PRIORITIZE engagement (score 8-10 = must engage):**
   - Cloud/infra pain we can speak to with genuine, specific insight
   - Provider reliability, accountability, or support issues
   - P2P compute, decentralized infra debates
   - Cloud pricing/cost frustration with a specific hook we can add to
   - Author expressing something we can add a genuinely non-obvious angle to

   **GOOD (score 7-8, reply only if truly exceptional):**
   - DevOps philosophy, platform engineering debates
   - GPU availability/cost discussions
   - Infrastructure tradeoffs with a real hook

   **SKIP (score < {CONV_LIKELIHOOD_THRESHOLD} -- remember: the threshold is {CONV_LIKELIHOOD_THRESHOLD}, not 6):**
   - Generic observations everyone already agrees with -- nothing to add
   - Pure engagement-bait with no substance
   - Off-topic (not cloud/infra/compute/marketplace)
   - Already overcrowded thread (>50 replies) where we would be lost
   - We would only be validating ("so true!") -- author won't reply back
   - We have nothing non-obvious to add

3. **Rate conversation likelihood (1-10) -- the KEY metric:**
   Would the author (with {follower_note} followers) actually write back to US specifically?

   **High (8-10):** Author asked a question; made a debatable claim we can push back on; expressed specific unresolved pain; they actively engage in replies
   **Medium (5-7):** We can add a sharp, non-obvious take; might engage if our reply stands out
   **Low (1-4):** Author is broadcasting, not looking for discussion; our reply would purely validate

   **The test:** "Would @{tweet_context["author"]} see our reply and think 'interesting -- I want to respond to that specifically'?"

   REMEMBER: Threshold for posting is conversationLikelihood >= {CONV_LIKELIHOOD_THRESHOLD}. Be conservative. A mediocre reply on a high-profile tweet is worse than silence.

4. **If YES (conv likelihood >= {CONV_LIKELIHOOD_THRESHOLD}), draft an exceptional reply:**

   **Voice (see STRATEGY.md for full style guide):**
   - Observational, not imperative — describe the mechanic, don't tell them what to do
   - Specific details over generic takes: numbers, timeframes, concrete scenarios
   - Peer voice — infrastructure person talking to infrastructure person, not a brand
   - Short sentences. Each one earns its place.
   - NOT: "you should check who's running your workload" — YES: "p2p providers have no accountability layer. that's why ghosting is so common."

   **What works:**
   - Naming the underlying mechanic behind their pain, with specifics they probably don't know
   - A non-obvious angle that makes them think "huh, hadn't considered that"
   - Provider accountability / trust observations — our core thesis, stated as fact not pitch
   - A contrarian take that adds something genuinely new (not what 5 others already said)

   **Hard rules (Phase 1):**
   - Length: 1 word to 2 sentences, under 280 chars. Short is often better — "Agreed.", "Hard disagree.", "Interesting angle." are valid replies when they're a genuine, direct response to what was said.
   - The reply MUST directly answer or follow up on the original post or the latest message in the thread. React to what they actually said.
   - Agreement, disagreement, or any other reaction is fine — what matters is that it's direct and specific.
   - Follow-up: add one when it naturally extends the conversation — "And how would you handle X?", "Does that change when you're multi-region?", "What's the fix?" — only when you have something specific to ask.
   - Vary your openers — check "Our last 8 replies" above and never reuse a word or phrase you opened with recently. Draw from: Agreed. / Fair point. / That tracks. / Exactly. / Disagree. / Hard disagree. / Not quite. / Depends. / Interesting angle. / Worth noting: / The tradeoff is… / The catch is… / Only partially. / Curious — / True, but…
   - NO product mentions (DecentCloud, our product)
   - NO hashtags, NO links
   - NO "check us out", NO "follow for updates"
   - NO imperative framing ("make sure you...", "you should...", "switch to...")
   - Standard sentence capitalization: capitalize the first word and proper nouns (AWS, GCP, Stripe, etc.)
   - profileClickWorthy check: only reply if you're adding a specific fact, a non-obvious angle, or a well-supported pushback. Pure validation ("so true", "exactly", "been there", "this is real") fails this check regardless of conversation likelihood.

# Output Format (JSON)
{{
  "shouldEngage": true/false,
  "conversationLikelihood": 1-10,
  "profileClickWorthy": true/false,
  "reasoning": "brief explanation -- why this merits/doesn't merit a reply",
  "reply": "draft reply text here" (or null if shouldEngage is false)
}}

Output ONLY valid JSON, nothing else.
"""

    try:
        response = call_llm(prompt, timeout=120)
        if not response:
            print("    LLM returned nothing", flush=True)
            return None

        json_str = extract_json(response)
        if json_str is None:
            print("    Could not extract JSON from LLM response, retrying...", flush=True)
            response2 = call_llm(prompt, timeout=120)
            if response2:
                json_str = extract_json(response2)
            if json_str is None:
                return None

        decision = json.loads(json_str)

        conv_score = decision.get("conversationLikelihood", 5)
        profile_click_worthy = decision.get("profileClickWorthy", False)
        if not decision.get("shouldEngage") or conv_score < CONV_LIKELIHOOD_THRESHOLD:
            reason = decision.get("reasoning", "no reason")
            if decision.get("shouldEngage") and conv_score < CONV_LIKELIHOOD_THRESHOLD:
                print(
                    f"    Low conv likelihood ({conv_score}/10, "
                    f"need {CONV_LIKELIHOOD_THRESHOLD}): {reason}",
                    flush=True,
                )
            else:
                print(f"    LLM decided NOT to engage: {reason}", flush=True)
            decision["shouldEngage"] = False
            return decision

        if not profile_click_worthy:
            print(
                f"    Not profile-click-worthy (pure validation), skipping",
                flush=True,
            )
            decision["shouldEngage"] = False
            return decision

        reply = decision.get("reply")
        if not reply or len(reply) > 280:
            print("    LLM reply invalid (empty or too long)", flush=True)
            return None

        return decision

    except Exception as e:
        print(f"    Exception in LLM analysis: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Process a single target account
# ---------------------------------------------------------------------------


def process_account(
    conn,
    username: str,
    replied_ids: set[str],
    recent_engagements: list[dict],
    recent_posts: list[dict],
    now: datetime,
) -> bool:
    """Check a single target account for new tweets and reply if appropriate.

    Returns True if a reply was posted, False otherwise.
    """
    acct = get_account_kv(conn, username)

    # Skip if checked recently
    last_checked = acct.get("lastCheckedAt")
    if last_checked:
        try:
            last_dt = datetime.fromisoformat(last_checked.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            age_min = (now - last_dt).total_seconds() / 60
            if age_min < MIN_CHECK_INTERVAL_MIN:
                print(
                    f"  @{username}: skipped (checked {age_min:.1f} min ago, "
                    f"need {MIN_CHECK_INTERVAL_MIN} min gap)",
                    flush=True,
                )
                return False
        except (ValueError, TypeError):
            pass

    print(f"  @{username}: checking for new tweets...", flush=True)

    # Fetch latest tweet via CDP
    latest = get_latest_profile_tweet(username)
    acct["lastCheckedAt"] = utc_now()

    if not latest:
        print(f"  @{username}: could not fetch profile", flush=True)
        set_account_kv(conn, username, acct)
        return False

    tweet_id = str(latest.get("tweetId", ""))
    if not tweet_id:
        print(f"  @{username}: no tweet ID in CDP result", flush=True)
        set_account_kv(conn, username, acct)
        return False

    last_known_id = acct.get("lastTweetId")

    # Always update so next run has the current ID
    acct["lastTweetId"] = tweet_id
    set_account_kv(conn, username, acct)

    # Skip if same tweet we already know about
    if tweet_id == last_known_id:
        print(f"  @{username}: no new tweet (ID unchanged: {tweet_id})", flush=True)
        return False

    print(
        f"  @{username}: new tweet! {tweet_id} (was: {last_known_id})",
        flush=True,
    )

    # Skip if we already replied to this tweet in a prior run
    if tweet_id in replied_ids:
        print(f"  @{username}: already replied to {tweet_id}", flush=True)
        return False

    # Also check DB
    if is_engaged(conn, tweet_id):
        print(f"  @{username}: already engaged {tweet_id} in DB", flush=True)
        return False

    # Check tweet age -- only reply if tweet is fresh (< MAX_TWEET_AGE_MIN)
    ts_str = latest.get("timestamp")
    tweet_ts = parse_tweet_timestamp(ts_str)
    if tweet_ts:
        age_min = (now - tweet_ts).total_seconds() / 60
        if age_min > MAX_TWEET_AGE_MIN:
            print(
                f"  @{username}: tweet {tweet_id} is {age_min:.1f} min old "
                f"(max {MAX_TWEET_AGE_MIN} min) -- skipping",
                flush=True,
            )
            acct["lastTweetAt"] = tweet_ts.isoformat()
            set_account_kv(conn, username, acct)
            return False
        print(
            f"  @{username}: tweet is {age_min:.1f} min old -- within window",
            flush=True,
        )
        acct["lastTweetAt"] = tweet_ts.isoformat()
        set_account_kv(conn, username, acct)
    else:
        # No parseable timestamp -- skip to avoid replying to stale content
        print(
            f"  @{username}: cannot parse timestamp '{ts_str}' -- skipping (safety)",
            flush=True,
        )
        return False

    # Fetch full tweet context via CDP
    print(f"  @{username}: fetching context for {tweet_id}...", flush=True)
    tweet_context = fetch_tweet_context(tweet_id)
    if not tweet_context:
        print(f"  @{username}: failed to fetch context for {tweet_id}", flush=True)
        return False

    # Enrich with author profile for LLM context
    profile = get_user_profile(username)
    if profile:
        tweet_context["authorProfile"] = profile

    follower_count = profile.get("followersCount") if profile else None

    # LLM: draft and evaluate reply
    print(f"  @{username}: asking LLM to draft reply...", flush=True)
    decision = draft_target_reply(
        username, tweet_context, follower_count, recent_engagements, recent_posts
    )

    if not decision or not decision.get("shouldEngage"):
        return False

    reply_text = decision["reply"]
    conv_score = decision.get("conversationLikelihood", 0)
    print(
        f"  @{username}: LLM approved (conv={conv_score}/10): {reply_text[:80]}...",
        flush=True,
    )
    print(f"  @{username}: reasoning: {decision.get('reasoning', 'N/A')}", flush=True)

    # Humanize the reply
    try:
        reply_text = humanize(reply_text)
    except Exception as e:
        print(f"  @{username}: humanize failed: {e}", flush=True)
        return False

    # Post the reply
    posted, our_reply_id = post_reply(tweet_id, reply_text)
    if not posted:
        send_error_alert(
            f"Target monitor: failed to post reply to {tweet_id} (@{username})"
        )
        return False

    print(f"  @{username}: reply posted successfully!", flush=True)
    if our_reply_id:
        print(f"  Captured ourReplyId: {our_reply_id}", flush=True)

    # Auto-follow after engagement
    auto_follow_after_engagement(conn, username, tweet_id)

    tweet_url = latest.get("href") or f"https://x.com/{username}/status/{tweet_id}"

    # Insert engagement record
    insert_engagement(
        conn,
        tweet_id=tweet_id,
        target_username=username,
        our_reply_text=reply_text,
        our_reply_id=our_reply_id,
        source="target_monitor",
        conv_likelihood=conv_score,
        profile_click_worthy=decision.get("profileClickWorthy"),
        llm_reasoning=decision.get("reasoning"),
    )

    # Add to replied IDs
    _persist_replied_id(conn, replied_ids, tweet_id)

    # Ensure account exists in DB
    upsert_account(conn, username, discovery_source="target", stage="engaged")

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=== TARGET ACCOUNT MONITOR ===", flush=True)
    now = datetime.now(timezone.utc)
    print(f"Time: {now.isoformat()}", flush=True)

    # Gate on peak hours
    if not is_peak_hours(now):
        hour = now.hour
        print(
            f"Outside US peak hours ({hour:02d}:00 UTC). "
            f"Active window: {PEAK_START_UTC:02d}:00-{PEAK_END_UTC:02d}:00 UTC. Exiting.",
            flush=True,
        )
        return 0

    print(
        f"Peak hours confirmed. Monitoring {len(TARGET_ACCOUNTS)} target accounts.",
        flush=True,
    )

    try:
        with get_conn() as conn:
            replied_ids = load_replied_ids(conn)

            # Voice consistency context
            recent_engagements = get_recent_engagements(conn, hours=168, limit=8)
            recent_posts = get_recent_posts(conn, limit=5)

            # Randomize order each run so all accounts get equal coverage over time
            accounts = list(TARGET_ACCOUNTS)
            random.shuffle(accounts)

            replies_posted = 0

            for username in accounts:
                if replies_posted >= MAX_REPLIES_PER_RUN:
                    print(
                        f"Reached max replies per run ({MAX_REPLIES_PER_RUN}). Stopping.",
                        flush=True,
                    )
                    break

                print(f"\nChecking @{username}...", flush=True)

                try:
                    posted = process_account(
                        conn=conn,
                        username=username,
                        replied_ids=replied_ids,
                        recent_engagements=recent_engagements,
                        recent_posts=recent_posts,
                        now=now,
                    )
                    if posted:
                        replies_posted += 1
                        # Update voice context in-memory
                        recent_engagements = [
                            {
                                "target_username": username,
                                "our_reply_text": "",
                                "replied_at": utc_now(),
                            }
                        ] + recent_engagements[:7]
                except Exception as e:
                    print(f"  @{username}: error during processing: {e}", flush=True)
                    continue

                # Small jitter between account checks to look natural
                if replies_posted < MAX_REPLIES_PER_RUN:
                    jitter_sleep(min_sec=3, max_sec=10)

            # Final state save
            kv_set(conn, "target_monitor:last_run", utc_now())

        noun = "reply" if replies_posted == 1 else "replies"
        print(f"\nDone. Posted {replies_posted} target {noun}.", flush=True)
        return 0

    except Exception as e:
        send_error_alert(f"Target monitor failed: {e}")
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
