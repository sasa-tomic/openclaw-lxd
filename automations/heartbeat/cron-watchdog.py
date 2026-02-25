#!/usr/bin/env python3
"""
Cron Watchdog - Monitor openclaw cron job health
Checks if critical jobs are running on schedule and alerts if stale
"""

import subprocess
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)
from lib.config import TELEGRAM_TARGET, OPENCLAW_BIN
from lib.telegram_utils import send_telegram

send_telegram_alert = send_telegram

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
    output, returncode = run_command(f"{OPENCLAW_BIN} cron list --json")
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


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )

    logger.info("=== CRON WATCHDOG CHECK ===")
    logger.info(f"Time: {datetime.now().isoformat()}")

    jobs = get_cron_list()
    if jobs is None:
        logger.error("❌ Failed to get cron job list")
        send_telegram_alert("🚨 Cron watchdog: Failed to fetch cron job list!")
        sys.exit(1)

    stale_jobs = check_job_staleness(jobs)

    if stale_jobs:
        alert = "🚨 **Cron Watchdog Alert**\\n\\nStale jobs detected:\\n"
        for job in stale_jobs:
            alert += f"• {job}\\n"
        alert += "\\nRecommended action: `systemctl --user restart openclaw-gateway`"

        logger.warning(f"⚠️ Stale jobs: {stale_jobs}")
        send_telegram_alert(alert)
    else:
        logger.info("✅ All monitored jobs are healthy")

    logger.info("Watchdog check complete.")


if __name__ == "__main__":
    main()
