#!/usr/bin/env python3
"""Account discovery pipeline for @DecentCloud_org.

Lifecycle per run:
  Phase 0 — Score unscored candidates (seeds + anything already in DB as 'candidate')
  Phase 1 — DISABLED: follow accounts manually from browser instead
  Phase 2 — Gather new candidates from social graph of followed accounts
  Phase 3 — Fetch profiles of new candidates (follower range filter)
  Phase 4 — LLM batch-score new candidates, upsert as 'scored'

Stage progression:
  candidate -> scored -> followed -> engaged -> warm -> follower -> blocked
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.llm_utils import call_llm, extract_json
from twitter_utils import (
    BLOCKED_AUTHORS,
    TWITTER_BASE_URL,
    _evaluate,
    _get_target_id,
    _navigate_and_wait,
    cdp_lock,
    fetch_user_profile,
    follow_user,
    jitter_sleep,
    send_error_alert,
    utc_now,
)
from db import (
    get_conn,
    get_accounts_by_stage,
    get_all_followed_usernames,
    get_candidates_from_edges,
    get_top_scored_candidates,
    set_followed,
    upsert_account,
    upsert_edge,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Max candidates to score per run
MAX_SCORE_PER_RUN = 20

# Max accounts to scrape their social graph per run (CDP is slow)
MAX_SCRAPE_ACCOUNTS = 5

# Follower range for candidates — wider than engagement filter, tighter on discovery
MIN_FOLLOWERS = 200
MAX_FOLLOWERS = 50_000

# Max daily follows — stay well under Twitter's 400/day hard limit
DAILY_FOLLOW_BUDGET = 25

# Minimum LLM score to follow (0.0–1.0)
FOLLOW_SCORE_THRESHOLD = 0.65

# Stages considered "in the funnel already" — skip as candidates
FUNNEL_STAGES = frozenset(("scored", "followed", "engaged", "warm", "follower", "blocked"))


# ---------------------------------------------------------------------------
# CDP helpers for social graph scraping
# ---------------------------------------------------------------------------

_FOLLOWING_JS = """
(function() {
  var anchors = document.querySelectorAll('a[role="link"][href^="/"]');
  var seen = {};
  var results = [];
  anchors.forEach(function(a) {
    var href = a.getAttribute('href') || '';
    var parts = href.split('/').filter(Boolean);
    if (parts.length !== 1) return;
    var u = parts[0].toLowerCase();
    var reserved = ['i','search','explore','notifications','messages','home',
                    'settings','login','logout','intent'];
    if (reserved.indexOf(u) !== -1) return;
    if (seen[u]) return;
    seen[u] = true;
    results.push(u);
  });
  return JSON.stringify(results.slice(0, 100));
})()
"""

_REPLY_TARGETS_JS = """
(function() {
  var seen = {};
  var results = [];
  var articles = document.querySelectorAll('article[data-testid="tweet"]');
  articles.forEach(function(a) {
    var text = a.textContent || '';
    var matches = text.match(/@([A-Za-z0-9_]+)/g) || [];
    matches.forEach(function(m) {
      var u = m.slice(1).toLowerCase();
      if (u && u.length >= 2 && !seen[u]) {
        seen[u] = true;
        results.push(u);
      }
    });
  });
  return JSON.stringify(results.slice(0, 100));
})()
"""


def _scrape_following(username: str) -> list[str]:
    """Return usernames from the /following page of an account."""
    try:
        with cdp_lock():
            target_id = _get_target_id()
            if not target_id:
                return []
            url = f"{TWITTER_BASE_URL}/{username}/following"
            if not _navigate_and_wait(url, target_id, wait_sec=4):
                return []
            time.sleep(2)
            for _ in range(3):
                _evaluate(target_id, "window.scrollBy(0, 800)")
                time.sleep(1.2)
            raw = _evaluate(target_id, _FOLLOWING_JS, timeout=15)
            if not raw:
                return []
            result = json.loads(raw)
            return [u for u in result if isinstance(u, str) and u]
    except Exception as e:
        print(f"  CDP scrape following @{username} failed: {e}", file=sys.stderr, flush=True)
        return []


def _scrape_reply_targets(username: str) -> list[str]:
    """Return @handles that an account replies to (from /with_replies feed)."""
    try:
        with cdp_lock():
            target_id = _get_target_id()
            if not target_id:
                return []
            url = f"{TWITTER_BASE_URL}/{username}/with_replies"
            if not _navigate_and_wait(url, target_id, wait_sec=4):
                return []
            time.sleep(2)
            for _ in range(4):
                _evaluate(target_id, "window.scrollBy(0, 800)")
                time.sleep(1.0)
            raw = _evaluate(target_id, _REPLY_TARGETS_JS, timeout=15)
            if not raw:
                return []
            result = json.loads(raw)
            return [u for u in result if isinstance(u, str) and u]
    except Exception as e:
        print(f"  CDP scrape reply targets @{username} failed: {e}", file=sys.stderr, flush=True)
        return []


# ---------------------------------------------------------------------------
# Phase 0 — Score unscored candidates
# ---------------------------------------------------------------------------


def score_unscored_candidates(conn) -> int:
    """Fetch profiles and LLM-score all accounts in stage='candidate'.

    Seeds start as 'candidate'. This phase moves them to 'scored' so Phase 1
    can then follow the best ones.

    Returns count of candidates scored.
    """
    candidates = get_accounts_by_stage(conn, "candidate", limit=MAX_SCORE_PER_RUN)
    if not candidates:
        print("  No unscored candidates.", flush=True)
        return 0

    print(f"  Scoring {len(candidates)} candidate(s)...", flush=True)

    # Fetch profiles via CDP
    profiles: list[dict] = []
    for acct in candidates:
        username = acct["username"]
        # If we already have bio in DB, skip profile fetch
        if acct.get("bio") and acct.get("follower_count"):
            profiles.append({
                "username": username,
                "display_name": acct.get("display_name", ""),
                "bio": acct.get("bio", ""),
                "follower_count": acct.get("follower_count", 0),
                "following_count": acct.get("following_count", 0),
            })
            continue

        try:
            profile = fetch_user_profile(username)
            if not profile:
                print(f"  No profile for @{username} — skipping", flush=True)
                continue
            fc = profile.get("followersCount") or 0
            profiles.append({
                "username": username,
                "display_name": profile.get("displayName", ""),
                "bio": profile.get("bio", ""),
                "follower_count": fc,
                "following_count": profile.get("followingCount", 0),
            })
            print(f"  Profile @{username}: {fc} followers", flush=True)
            jitter_sleep(2, 5)
        except Exception as e:
            print(f"  Profile error @{username}: {e}", file=sys.stderr, flush=True)

    if not profiles:
        return 0

    scored = _llm_batch_score(profiles)
    scored_map = {s["username"]: s for s in scored}

    count = 0
    for profile in profiles:
        username = profile["username"]
        score_data = scored_map.get(username, {"relevance_score": 0.5, "relevance_notes": "no score"})
        upsert_account(
            conn,
            username=username,
            display_name=profile.get("display_name"),
            bio=profile.get("bio"),
            follower_count=profile.get("follower_count"),
            following_count=profile.get("following_count"),
            relevance_score=score_data["relevance_score"],
            relevance_notes=score_data.get("relevance_notes"),
            stage="scored",
        )
        print(
            f"  Scored @{username}: {score_data['relevance_score']:.2f} — {score_data.get('relevance_notes','')[:60]}",
            flush=True,
        )
        count += 1

    return count


# ---------------------------------------------------------------------------
# Phase 1 — Follow top-scored candidates
# ---------------------------------------------------------------------------


def _count_follows_today(conn) -> int:
    """Count follows performed today (UTC)."""
    today = datetime.now(timezone.utc).date().isoformat()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM accounts WHERE followed_at::date = %s",
            (today,),
        )
        return cur.fetchone()[0]


def follow_top_candidates(conn) -> int:
    """Follow accounts in stage='scored' with score >= threshold.

    Respects daily budget. Returns count of new follows.
    """
    already_today = _count_follows_today(conn)
    budget = DAILY_FOLLOW_BUDGET - already_today
    if budget <= 0:
        print(f"  Daily follow budget exhausted ({already_today}/{DAILY_FOLLOW_BUDGET})", flush=True)
        return 0

    print(f"  Follow budget remaining: {budget}/{DAILY_FOLLOW_BUDGET}", flush=True)
    top = get_top_scored_candidates(conn, limit=budget * 2)
    if not top:
        print("  No scored candidates to follow.", flush=True)
        return 0

    followed = 0
    for acct in top:
        if followed >= budget:
            break
        username = acct["username"]
        score = acct.get("relevance_score", 0)
        if score < FOLLOW_SCORE_THRESHOLD:
            print(f"  @{username} score {score:.2f} below threshold {FOLLOW_SCORE_THRESHOLD} — stopping", flush=True)
            break

        print(f"  Following @{username} (score={score:.2f})...", flush=True)
        if follow_user(username):
            set_followed(conn, username)
            followed += 1
            print(f"  Followed @{username}", flush=True)
            jitter_sleep(8, 20)
        else:
            print(f"  Follow failed for @{username}", flush=True)
            jitter_sleep(3, 8)

    return followed


# ---------------------------------------------------------------------------
# Phase 2 — Gather new candidates from social graph
# ---------------------------------------------------------------------------


def gather_new_candidates(conn) -> list[str]:
    """Scrape following lists and reply targets of followed accounts.

    Returns deduplicated list of usernames not yet in the accounts table.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT username FROM accounts")
        all_known = {row[0].lower() for row in cur.fetchall()}

    # Scrape from both 'followed' and more engaged stages
    scrape_stages = ("followed", "engaged", "warm", "follower")
    to_scrape: list[dict] = []
    for stage in scrape_stages:
        to_scrape.extend(get_accounts_by_stage(conn, stage, limit=10))

    to_scrape = to_scrape[:MAX_SCRAPE_ACCOUNTS]
    if not to_scrape:
        print("  No followed accounts to scrape from yet.", flush=True)
        return []

    print(f"  Scraping social graph of {len(to_scrape)} account(s)...", flush=True)
    candidates: set[str] = set()

    blocked_lower = {a.lower() for a in BLOCKED_AUTHORS}

    for acct in to_scrape:
        username = acct["username"]

        following = _scrape_following(username)
        print(f"  @{username}/following: {len(following)} accounts", flush=True)
        for u in following:
            if u not in all_known and u not in blocked_lower:
                candidates.add(u)
                upsert_edge(conn, source=username.lower(), target=u, type="follows")
        jitter_sleep(5, 12)

        reply_targets = _scrape_reply_targets(username)
        print(f"  @{username}/with_replies: {len(reply_targets)} reply targets", flush=True)
        for u in reply_targets:
            if u not in all_known and u not in blocked_lower:
                candidates.add(u)
                upsert_edge(conn, source=username.lower(), target=u, type="replies_to")
        jitter_sleep(5, 12)

    # Also surface candidates from existing edges not yet in accounts table
    warm_usernames = [a["username"].lower() for a in to_scrape]
    edge_candidates = get_candidates_from_edges(conn, sources=warm_usernames, limit=100)
    for u in edge_candidates:
        if u not in all_known and u not in blocked_lower:
            candidates.add(u)

    print(f"  Total new candidate handles: {len(candidates)}", flush=True)
    return list(candidates)[:MAX_SCORE_PER_RUN]


# ---------------------------------------------------------------------------
# Phases 3+4 — Fetch profiles and score new candidates
# ---------------------------------------------------------------------------


def fetch_and_score_new_candidates(conn, usernames: list[str]) -> int:
    """Fetch profiles for new candidate usernames and score them.

    Upserts into accounts as stage='scored'. Returns count upserted.
    """
    profiles: list[dict] = []
    for username in usernames:
        try:
            profile = fetch_user_profile(username)
            if not profile:
                continue
            fc = profile.get("followersCount") or 0
            if fc < MIN_FOLLOWERS or fc > MAX_FOLLOWERS:
                print(f"  @{username}: {fc} followers — out of range, skipping", flush=True)
                upsert_account(
                    conn,
                    username=username,
                    follower_count=fc,
                    stage="scored",
                    relevance_score=0.0,
                    relevance_notes=f"follower count {fc} outside range",
                    discovery_source="social_graph",
                )
                continue
            profiles.append({
                "username": username,
                "display_name": profile.get("displayName", ""),
                "bio": profile.get("bio", ""),
                "follower_count": fc,
                "following_count": profile.get("followingCount", 0),
            })
            print(f"  Profile @{username}: {fc} followers | {profile.get('bio','')[:60]}", flush=True)
            jitter_sleep(2, 5)
        except Exception as e:
            print(f"  Profile error @{username}: {e}", file=sys.stderr, flush=True)

    if not profiles:
        return 0

    scored = _llm_batch_score(profiles)
    scored_map = {s["username"]: s for s in scored}

    count = 0
    for profile in profiles:
        username = profile["username"]
        score_data = scored_map.get(username, {"relevance_score": 0.5, "relevance_notes": "no score"})
        upsert_account(
            conn,
            username=username,
            display_name=profile.get("display_name"),
            bio=profile.get("bio"),
            follower_count=profile.get("follower_count"),
            following_count=profile.get("following_count"),
            relevance_score=score_data["relevance_score"],
            relevance_notes=score_data.get("relevance_notes"),
            discovery_source="social_graph",
            stage="scored",
        )
        print(
            f"  Scored @{username}: {score_data['relevance_score']:.2f} — {score_data.get('relevance_notes','')[:60]}",
            flush=True,
        )
        count += 1

    return count


# ---------------------------------------------------------------------------
# LLM batch scoring
# ---------------------------------------------------------------------------


def _llm_batch_score(profiles: list[dict]) -> list[dict]:
    """Batch-score profiles via LLM. Returns list of {username, relevance_score, relevance_notes}."""
    if not profiles:
        return []

    profile_summaries = [
        {
            "username": p["username"],
            "display_name": (p.get("display_name") or "")[:60],
            "bio": (p.get("bio") or "")[:200],
            "followers": p.get("follower_count", 0),
        }
        for p in profiles
    ]

    prompt = f"""Score each Twitter account for relevance to @DecentCloud_org's target audience.

@DecentCloud_org is a P2P cloud marketplace targeting DevOps/SRE/infra engineers who have cloud cost pain, provider accountability frustrations, or are building distributed systems.

Ideal (score 0.7-1.0):
- Cloud cost practitioners, FinOps advocates
- DevOps/SRE engineers who talk about AWS/GCP/Azure pain, billing surprises, support failures
- Self-hosting advocates (DHH-style)
- Infra practitioners at startups who care about unit economics
- Cloud-skeptic or AWS-critical voices
- Platform engineers, k8s operators, on-call engineers

Adjacent (score 0.4-0.6):
- General software engineers with occasional infra content
- Developer advocates at cloud companies (useful signal but less pain-expressing)

Not relevant (score 0.0-0.3):
- Generic tech news or influencer accounts
- Enterprise software sales/marketing
- Pure AI/ML hype without cloud infra angle
- Crypto/blockchain maximalists with no cloud angle
- Accounts with extremely broad topics (no infra specialization)

Accounts to score:
{json.dumps(profile_summaries, indent=2)}

Return ONLY a JSON array. Each element: {{"username": "...", "score": 0.0-1.0, "notes": "max 20 words"}}
Example: [{{"username": "example", "score": 0.85, "notes": "active AWS cost critic, SRE at startup"}}]"""

    try:
        success, raw = call_llm(prompt, max_retries=2, timeout=120)
        if not success or not raw:
            print(f"  LLM scoring failed — using neutral scores", file=sys.stderr, flush=True)
            return [{"username": p["username"], "relevance_score": 0.5, "relevance_notes": "llm_unavailable"} for p in profiles]

        json_str = extract_json(raw)
        if not json_str:
            # Try to parse raw directly, stripping common markdown fences
            cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            json_str = cleaned
        try:
            scored_list = json.loads(json_str)
        except json.JSONDecodeError:
            print(f"  LLM response was not valid JSON. First 300 chars: {raw[:300]}", file=sys.stderr, flush=True)
            raise
        if not isinstance(scored_list, list):
            raise ValueError(f"Expected list, got {type(scored_list)}")

        result = []
        for item in scored_list:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username", "")).lower().strip()
            if not username:
                continue
            score = float(item.get("score", 0.5))
            notes = str(item.get("notes", ""))[:200]
            result.append({
                "username": username,
                "relevance_score": max(0.0, min(1.0, score)),
                "relevance_notes": notes,
            })

        print(f"  LLM scored {len(result)}/{len(profiles)} accounts", flush=True)
        return result

    except Exception as e:
        print(f"  LLM scoring error: {e}", file=sys.stderr, flush=True)
        return [{"username": p["username"], "relevance_score": 0.5, "relevance_notes": "llm_error"} for p in profiles]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=== TWITTER ACCOUNT DISCOVERY ===", flush=True)
    print(f"Time: {utc_now()}", flush=True)

    try:
        with get_conn() as conn:
            # Phase 0: Score any unscored candidates (seeds on first run)
            print("\n--- Phase 0: Score unscored candidates ---", flush=True)
            scored_count = score_unscored_candidates(conn)
            print(f"  Scored {scored_count} candidate(s)", flush=True)

            # Phase 1: Auto-follow disabled — follow accounts manually from browser
            followed_count = 0

            # Phase 2: Gather new candidates from social graph of followed accounts
            print("\n--- Phase 2: Gather new candidates from social graph ---", flush=True)
            new_candidates = gather_new_candidates(conn)
            print(f"  Found {len(new_candidates)} new candidate handle(s)", flush=True)

            # Phases 3+4: Fetch profiles and score new candidates
            if new_candidates:
                print("\n--- Phases 3+4: Fetch profiles and score ---", flush=True)
                upserted = fetch_and_score_new_candidates(conn, new_candidates)
                print(f"  Upserted {upserted} new scored candidate(s)", flush=True)
            else:
                upserted = 0

            print(f"\nDiscovery complete: scored={scored_count}, followed={followed_count}, new_candidates={upserted}", flush=True)

        return 0

    except Exception as e:
        send_error_alert(f"Account discovery error: {e}")
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    sys.exit(main())
