#!/usr/bin/env python3
"""
Cron Watchdog - Monitor openclaw cron job health
Checks if critical jobs are running on schedule and alerts if stale
"""

import subprocess
import json
import sys
from datetime import datetime, timedelta

# Telegram target from the original cron config
TELEGRAM_TARGET = "5996479639"

# Jobs to monitor (from original watchdog config)
MONITORED_JOBS = [
    "email-check",
    "obsidian-note-review",
    "Morning TODO review",
    "twitter-morning",
    "twitter-engagement",
    "obsidian-maintenance",
]


def run_command(cmd):
    """Run a command and return output"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired:
        return "", 1


def get_cron_list():
    """Get list of cron jobs via openclaw CLI"""
    output, returncode = run_command("/home/openclaw/.npm-global/bin/openclaw cron list --json")
    if returncode != 0:
        return None

    try:
        data = json.loads(output)
        return data.get("jobs", [])
    except json.JSONDecodeError:
        return None


def check_job_staleness(jobs):
    """Check if monitored jobs are stale (>90 minutes since last run)"""
    now = datetime.now()
    stale_jobs = []

    for job in jobs:
        if job.get("name") not in MONITORED_JOBS:
            continue

        if not job.get("enabled"):
            continue

        last_run_ms = job.get("state", {}).get("lastRunAtMs")

        # If job never ran or last run is None, it's stale
        if not last_run_ms:
            stale_jobs.append(job["name"])
            continue

        # Calculate time since last run
        last_run = datetime.fromtimestamp(last_run_ms / 1000)
        time_since = now - last_run

        if time_since > timedelta(minutes=90):
            stale_jobs.append(
                f"{job['name']} (last run: {int(time_since.total_seconds() / 60)}min ago)"
            )

    return stale_jobs


def send_telegram_alert(message):
    """Send alert to Telegram"""
    cmd = f'/home/openclaw/.npm-global/bin/openclaw message send --channel telegram --target {TELEGRAM_TARGET} --message "{message}"'
    run_command(cmd)


def main():
    print("=== CRON WATCHDOG CHECK ===")
    print(f"Time: {datetime.now().isoformat()}")

    jobs = get_cron_list()
    if jobs is None:
        print("❌ Failed to get cron job list")
        send_telegram_alert("🚨 Cron watchdog: Failed to fetch cron job list!")
        sys.exit(1)

    stale_jobs = check_job_staleness(jobs)

    if stale_jobs:
        alert = "🚨 **Cron Watchdog Alert**\\n\\nStale jobs detected:\\n"
        for job in stale_jobs:
            alert += f"• {job}\\n"
        alert += "\\nRecommended action: `systemctl --user restart openclaw-gateway`"

        print(f"⚠️ Stale jobs: {stale_jobs}")
        send_telegram_alert(alert)
    else:
        print("✅ All monitored jobs are healthy")
        # Don't send message if everything is OK (as per original config)

    print("Watchdog check complete.")


if __name__ == "__main__":
    main()
