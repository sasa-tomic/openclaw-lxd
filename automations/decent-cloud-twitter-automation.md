# Decent Cloud Twitter Automation Architecture

**Last updated:** 2026-02-22
**Strategy doc:** `decent-cloud-twitter-plan.md`
**Account:** @DecentCloud_org

---

## Overview

All Twitter interactions use **Chrome CDP** via `openclaw browser` (satbox profile).
No bird CLI. No direct API calls. Everything goes through the logged-in browser session.

Scheduling and orchestration is handled by **Prefect** (replaces old systemd timers).
The Prefect server and worker run as systemd user services and are always on.

---

## Architecture: Prefect Flows

| Flow name | Script | Schedule (UTC) | Purpose |
|-----------|--------|----------------|---------|
| `engagement` | `heartbeat/twitter-engagement.py` | 13:00, 17:00, 20:00, 23:00, 02:00 | Search + reply to 8 tweets/run (5x/day = ~40/day) |
| `original-content` | `twitter/post_original_content.py` | 10:30, 16:30 | Post 2 original takes/day (from content queue) |
| `weekly-thread` | `twitter/post_thread.py` | Wed 15:00 | Post 1 deep technical thread/week (5-7 tweets) |
| `daily-eval` | `twitter/daily_strategy_eval.py` | 07:00 | Gather metrics, LLM evaluation, Telegram report |
| `morning-research` | `heartbeat/twitter_morning.py` | 08:00 | HN research for content ideas |
| `cdp-health` | `twitter/cdp_health_check.py` | Every 15 min | CDP connectivity check + Telegram alerting |
| `reply-monitor` | `heartbeat/reply_monitor.py` | Every 5 min | Monitor mentions/replies, auto-respond |
| `target-monitor` | *(in development)* | Every 30 min peak hours | Fast-reply to curated target accounts |

### Prefect Infrastructure

```
systemd user services:
  twitter-prefect-server.service   -> prefect server start --host 0.0.0.0
  twitter-prefect-worker.service   -> prefect worker start --pool twitter-automation

Work pool: twitter-automation
  concurrency_limit: 2   (prevents SQLite locking under high concurrency)

Deploy with:
  cd /projects/automations/twitter && uv run prefect deploy --all

UI:
  http://localhost:4200
```

All flows run via `twitter/twitter_scheduler.py` which uses `subprocess.Popen` to launch
each script, streaming stdout/stderr into Prefect's log system. Scripts run with
`cwd=script_path.parent` so relative imports work correctly.

---

## Core Modules

### `twitter/twitter_utils.py` — Shared Utilities

The single source of truth for all Twitter interactions.

**CDP Functions:**
- `post_tweet(text)` — Navigate to compose, type, click Post (with post verification)
- `post_reply(tweet_id, reply_text)` — Navigate to tweet, click-focus reply box, type, click Reply (with post verification)
- `get_latest_own_tweet_id(username)` — Navigate to profile Replies tab, extract latest tweet ID (for thread chaining)
- `search_candidates(terms, limit)` — Search via Twitter web, extract results via JS (5 terms, 12 results/term)
- `fetch_tweet_context(tweet_id)` — Navigate to tweet, extract text/stats/thread/parentChain/otherReplies via JS
- `get_user_profile(username)` — Navigate to profile, extract bio, followers, recent tweets (with in-process cache)
- `get_follower_count(username)` — Navigate to user profile, extract follower count (handles K/M suffixes)
- `follow_user(username)` — Navigate to profile, click Follow (verifies button changes to "Following")
- `unfollow_user(username)` — Navigate to profile, click Following -> confirm Unfollow
- `check_follows_back(username)` — Check for "Follows you" badge on profile
- `auto_follow_after_engagement(state, author, tweet_id)` — Auto-follow after each successful reply

**CDP Helpers:**
- `_get_target_id()` — Get first browser tab target ID
- `_navigate_and_wait(url, target_id)` — Navigate and wait for page load
- `_snapshot(target_id)` — Take efficient page snapshot
- `_find_ref(snapshot, pattern)` — Find element ref in snapshot
- `_scroll_into_view(target_id, ref)` — Scroll element into viewport
- `_type_text(target_id, ref, text)` — Type text into element (with scroll-into-view)
- `_click(target_id, ref)` — Click element (with scroll-into-view)
- `_evaluate(target_id, js)` — Run JavaScript on page

**CDP File Locking:**
All CDP operations acquire a file lock (`/tmp/twitter-cdp.lock`) to prevent concurrent
scripts from fighting over the same Chrome tab. Lock is held for the duration of each
CDP action and released immediately after.

**Helper Functions:**
- `humanize(text)` — Run through humanize.py for natural voice
- `jitter_sleep()` — Random delay between actions
- `load_state() / save_state()` — Twitter state management (with trimming: engagedPosts capped at 500, recentPosts at 300)
- `load_project_context()` — Load strategy doc for LLM prompts
- `send_error_alert()` — Telegram notifications
- `get_engaged_ids(state)` — Fast set of already-engaged tweet IDs
- `get_our_tweet_ids()` — Set of our own tweet IDs (for thread detection)
- `log_recent(type, description, url, tweet_id)` — Log to Obsidian notes
- `lookup_our_thread(tweet_ids)` — Check if any IDs match our own thread posts
- `utc_now()` — Return UTC ISO timestamp string

### `lib/llm_utils.py` — LLM Interface (Shared Library)

Located at `/projects/automations/lib/llm_utils.py`. Imported via:

```python
sys.path.insert(0, "/projects/automations")
from lib.llm_utils import call_llm_simple, extract_json
```

- `call_llm_simple(prompt, timeout)` — OpenAI-compatible API call (GLM-5 default)
- `extract_json(text)` — Robust JSON extraction from LLM output (handles objects `{}` and arrays `[]`, trailing commas, reasoning wrappers)

Note: `twitter/llm_helper.py` is a legacy alias that re-exports these functions for
backwards compatibility with older twitter-dir scripts.

### `twitter/cdp_health_check.py` — CDP Health Monitor

- Checks socat-proxy service status
- Checks Chrome CDP HTTP endpoint at `localhost:9222`
- Sends Telegram alerts on failure (with dedup — one alert per incident)
- Sends recovery alerts when Chrome comes back
- State tracked in `/tmp/cdp-health-state.json`

---

## Content Queue System

Original content posting uses a quality-gated queue (`twitter-content-queue.json`):

1. **Batch drafting**: LLM generates 3-5 tweet options as JSON array
2. **Quality scoring**: Each tweet scored 0.0-1.0 based on specificity, brevity, opinion markers
3. **Selection**: Highest-scored unposted tweet is picked each run
4. **Fallback**: If batch drafting fails (GLM-5 limitations), single-tweet prompt used
5. **Cleanup**: Posted entries removed after 7 days

---

## Data Flow

```
Morning Research (08:00 UTC)
    -> Search HN for cloud/infra stories
    -> Cache tweet ideas to /tmp/twitter-morning-research.json

Daily Eval (07:00 UTC)
    -> Gather follower count (shared CDP function), engagement stats
    -> Load project context from strategy doc
    -> LLM analysis -> Telegram report
    -> Append to twitter-eval-history.json

CDP Health Check (every 15 min)
    -> Check socat-proxy + Chrome CDP endpoint at localhost:9222
    -> Alert via Telegram on failure/recovery
    -> State in /tmp/cdp-health-state.json

Engagement Runs (13:00, 17:00, 20:00, 23:00, 02:00 UTC)
    -> Dynamic keyword generation from recent successful engagements
    -> CDP search with SEARCH_TERMS (12 results/term, up to 60 candidates)
    -> Filter: junk/bots/blocked authors/already-engaged IDs
    -> Fetch tweet context (full parentChain + otherReplies for conversation awareness)
    -> Fetch author profile (bio, followers, recent tweets) for LLM context
    -> LLM analyze with full context: conversation thread, our recent 8 replies, voice samples
    -> LLM must score conversationLikelihood >= 6 to proceed
    -> Humanize approved replies
    -> CDP click-focus reply box -> type -> click Reply (with post verification)
    -> Auto-follow author via CDP (tracked in followedUsers)
    -> Capture ourReplyId for future engagement tracking
    -> Log to twitter-state.json engagedPosts array
    -> Process unfollow queue: 7-14 day window, max 5 unfollows/run

Reply Monitor (every 5 min)
    -> CDP navigate to x.com/notifications/mentions
    -> Extract new mention tweet IDs
    -> Filter: already-seen, already-engaged, blocked, own account
    -> For each new mention (cap 5/run):
        -> fetch_tweet_context -> detect if direct reply to our tweet
        -> Draft context-aware reply via LLM (mention-framing vs cold-engagement)
        -> Humanize -> jitter_sleep(5-30s) -> post_reply
        -> Auto-follow + log + save_state
    -> Final save_state

Original Content (10:30, 16:30 UTC)
    -> Check content queue (twitter-content-queue.json)
    -> If <2 unposted: LLM batch draft 3-5 tweets -> score -> queue
    -> Inject HN research + dev activity + last 12 posts + engagement themes into prompt
    -> Pick highest-scored unposted tweet
    -> Humanize -> CDP post tweet -> Log to twitter-state.json

Weekly Thread (Wed 15:00 UTC)
    -> LLM generate 5-7 tweet thread
    -> Context: last 10 posts, last 6 thread topics, engagement themes
    -> Humanize each tweet
    -> CDP post first tweet -> get ID from profile
    -> Chain remaining tweets as replies (30-90s jitter between tweets)
    -> Log thread to twitter-state.json (threads array) + twitter-thread-index.json
```

---

## State Files

| File | Purpose |
|------|---------|
| `~/clawd/memory/twitter-state.json` | Engaged posts, recent posts, LLM cache, followed users |
| `~/clawd/memory/twitter-eval-history.json` | Daily evaluation metrics history |
| `~/clawd/memory/twitter-content-queue.json` | Content queue (drafted tweets with scores) |
| `~/clawd/memory/twitter-thread-index.json` | Index of our own thread tweet IDs (for thread detection) |
| `~/clawd/memory/twitter-profile-cache.json` | Cached author profiles (bio, followers, recent tweets) |
| `~/clawd/memory/twitter-search-cache.json` | Search result cache to avoid redundant CDP searches |
| `~/clawd/memory/twitter-approved-engagement-queue.json` | Pre-approved replies waiting to be posted |
| `/tmp/cdp-health-state.json` | CDP health check state (last status, down-since timestamp) |
| `/tmp/twitter-morning-research.json` | Morning HN research cache (daily, in /tmp) |

---

## Browser Profile & Infrastructure

- **Profile:** `satbox` (always-running Chrome with X login)
- **CDP access:** via `openclaw browser --browser-profile satbox`
- **Auth:** Cookies in browser session (no separate auth tokens needed)
- **Chrome host:** Remote machine at `192.168.0.13:9222`
- **Proxy:** `socat-proxy.service` on automation host (10.3.40.31) forwards local port 9222 -> remote Chrome
- **File lock:** `/tmp/twitter-cdp.lock` prevents concurrent CDP tab conflicts
- **Health check:** `cdp-health` Prefect flow every 15 min with Telegram alerting
- **Known issue:** Chrome occasionally freezes (TCP accepts connections but HTTP hangs). Requires manual restart on the physical satbox machine. SSH access to 192.168.0.13 is not available from the automation host.

---

## Auto-Follow / Unfollow Churn

After each successful reply, the engagement script auto-follows the author via CDP.
Follows are tracked in `twitter-state.json` -> `followedUsers`.

**Unfollow churn logic** (processed at the start of each engagement run):
- Days 0-7: Keep following (give them time to follow back)
- Days 7-14: Randomized unfollow chance (increases linearly with age)
- Day 14+: Definite unfollow if no follow-back
- Before unfollowing, checks `check_follows_back()` — users who follow back are kept permanently
- Max 5 unfollows per run to look natural

---

## Phase 1 Rules (Current)

- NO links in posts/replies
- NO product mentions (Decent Cloud, our platform, etc.)
- NO hashtags
- Founder voice: opinionated, technical, slightly arrogant but correct
- Target follower range: 100-5,000 (LLM guided; accounts outside this range deprioritized)
- Phase 2 unlock: 1-3k followers AND replies consistently getting >5 likes

### Premium+ Account Note

The @DecentCloud_org account is a **Twitter Premium+ subscriber**. This enables:
- Longer posts (up to 25,000 chars for threads, though we keep replies under 280)
- Higher algorithmic distribution for original content
- Subscriber-only content options (reserved for Phase 2 strategy)

---

## Engagement Filtering Logic

Candidates go through multiple layers before LLM analysis:

1. **Search**: CDP search across SEARCH_TERMS (dynamically generated from recent successful engagements + static fallback list)
2. **Blocked authors**: Skip accounts in BLOCKED_AUTHORS list
3. **Already-engaged**: Skip tweet IDs already in engagedPosts
4. **LLM analysis**: Full context analysis with conversationLikelihood score
   - Requires `shouldEngage: true` AND `conversationLikelihood >= 6`
   - LLM given author profile (bio, followers, recent tweets) for context
   - LLM given full parentChain + otherReplies for conversation awareness
   - LLM given our own recent 8 replies + 5 posts for voice consistency
5. **Humanize**: Natural voice pass before posting

Note: Hard follower count cutoffs (`get_follower_count()`) are available but currently
the LLM receives follower data as context and makes the call rather than hard-filtering.
