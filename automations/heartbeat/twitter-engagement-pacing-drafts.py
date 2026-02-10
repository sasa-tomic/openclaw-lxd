#!/usr/bin/env python3
"""
Twitter Engagement Pacing Drafts - 3x daily engagement draft preparation
Keep engagement pace for @DecentCloud_org WITHOUT posting
"""

import subprocess
import sys
from datetime import datetime

AGENT_MESSAGE = """Keep engagement pace for @DecentCloud_org WITHOUT posting.

1) Run /projects/automations/heartbeat/twitter-engagement.sh (or read /tmp/twitter-engagement-results.txt if already fresh).
2) Pick 5-8 best tweets to engage with (avoid low-quality promo; prefer real builders or high-signal threads).
   IMPORTANT: avoid tweets we already engaged with recently (don’t repeat the same tweetId).
3) Draft replies (short, human, not salesy). Ask 1 concrete question when possible.
4) Output an approval queue: each item = link/id + the draft reply.

Execution model (FYI): when approved, we will usually do LIKE + REPLY for each item with a random 5–60s delay between actions.

Constraints: no likes, no replies, no follows. Do not call message tool; rely on delivery announce."""


def run_agent_task():
    """Run agent task via openclaw agent command"""
    print("=== TWITTER ENGAGEMENT PACING DRAFTS ===")
    print(f"Time: {datetime.now().isoformat()}")

    cmd = [
        "openclaw",
        "agent",
        "--agent",
        "main",
        "--message",
        AGENT_MESSAGE,
        "--timeout",
        "180",
        "--json",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=200)

        print(f"Exit code: {result.returncode}")
        if result.stdout:
            print(f"Output: {result.stdout[:500]}")
        if result.stderr:
            print(f"Error: {result.stderr[:500]}")

        return result.returncode

    except subprocess.TimeoutExpired:
        print("❌ Agent task timed out")
        return 1
    except Exception as e:
        print(f"❌ Error running agent: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(run_agent_task())
