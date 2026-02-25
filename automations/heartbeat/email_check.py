#!/usr/bin/env python3
"""Email Check Script

Runs every 10-30 minutes to check for important emails.
Uses himalaya CLI to fetch emails and filters out marketing/spam.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import HIMALAYA_BIN, MEMORY_DIR
from lib.state_utils import load_state, save_state

STATE_FILE = Path(MEMORY_DIR) / "email-check-state.json"
RESULTS_FILE = Path("/tmp/email-check-results.txt")
HIMALAYA = Path(HIMALAYA_BIN)

MARKETING_PATTERNS = [
    r"noreply@",
    r"newsletter",
    r"notifications@",
    r"update@",
    r"news@",
    r"promo@",
    r"marketing@",
    r"offers@",
    r"deals@",
]

MARKETING_SUBJECT_PATTERNS = [
    r"your\s+weekly",
    r"digest",
    r"newsletter",
    r"promo",
    r"offer",
    r"deal",
    r"\d+%",
    r"[€$]\d+",
]

DEFAULT_STATE = {"lastEmailId": None, "lastCheck": None}


def run_himalaya(args: list[str]) -> tuple[bool, str]:
    """Run himalaya command and return (success, output)."""
    try:
        result = subprocess.run(
            [str(HIMALAYA)] + args,
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ.copy(),
        )
        return result.returncode == 0, result.stdout
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


def is_marketing(sender: str, subject: str) -> bool:
    """Check if email looks like marketing/spam."""
    sender_lower = sender.lower()
    subject_lower = subject.lower()

    for pattern in MARKETING_PATTERNS:
        if re.search(pattern, sender_lower):
            return True

    for pattern in MARKETING_SUBJECT_PATTERNS:
        if re.search(pattern, subject_lower):
            return True

    return False


def parse_email_list(output: str) -> list[dict]:
    """Parse himalaya envelope list output."""
    emails = []
    for line in output.strip().split("\n"):
        if not line.strip():
            continue
        # Format: ID | Date | From | Subject
        match = re.match(r"^(\d+)\s*\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|\s*(.*)$", line)
        if match:
            emails.append(
                {
                    "id": match.group(1).strip(),
                    "date": match.group(2).strip(),
                    "from": match.group(3).strip(),
                    "subject": match.group(4).strip(),
                }
            )
    return emails


def get_email_body(email_id: str) -> str:
    """Fetch email body."""
    success, output = run_himalaya(["message", "read", email_id])
    if success:
        lines = output.strip().split("\n")[:20]
        return "\n".join(lines)
    return "(Could not read email body)"


def main():
    print("=== EMAIL CHECK ===")

    state = load_state(STATE_FILE, DEFAULT_STATE)
    last_email_id = state.get("lastEmailId")
    print(f"Last checked ID: {last_email_id}")

    # List recent emails
    success, output = run_himalaya(["envelope", "list", "-s", "20"])
    if not success:
        print(f"Error listing emails: {output}", file=sys.stderr)
        return 1

    emails = parse_email_list(output)
    important_emails = []
    current_email_id = None

    for email in emails:
        if current_email_id is None:
            current_email_id = email["id"]

        # Stop if we've seen this email
        if email["id"] == last_email_id:
            break

        # Skip marketing
        if is_marketing(email["from"], email["subject"]):
            print(f"Skipping: {email['from']} - {email['subject']} (marketing)")
            continue

        print(f"📧 Important: {email['from']} - {email['subject']}")

        body = get_email_body(email["id"])

        important_emails.append(f"""
**From:** {email["from"]}
**Subject:** {email["subject"]}
**ID:** {email["id"]}

{body}

---
""")

    # Update state
    if current_email_id:
        state["lastEmailId"] = current_email_id
        state["lastCheck"] = datetime.now().isoformat()
        save_state(STATE_FILE, state)

    # Report
    if important_emails:
        print()
        print("IMPORTANT EMAILS FOUND:")
        for email in important_emails:
            print(email)

        RESULTS_FILE.write_text("IMPORTANT_EMAILS:" + "\n".join(important_emails))
    else:
        print("No important emails since last check.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
