"""Shared candidate triage helpers for engagement and queue fill flows."""

from __future__ import annotations

import time as _time

from lib.llm_utils import call_llm_simple as call_llm, extract_json
from twitter_utils import is_english_text


def reach_score(candidate: dict) -> int:
    """Simple reach proxy: higher = reply gets more eyeballs."""
    base = (candidate.get("likes") or 0) + (candidate.get("retweets") or 0) * 3
    tweet_id = int(candidate.get("tweetId") or candidate.get("tweet_id") or 0)
    if tweet_id:
        ms = (tweet_id >> 22) + 1288834974657
        age_min = (_time.time() * 1000 - ms) / 60000
        if age_min < 30:
            base += 500
        elif age_min < 60:
            base += 200
    return base


def llm_triage_candidates(candidates: list[dict], top_n: int = 15) -> list[str]:
    """Rank candidates by engagement potential in one LLM call."""
    fallback = [str(c.get("tweetId") or c.get("tweet_id") or "") for c in candidates]

    lines = []
    id_set: set[str] = set()
    for i, c in enumerate(candidates, 1):
        tid = str(c.get("tweetId") or c.get("tweet_id") or "")
        if not tid:
            continue
        id_set.add(tid)
        text = (c.get("text") or "")[:200].replace("\n", " ")
        likes = c.get("likes") or 0
        rts = c.get("retweets") or 0
        est = likes * 20 + rts * 50
        lines.append(f'[{i}] ID:{tid} [{likes}L {rts}RT ~{est:,} est.impressions] "{text}"')

    if not lines:
        return fallback[:top_n]

    prompt = f"""You are triaging Twitter candidates for @DecentCloud_org to reply to.

Strategy: We build a p2p AI-driven marketplace where providers earn reputation that's hard to build and easy to lose, and AI helps users and providers achieve their objectives. Goal: grow follower base by dropping sharp, human-sounding takes in high-visibility threads. Phase 1: no product pitches — just point out the pain.

For each candidate, assess:
- Reach opportunity (estimated impressions = how many people would see our reply) — this is the primary signal
- Hook potential: is there a specific fact, stat, or honest observation we could drop that earns likes from people reading this thread?
- Skip only: accounts with zero followers (no reach benefit)

Candidates (sorted by reach):
{chr(10).join(lines)}

Return ONLY a JSON array of the top {top_n} tweet IDs, ranked best-first (most worth replying to).
Include only IDs from the list above.  No explanation, no markdown — just the JSON array.
Example: ["1234567890", "9876543210"]"""

    try:
        raw = call_llm(prompt, timeout=60, json_mode=True)
        if not raw:
            return fallback[:top_n]

        parsed = extract_json(raw)
        if not isinstance(parsed, list):
            return fallback[:top_n]

        ranked = [str(x) for x in parsed if str(x) in id_set]
        seen = set(ranked)
        for tid in fallback:
            if tid not in seen:
                ranked.append(tid)
        return ranked[:top_n]
    except Exception as e:
        print(f"  LLM triage failed ({e}) — using reach-sort fallback", flush=True)
        return fallback[:top_n]


def filter_candidates_for_engagement(
    candidates: list[dict],
    *,
    engaged_ids: set[str],
    blocked_authors: set[str],
    extra_skip_ids: set[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Shared hard filters for engagement candidate lists.

    Returns (eligible, discard_ids).
    """
    eligible: list[dict] = []
    discard_ids: list[str] = []
    skip_ids = extra_skip_ids or set()
    blocked = {a.lower() for a in blocked_authors}

    for c in candidates:
        tid = str(c.get("tweetId") or c.get("tweet_id") or "")
        if not tid:
            continue
        if tid in engaged_ids or tid in skip_ids:
            discard_ids.append(tid)
            continue
        author = (c.get("author") or "").lower()
        if author in blocked:
            discard_ids.append(tid)
            continue
        if not is_english_text(c.get("text")):
            discard_ids.append(tid)
            continue
        eligible.append(c)

    return eligible, discard_ids
