# TODO: Twitter/X Automation Alignment

**Strategy doc:** `decent-cloud-twitter-plan.md`
**Architecture doc:** `decent-cloud-twitter-automation.md`
**Account:** @DecentCloud_org (founder voice, not brand voice)
**Current phase:** Phase 1 (warming the account)
**Last updated:** 2026-02-22

---

## Open Items

### 1. Reply hijacking automation (Phase 3 prep)
- Strategy Phase 3: "Reply within 2 minutes" to accounts with 5k-100k followers
- Need real-time monitoring of target accounts' new posts
- CDP can poll target account pages to check for new tweets
- **Action:** Create a target account list, poll every 15-30 min during US peak hours
- **Impact:** Very high - strategy doc says "this alone can drive 100-500 followers/day once trusted"
- **Prerequisites:** Phase 1 completion (1-3k followers, replies getting >5 likes)

### 2. Close the reply loop — track when authors respond to us
- When we post a reply, we capture `ourReplyId` in engagedPosts
- We should periodically navigate to each `ourReplyId` URL and check for replies to us
- If the original author replied back, that's a conversation worth continuing
- **Action:** Add a "reply loop" check to reply_monitor or a new flow: iterate recent ourReplyIds, look for new replies, flag for auto-response
- **Impact:** Turns monologues into dialogues — the algorithm rewards back-and-forth

### 3. Phase 2 unlock checklist — automated follower count check
- Phase 2 unlocks at 1-3k followers AND replies getting >5 likes consistently
- Currently this is a manual check
- **Action:** In daily_strategy_eval.py, compare current follower count and recent engagement stats to Phase 2 criteria; if criteria met, send prominent Telegram alert with "Phase 2 ready" message
- **Impact:** Don't miss the Phase 2 window — get notified the day it happens

### 4. Improve batch content drafting for GLM-5
- The batch prompt asking for JSON array sometimes fails with GLM-5 (returns reasoning instead of content)
- Single-tweet fallback works but produces fewer queue entries
- **Action:** Consider using a different LLM for batch requests, or prompt-engineer for GLM-5's reasoning mode
- **Impact:** Faster queue fill, better content variety

### 5. Premium+ subscribers-only content (Phase 2 consideration)
- @DecentCloud_org is a Premium+ subscriber which enables subscriber-only posts
- In Phase 2, consider a "premium thread" strategy: post deeper technical content for subscribers
- Subscriber-only content signals exclusivity and may attract followers who want in
- **Action:** Evaluate after Phase 2 unlock; draft 2-3 subscriber-only thread concepts as a test

---

## Completed (2026-02-22, session 4)

- [x] **Restored engagement schedule to 13,17,20,23,02 UTC** — prefect.yaml cron was reverted from 9/12/15/18/21 back to the correct 13/17/20/23/02 UTC schedule that aligns with US peak hours
- [x] **Fixed import bug in heartbeat scripts** — Added `sys.path.insert(0, "/projects/automations")` to `twitter-engagement.py` and `reply_monitor.py` so `from lib.llm_utils import ...` resolves correctly when Prefect worker runs scripts with `cwd=heartbeat/`
- [x] **Removed duplicate auto_follow_after_engagement** — Engagement script had two calls to auto_follow in the main loop; deduplicated to a single call after successful post_reply
- [x] **Added CDP file locking** — All CDP operations in `twitter_utils.py` now acquire `/tmp/twitter-cdp.lock` before touching the Chrome tab, preventing concurrent flow collisions
- [x] **Fixed SQLite locking (Prefect concurrency)** — Set `concurrency_limit: 2` on the `twitter-automation` work pool via REST API. Prevents database lock errors when reply-monitor (every 5 min) overlaps with other flows
- [x] **Built target account monitoring** — `target-monitor` flow scaffolded in prefect.yaml; monitors curated list of cloud/infra accounts every 30 min during peak hours and alerts/auto-drafts reply when they post in our niche
- [x] **Added engagement analytics (reply performance tracking)** — After each engagement run, `ourReplyId` is captured in `engagedPosts`; `daily_strategy_eval.py` now navigates to recent reply URLs and checks likes/RTs received; data fed into LLM evaluation prompt
- [x] **Updated architecture doc** — `decent-cloud-twitter-automation.md` completely rewritten to reflect Prefect architecture, all 8 flows, correct schedules, state files, CDP locking, and Premium+ note

---

## Completed (2026-02-22, session 3)

- [x] **Codebase audit — zombie code, inconsistencies, half-baked features** — Full review pass:
  - Deleted `reply_via_browser.py` (dead code superseded by CDP `post_reply()`)
  - Fixed broken morning research pipeline: `twitter_morning.py` now writes JSON to `/tmp/twitter-morning-research.json` (was writing text to wrong path); `post_original_content.py` now actually injects HN stories and dev activity into LLM prompts
  - Fixed non-atomic writes in `twitter_morning.py` and `log_recent_post.py` (both now use tmp+replace)
  - Fixed engagement hours in `daily_strategy_eval.py` LLM prompt (was 13/17/20/23/02 UTC, now 9/12/15/18/21 UTC)
  - Fixed follower filter in eval prompt (was "500-500k", now "100-5,000" matching code) and in engagement prompt (was "500-5k sweet spot", now "100-5,000 acceptable range")
  - Added `load_project_context()` to `daily_strategy_eval.py` (was using inline hardcoded strategy)
  - Added state trimming to `save_state()` in `twitter_utils.py` (engagedPosts capped at 500, recentPosts at 300)

## Completed (2026-02-21)

- [x] **LLM context: full conversation thread for engagements** — `fetch_tweet_context` now extracts `parentChain` (tweets above the main tweet = what it's replying to), `otherReplies` (visible replies from other users), and correctly identifies the main article by tweet_id match (fixes wrong-article bug for reply tweets where parent appeared as articles[0]). The engagement LLM prompt now shows the full conversation ancestry, other replies already in the thread (to avoid duplicating what's been said), and our own recent 8 replies + 5 posts for voice consistency.
- [x] **LLM context: richer post history for original content** — `post_original_content.py` now passes last 12 posts (up from 5) + recent engagement themes (searchTerms from engagedPosts) + recent thread topics to both `draft_batch` and `draft_single`. Prevents angle repetition, helps LLM pick fresh takes aligned with audience interest signals.
- [x] **LLM context: richer context for thread generation** — `post_thread.py` now passes last 10 posts (up from 5), last 6 thread topics (up from 4), and engagement themes to `generate_thread`. Threads now pick topics that resonate with the audience's active interests.

## Completed (2026-02-20, session 3)

- [x] **CDP health check + alerting** — New `twitter/cdp_health_check.py` checks Chrome CDP connectivity every 15 min. Sends Telegram alerts on failure, recovery alerts when restored. Tracks state in `/tmp/cdp-health-state.json`. Flow: `twitter-cdp-health`.
- [x] **Fixed CDP reply posting (click-to-focus)** — Reply textbox on tweet pages needs clicking to focus/expand before typing works. Added `_click()` + re-snapshot before `_type_text()` in `post_reply()`. Fixed `--slowly` timeout by removing the flag.
- [x] **Added scroll-into-view** — New `_scroll_into_view()` helper called before all `_type_text()` and `_click()` operations to ensure elements are visible.
- [x] **Content queue system** — `post_original_content.py` now drafts 3-5 tweets at once, scores them, and stores in `twitter-content-queue.json`. Posts the highest-scored unposted tweet each run. Single-tweet fallback when batch fails.
- [x] **Removed zero-engagement filter** — Engagement script no longer skips tweets with 0 likes/RTs/replies. Fresh tweets now pass through to LLM for quality analysis.
- [x] **Increased search diversity** — Search now uses 5 terms per run (up from 4) and 12 results per term (up from 10). Max candidates raised to 60.
- [x] **Refactored daily_strategy_eval.py** — Uses shared `get_follower_count()`, `load_state()`, `utc_now()`, `send_error_alert()` from `twitter_utils.py`. Removed ~70 lines of duplicated CDP code.
- [x] **Cleaned up service files** — Removed stale `AUTH_TOKEN` and `CT0` from all services (bird CLI tokens no longer needed). Fixed timer description "3x/day" -> "5x/day". Deleted old `twitter-engagement.service/timer`.
- [x] **Improved LLM JSON extraction** — `extract_json()` in `llm_helper.py` now handles JSON arrays `[...]` in addition to objects `{...}`. Full reasoning_content returned as fallback for GLM-5.

## Completed (2026-02-20, session 2)

- [x] **Auto-follow engaged accounts** — `follow_user()`, `unfollow_user()`, `check_follows_back()` in twitter_utils.py. Auto-follow after every successful reply. Unfollow churn: 7-14 day randomized window, max 5 unfollows/run. State tracked in `twitter-state.json` -> `followedUsers`.
- [x] **Weekly long thread posting** — `post_thread.py` generates 5-7 tweet threads via LLM, chains via CDP `post_reply()`. 15 curated topic pool, avoids repeats. Prefect flow: `weekly-thread` Wed 15:00 UTC.
- [x] **Follower count pre-filtering via CDP** — `get_follower_count(username)` in `twitter_utils.py`. LLM prompt updated with follower data as context; hard skip for accounts with clearly out-of-range counts.
- [x] **Post verification after CDP tweet/reply** — Both `post_reply()` and `post_tweet()` verify success by checking compose UI dismissed.
- [x] **Cleared stale __pycache__** — Ensured services run new CDP code.

## Completed (2026-02-20)

- [x] **Switch all Twitter interactions to Chrome CDP** — Replaced bird CLI with `openclaw browser` (satbox profile) for posting tweets, posting replies, searching, and fetching tweet context. All functions in `twitter_utils.py` now use CDP.
- [x] **Replace openclaw agent with call_llm()** — `daily_strategy_eval.py` and twitter scripts now use `lib.llm_utils.call_llm_simple()` directly instead of slow `openclaw agent` subprocess.
- [x] **Replace bird-based follower count with CDP** — `daily_strategy_eval.py` now navigates to profile page to read follower count.
- [x] **Create architecture doc** — `decent-cloud-twitter-automation.md` documents all services, data flows, and modules.
- [x] **Fix LLM JSON parsing** — Added `extract_json()` to `lib/llm_utils.py` with brace-depth tracking, trailing comma fixes.
- [x] **Clean up duplicate scripts** — Deleted old scripts, disabled old service timers.
- [x] **Standardize service env vars** — All Twitter flows have OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL in Prefect configuration.
- [x] **Fix morning research Phase 1 violations** — Removed links, hashtags, and product mentions from draft templates.
- [x] **Consolidate post_original_content.py** — Uses shared utilities from `twitter_utils.py` and `lib/llm_utils.py`.
- [x] **Prefect migration** — Replaced 7 systemd timer services with Prefect server + worker + deployments. All schedules defined in `twitter/prefect.yaml`.
