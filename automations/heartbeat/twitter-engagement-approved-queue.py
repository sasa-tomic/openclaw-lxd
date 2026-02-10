#!/usr/bin/env python3
"""Build/extend an *approved* engagement queue for @DecentCloud_org.

This is meant to let engagement run in the background with minimal/no human involvement.

Mechanism:
- Uses `openclaw agent` (main agent) to:
  - read fresh search results from twitter-engagement.sh output,
  - pick 5–8 high-signal targets,
  - draft short replies,
  - emit a JSON array for merging into the approved queue.

Queue file:
- /home/openclaw/clawd/memory/twitter-approved-engagement-queue.json

The executor consumes this queue later.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

QUEUE_PATH = Path("/home/openclaw/clawd/memory/twitter-approved-engagement-queue.json")

AGENT_MESSAGE = """Prepare a pre-approved engagement queue for @DecentCloud_org.

Goal: enable background execution with no further approval.

Steps:
1) Ensure we have fresh candidates: run /projects/automations/heartbeat/twitter-engagement.sh (or use /tmp/twitter-engagement-results.txt if already fresh).
2) Pick 5–8 best tweets to engage with.
   - Avoid promo/airdrop/web3-gaming shill, politics, drama, dunking.
   - Prefer real builders, infra/pricing/egress, outages, reliability, FinOps.
   - Avoid anything we've already engaged with recently.
3) For each chosen tweet:
   - Provide tweetId, canonical URL, and a short reply draft.
   - Reply must be: human, not salesy, not combative, ideally 1 concrete question.
   - NO hashtags, NO links in reply text.

Output format (IMPORTANT):
- Output ONLY valid JSON to stdout: an array of objects:
  [{"tweetId":"...","url":"https://x.com/i/web/status/<id>","reply":"...","reason":"..."}, ...]

Constraints:
- Do NOT like/reply/follow.
- Do NOT call message tool.
"""


def _load_queue() -> list[dict]:
    if QUEUE_PATH.exists():
        try:
            return json.loads(QUEUE_PATH.read_text())
        except Exception:
            return []
    return []


def _save_queue(items: list[dict]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(QUEUE_PATH)


def main() -> int:
    print("=== TWITTER APPROVED QUEUE (BUILD) ===")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")

    cmd = [
        "openclaw",
        "agent",
        "--agent",
        "main",
        "--message",
        AGENT_MESSAGE,
        "--timeout",
        "240",
        "--json",
    ]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=260)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        return r.returncode

    try:
        payload = json.loads(r.stdout)
        text = "\n".join(p.get("text", "") for p in payload.get("result", {}).get("payloads", []))
        
        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]  # Remove ```json
        elif text.startswith("```"):
            text = text[3:]  # Remove ```
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        
        candidates = json.loads(text)
        if not isinstance(candidates, list):
            raise ValueError("agent did not return a JSON array")
    except Exception as e:
        print("❌ Could not parse agent JSON output", e, file=sys.stderr)
        print("Raw agent output (first 800 chars):", r.stdout[:800], file=sys.stderr)
        return 1

    existing = _load_queue()
    existing_ids = {str(x.get("tweetId")) for x in existing if x.get("tweetId")}

    added = 0
    now = datetime.now(timezone.utc).isoformat()
    for c in candidates:
        tid = str(c.get("tweetId", "")).strip()
        if not tid or tid in existing_ids:
            continue
        item = {
            "tweetId": tid,
            "url": c.get("url") or f"https://x.com/i/web/status/{tid}",
            "reply": (c.get("reply") or "").strip(),
            "reason": (c.get("reason") or "").strip(),
            "approvedAt": now,
            "status": "approved",
        }
        # Require a reply draft; executor will like+reply.
        if not item["reply"]:
            continue
        existing.append(item)
        existing_ids.add(tid)
        added += 1

    _save_queue(existing)

    print(f"Approved queue updated: +{added}, total={len(existing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
