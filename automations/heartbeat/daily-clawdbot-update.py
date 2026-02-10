#!/usr/bin/env python3
"""
Daily Clawdbot Update - Check for updates and restart gateway
Runs openclaw update and restarts the gateway service
"""

import subprocess
import sys
from datetime import datetime


def run_command(cmd, timeout=300):
    """Run a command and return output"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "Command timed out", 1


def main():
    print("=== DAILY OPENCLAW UPDATE ===")
    print(f"Time: {datetime.now().isoformat()}")

    # Run openclaw update
    print("Running: openclaw update")
    stdout, stderr, returncode = run_command("/home/openclaw/.npm-global/bin/openclaw update")

    update_applied = False
    if returncode == 0:
        if "already up to date" in stdout.lower() or "up-to-date" in stdout.lower():
            print("✅ OpenClaw is already up to date")
            message = "OpenClaw is already up-to-date. No update needed."
        else:
            print("✅ Update completed successfully")
            print(stdout)
            update_applied = True
            message = (
                f"OpenClaw update applied successfully:\\n```\\n{stdout[:500]}\\n```"
            )
    else:
        print(f"❌ Update failed: {stderr}")
        message = f"❌ OpenClaw update failed:\\n```\\n{stderr[:500]}\\n```"

    # Restart gateway service
    print("\\nRestarting openclaw-gateway service...")
    stdout, stderr, returncode = run_command(
        "systemctl --user restart openclaw-gateway", timeout=30
    )

    if returncode == 0:
        print("✅ Gateway restarted successfully")
        message += "\\n\\n✅ openclaw-gateway service restarted successfully."
    else:
        print(f"❌ Gateway restart failed: {stderr}")
        message += f"\\n\\n❌ Gateway restart failed: {stderr[:200]}"

    # Send notification (no Telegram target specified in original, so just log)
    print(f"\\nFinal message:\\n{message}")

    print("\\nDaily update complete.")
    return 0 if not update_applied or returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
