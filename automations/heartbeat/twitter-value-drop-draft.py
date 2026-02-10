#!/usr/bin/env python3
"""
Twitter Value-Drop Draft - Daily tweet draft preparation
Prepares 1 value-drop tweet draft for @DecentCloud_org (no posting)
"""

import subprocess
import sys
from datetime import datetime

AGENT_MESSAGE = """Prepare 1 value-drop tweet draft for @DecentCloud_org (no posting). Use /projects/automations/heartbeat/twitter-morning.sh as input if available; otherwise do quick scan of today's items in /tmp/twitter-morning-results.txt and pick the best. Apply human writing style + humanize rule. Output: 1 draft + 1 fallback draft + suggested posting time window. Do not call message tool; rely on delivery announce."""


def run_agent_task():
    """Run agent task via openclaw agent command"""
    print("=== TWITTER VALUE-DROP DRAFT ===")
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
