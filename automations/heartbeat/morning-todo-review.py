#!/usr/bin/env python3
"""
Morning TODO Review - Pull tasks from Todoist and calendar, send summary via Telegram
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import TELEGRAM_TARGET, OPENCLAW_BIN
from lib.telegram_utils import send_telegram
from lib.todoist_client import TodoistClient


def run_command(cmd, timeout=60):
    """Run a command and return output"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "Command timed out", 1


def format_task(task: dict) -> str:
    """Format a task dict for display."""
    content = task.get("content", "")
    due = task.get("due", {})
    if due and due.get("date"):
        return f"{content} (due: {due['date']})"
    return content


def main():
    print("=== MORNING TODO REVIEW ===")
    print(f"Time: {datetime.now().isoformat()}")

    summary_parts = []

    # 1. Get all tasks via REST API (replaces sync + list)
    print("\nFetching tasks from Todoist...")
    all_tasks = TodoistClient.get_tasks()
    if all_tasks is not None:
        print(f"✅ Got {len(all_tasks)} tasks")
    else:
        print("⚠️ Failed to fetch tasks")
        summary_parts.append("⚠️ Todoist fetch failed")

    # 2. Get P1 tasks (priority 4 in API = P1 in UI)
    print("\nFetching P1 tasks...")
    p1_tasks = [t for t in all_tasks if t.get("priority") == 4]
    if p1_tasks:
        print(f"Found {len(p1_tasks)} P1 tasks")
        summary_parts.append(f"**P1 Tasks ({len(p1_tasks)}):**")
        for task in p1_tasks[:5]:
            summary_parts.append(f"• {format_task(task)}")
    else:
        print("No P1 tasks")

    # 3. Get Personal P1 tasks
    print("\nFetching Personal P1 tasks...")
    personal_project_id = None
    projects = TodoistClient.get_projects()
    for p in projects:
        if p.get("name", "").lower() == "personal":
            personal_project_id = p.get("id")
            break

    personal_p1 = [
        t
        for t in all_tasks
        if t.get("priority") == 4 and t.get("project_id") == personal_project_id
    ]
    if personal_p1:
        print(f"Found {len(personal_p1)} Personal P1 tasks")
        summary_parts.append(f"\n**Personal P1 ({len(personal_p1)}):**")
        for task in personal_p1[:3]:
            summary_parts.append(f"• {format_task(task)}")

    # 4. Get overdue/today tasks
    print("\nFetching overdue/today tasks...")
    today = datetime.now().date().isoformat()
    urgent = [
        t
        for t in all_tasks
        if t.get("due") and t.get("due", {}).get("date", "") <= today
    ]
    if urgent:
        print(f"Found {len(urgent)} overdue/today tasks")
        summary_parts.append(f"\n**⚠️ Overdue/Today ({len(urgent)}):**")
        for task in urgent[:5]:
            summary_parts.append(f"• {format_task(task)}")

    # 5. Check calendar (next 48h)
    print("\nChecking calendar...")
    stdout, stderr, returncode = run_command(
        "cd /projects/automations/google-calendar && .venv/bin/python gcal.py list --days 2"
    )
    if returncode == 0 and stdout.strip():
        print("✅ Calendar checked")
        if "event" in stdout.lower() or "meeting" in stdout.lower():
            summary_parts.append(f"\n**📅 Calendar (next 48h):**\n{stdout[:300]}")

    # Build final message
    if not summary_parts:
        message = "📋 **Morning Review**\n\nNo urgent tasks or Todoist check failed.\nFallback: Check `/projects/Notes/TODO.md`"
    else:
        message = "📋 **Morning Review**\n\n" + "\n".join(summary_parts)

    print(f"\nSending summary to Telegram...")
    print(message)
    if not send_telegram(message):
        print("❌ Failed to send Telegram message")
        sys.exit(1)
    print("✅ Sent to Telegram")

    print("\n✅ Morning TODO review complete")


if __name__ == "__main__":
    main()
