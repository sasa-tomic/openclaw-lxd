#!/usr/bin/env python3
"""
Send Reminder - Generic reminder notification script
Usage: send-reminder.py "reminder message"
"""

import subprocess
import sys


def send_notification(message):
    """Send notification via system notification (no Telegram needed for reminders)"""
    print(f"=== REMINDER ===")
    print(message)
    print("=" * 50)


def main():
    if len(sys.argv) < 2:
        print('Usage: send-reminder.py "reminder message"')
        sys.exit(1)

    message = sys.argv[1]
    send_notification(message)


if __name__ == "__main__":
    main()
