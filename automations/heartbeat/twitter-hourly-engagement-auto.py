#!/usr/bin/env python3
"""
Twitter Hourly Engagement (Auto) - Automated engagement during active hours
Runs ONE small engagement (like/reply) for @DecentCloud_org
"""

import subprocess
import sys
from datetime import datetime

AGENT_MESSAGE = """Run ONE small engagement for @DecentCloud_org during active hours.

Jitter:
- Sleep a random 0–59 minutes before acting.

Selection:
- Use /tmp/twitter-engagement-results.txt (or run /projects/automations/heartbeat/twitter-engagement.sh if stale) to pick ONE high-signal tweet.
- Avoid promo/airdrop/web3-gaming shill, politics, drama, dunking.

Action rules (NO approval needed):
- Prefer AUTO-LIKE (safest).
- AUTO-REPLY only if it's a neutral question or short practical tip. No strong claims, no sales pitch, no sarcasm.
- Max: exactly 1 action (like OR reply).

Execution:
- Use browser profile satbox.
- For reply:
  1) Draft reply text.
  2) Run it through the humanize filter: `/projects/automations/text/humanize.py` (stdin->stdout).
  3) Use the *humanized* text in the X intent flow with in_reply_to + prefilled text, then click Reply.
- For like, open the tweet URL and click Like.

Output:
- Announce what you did (tweet link/id + like/reply + the reply text if any).
- If nothing qualifies, announce 'skipped' with 1-line reason.

Do NOT call message tool; rely on delivery announce.

Hard rule:
- Any outbound X/Twitter text (tweet/reply/quote) MUST be passed through `/projects/automations/text/humanize.py` before posting, even when using browser intent flow.
"""


def run_agent_task():
    """Run agent task via openclaw agent command"""
    print("=== TWITTER HOURLY ENGAGEMENT (AUTO) ===")
    print(f"Time: {datetime.now().isoformat()}")

    # Use isolated session with agent main
    cmd = [
        "openclaw",
        "agent",
        "--agent",
        "main",
        "--message",
        AGENT_MESSAGE,
        "--timeout",
        "300",
        "--json",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=320)

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
