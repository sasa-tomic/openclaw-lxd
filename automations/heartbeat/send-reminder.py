#!/usr/bin/env python3
"""
Send Reminder - Generic reminder notification script
Usage: send-reminder.py "reminder message"
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib import send_telegram


def send_notification(message):
    """Send notification via Telegram, fall back to print."""
    print(f"=== REMINDER ===")
    print(message)
    print("=" * 50)

    if send_telegram(f"🔔 Reminder\n\n{message}"):
        print("✅ Sent via Telegram")
    else:
        print("⚠️ Telegram send failed, message printed above")


def main():
    if len(sys.argv) < 2:
        print('Usage: send-reminder.py "reminder message"')
        sys.exit(1)

    message = sys.argv[1]
    send_notification(message)


if __name__ == "__main__":
    main()
