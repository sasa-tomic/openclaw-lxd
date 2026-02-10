#!/usr/bin/env python3
"""
Morning TODO Review - Pull tasks from Todoist and calendar, send summary via Telegram
"""

import subprocess
import sys
from datetime import datetime

TELEGRAM_TARGET = "5996479639"


def run_command(cmd, timeout=60):
    """Run a command and return output"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "Command timed out", 1


def send_telegram(message):
    """Send message to Telegram"""
    # Escape quotes in message
    escaped = message.replace('"', '\"').replace("`", "\`")
    # Use full path to openclaw (systemd doesn't have .npm-global in PATH)
    cmd = f'/home/openclaw/.npm-global/bin/openclaw message send --channel telegram --target {TELEGRAM_TARGET} --message "{escaped}"'
    stdout, stderr, returncode = run_command(cmd, timeout=30)
    
    # VERIFY IT WORKED
    if returncode != 0:
        print(f"❌ SEND FAILED: {stderr}")
        sys.exit(1)
    if "Message ID:" not in stdout:
        print(f"❌ NO MESSAGE ID in response: {stdout}")
        sys.exit(1)
    
    msg_id = stdout.split("Message ID: ")[1].split()[0] if "Message ID:" in stdout else "unknown"
    print(f"✅ Sent to Telegram (ID: {msg_id})")

def main():
    print("=== MORNING TODO REVIEW ===")
    print(f"Time: {datetime.now().isoformat()}")

    summary_parts = []

    # 1. Sync Todoist
    print("\\nSyncing Todoist...")
    stdout, stderr, returncode = run_command("todoist sync")
    if returncode == 0:
        print("✅ Todoist synced")
    else:
        print(f"⚠️ Todoist sync failed: {stderr}")
        summary_parts.append("⚠️ Todoist sync failed")

    # 2. Get P1 tasks
    print("\\nFetching P1 tasks...")
    stdout, stderr, returncode = run_command('todoist list --filter "p1"')
    if returncode == 0 and stdout.strip():
        p1_tasks = stdout.strip().split("\\n")
        print(f"Found {len(p1_tasks)} P1 tasks")
        summary_parts.append(f"**P1 Tasks ({len(p1_tasks)}):**")
        for task in p1_tasks[:5]:  # Top 5
            summary_parts.append(f"• {task}")
    else:
        print("No P1 tasks or error")

    # 3. Get Personal P1 tasks
    print("\\nFetching Personal P1 tasks...")
    stdout, stderr, returncode = run_command('todoist list --filter "#Personal & p1"')
    if returncode == 0 and stdout.strip():
        personal_p1 = stdout.strip().split("\\n")
        print(f"Found {len(personal_p1)} Personal P1 tasks")
        if personal_p1:
            summary_parts.append(f"\\n**Personal P1 ({len(personal_p1)}):**")
            for task in personal_p1[:3]:  # Top 3
                summary_parts.append(f"• {task}")

    # 4. Get overdue/today tasks
    print("\\nFetching overdue/today tasks...")
    stdout, stderr, returncode = run_command('todoist list --filter "overdue | today"')
    if returncode == 0 and stdout.strip():
        urgent = stdout.strip().split("\\n")
        print(f"Found {len(urgent)} overdue/today tasks")
        if urgent:
            summary_parts.append(f"\\n**⚠️ Overdue/Today ({len(urgent)}):**")
            for task in urgent[:5]:
                summary_parts.append(f"• {task}")

    # 5. Check calendar (next 48h)
    print("\\nChecking calendar...")
    stdout, stderr, returncode = run_command(
        "cd /projects/automations/google-calendar && .venv/bin/python gcal.py list --days 2"
    )
    if returncode == 0 and stdout.strip():
        print("✅ Calendar checked")
        # Parse calendar output for upcoming events
        if "event" in stdout.lower() or "meeting" in stdout.lower():
            summary_parts.append(f"\\n**📅 Calendar (next 48h):**\\n{stdout[:300]}")

    # Build final message
    if not summary_parts:
        message = "📋 **Morning Review**\\n\\nNo urgent tasks or Todoist check failed.\\nFallback: Check `/projects/Notes/TODO.md`"
    else:
        message = "📋 **Morning Review**\\n\\n" + "\\n".join(summary_parts)

    print(f"\\nSending summary to Telegram...")
    print(message)
    send_telegram(message)

    print("\\n✅ Morning TODO review complete")


if __name__ == "__main__":
    main()
