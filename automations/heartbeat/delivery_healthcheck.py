#!/usr/bin/env python3
"""Message Delivery Healthcheck

Verify end-to-end message delivery capability.
Run this as a healthcheck - if it fails, alert immediately.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import OPENCLAW_BIN, TELEGRAM_TARGET

OPENCLAW = OPENCLAW_BIN
TARGET = TELEGRAM_TARGET


def send_test_message(msg: str) -> tuple[bool, str]:
    """Send a test message via openclaw."""
    try:
        result = subprocess.run(
            [
                OPENCLAW,
                "message",
                "send",
                "--channel",
                "telegram",
                "--target",
                TARGET,
                "--message",
                msg,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


def check_openclaw_in_path() -> bool:
    """Check if openclaw is available."""
    try:
        result = subprocess.run(["which", "openclaw"], capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False


def main():
    print("=== MESSAGE DELIVERY HEALTHCHECK ===")
    print(f"Time: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")

    timestamp = int(time.time())
    test_msg = f"healthcheck-{timestamp}"

    # 1. Send test message
    print("Sending test message...")
    success, output = send_test_message(test_msg)

    if success and "Sent via Telegram" in output:
        match = re.search(r"Message ID: (\d+)", output)
        msg_id = match.group(1) if match else "unknown"
        print(f"✅ Message sent (ID: {msg_id})")
    else:
        print("❌ CRITICAL: Message send failed")
        print(f"Output: {output}")
        return 1

    # 2. Check if openclaw is in PATH
    if not check_openclaw_in_path():
        print("⚠️ WARNING: openclaw not in PATH (systemd services will fail)")
        return 1

    print("✅ Delivery healthcheck passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
