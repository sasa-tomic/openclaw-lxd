#!/usr/bin/env python3
"""Pickle Heartbeat Pre-Check Script

Runs all health checks and only wakes Pickle if there are issues.
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import TELEGRAM_TARGET, OPENCLAW_BIN
from lib.state_utils import load_state, save_state
from lib.telegram_utils import send_telegram

SCRIPT_DIR = Path(__file__).parent
LOG_DIR = Path.home() / ".pickle" / "logs"
STATE_FILE = Path.home() / ".pickle" / "heartbeat-state.json"
STATE_DEFAULT = {"lastChecks": {}, "lastIssues": [], "lastAlertTime": None}
ALERT_FILE = Path.home() / ".pickle" / "heartbeat-alerts.txt"


def ensure_dirs():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


def run_cmd(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    """Run command and return result."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def check_whatsapp() -> tuple[list[str], list[str]]:
    """Check WhatsApp sync service health. Returns (issues, alerts)."""
    issues, alerts = [], []

    result = run_cmd(["systemctl", "--user", "is-active", "wacli-sync"])
    sync_running = result.returncode == 0

    if sync_running:
        result = run_cmd(
            [
                "journalctl",
                "--user",
                "-u",
                "wacli-sync",
                "--since",
                "5 minutes ago",
                "--no-pager",
                "-n",
                "50",
            ]
        )
        logs = result.stdout.lower()
        if "error" in logs or "failed" in logs or "not authenticated" in logs:
            print("⚠️ WhatsApp: sync running but errors in logs")
            issues.append("WhatsApp: errors in sync logs")
        else:
            print("✅ WhatsApp: sync running healthy")
    else:
        print("❌ WhatsApp: sync not running")
        issues.append("WhatsApp: sync not running")
        result = run_cmd(["systemctl", "--user", "restart", "wacli-sync"])
        if result.returncode != 0:
            alerts.append("Failed to restart wacli-sync")

    return issues, alerts


def check_service(name: str) -> tuple[list[str], list[str]]:
    """Check if a systemd user service is active. Returns (issues, alerts)."""
    issues, alerts = [], []

    result = run_cmd(["systemctl", "--user", "is-active", name])

    if result.returncode == 0:
        print(f"✅ {name}: active")
    else:
        print(f"❌ {name}: DOWN")
        issues.append(f"{name}: DOWN")
        # Try to fix
        restart = run_cmd(["systemctl", "--user", "restart", name])
        if restart.returncode != 0:
            alerts.append(f"Failed to restart {name}")

    return issues, alerts


def check_logs() -> tuple[list[str], list[str]]:
    """Check recent logs for errors. Returns (issues, alerts)."""
    issues, alerts = [], []

    result = run_cmd(
        [
            "journalctl",
            "--user",
            "--since",
            "15 minutes ago",
            "--priority",
            "err",
            "--no-pager",
            "-n",
            "200",
        ]
    )

    lines = [
        l for l in result.stdout.strip().split("\n") if l and "No entries" not in l
    ]

    if lines:
        count = len(lines)
        print(f"⚠️ Found {count} error log entries")
        sample = lines[:5]
        issues.append(f"Errors in logs (last 15min): {count} entries")
        alerts.append("Recent error logs detected:\n" + "\n".join(sample))
    else:
        print("✅ No errors in recent logs")

    return issues, alerts


def check_calendar() -> list[str]:
    """Check calendar for upcoming events. Returns alerts."""
    alerts = []

    gcal_dir = Path("/projects/automations/google-calendar")
    gcal_script = gcal_dir / "gcal.py"

    if not gcal_script.exists():
        return alerts

    result = run_cmd(
        [str(gcal_dir / ".venv/bin/python"), str(gcal_script), "list", "--days", "1"]
    )

    if result.returncode != 0:
        print("❌ Calendar check failed")
        return alerts

    now = datetime.now(timezone.utc)
    two_hours = now + timedelta(hours=2)

    events_found = []
    for line in result.stdout.split("\n"):
        line = line.strip()
        if not line or "No events" in line:
            continue
        events_found.append(line)

    if events_found:
        print(f"✅ Calendar checked: {len(events_found)} event(s) found")
        upcoming = [e for e in events_found[:5]]
        if upcoming:
            alerts.append(f"📅 Upcoming calendar events:\n" + "\n".join(upcoming))
    else:
        print("✅ Calendar checked: no upcoming events")

    return alerts


def main():
    print(f"=== SYSTEM HEALTH CHECK @ {datetime.now(timezone.utc).isoformat()}Z ===")

    ensure_dirs()
    state = load_state(STATE_FILE, STATE_DEFAULT)

    issues = []
    alerts = []

    # 1. WhatsApp check
    print("\nWhatsApp: ", end="")
    i, a = check_whatsapp()
    issues.extend(i)
    alerts.extend(a)

    # 2. Service checks
    for service in ["signal-obsidian-sync", "message-watcher", "wacli-sync"]:
        print(f"{service}: ", end="")
        i, a = check_service(service)
        issues.extend(i)
        alerts.extend(a)

    # 3. Log check
    print("\n=== LOG CHECK ===")
    i, a = check_logs()
    issues.extend(i)
    alerts.extend(a)

    # 4. Calendar check
    print("\n=== CALENDAR CHECK ===")
    alerts.extend(check_calendar())

    # Update state
    state["lastChecks"]["heartbeat"] = datetime.now(timezone.utc).isoformat()
    state["lastIssues"] = issues
    save_state(STATE_FILE, state)

    # Wake Pickle if needed
    if alerts:
        print("\n=== WAKING PICKLE ===")

        alert_message = (
            f"🚨 Heartbeat Alert @ {datetime.now(timezone.utc).isoformat()}Z\n\n"
        )
        alert_message += "\n\n".join(alerts)

        ALERT_FILE.write_text(alert_message)

        if send_telegram(alert_message):
            print("✅ Pickle woken with alerts")
        else:
            print("❌ Failed to send alerts")
    elif issues:
        print("\nℹ️ Issues detected but auto-fixed (no alerts needed)")
    else:
        print("\n✅ HEARTBEAT_OK - all systems nominal")

    return 0


if __name__ == "__main__":
    sys.exit(main())
