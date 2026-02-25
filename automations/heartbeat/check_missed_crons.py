#!/usr/bin/env python3
"""Check for Missed Cron Jobs

Runs every 30 minutes to catch and report jobs that missed their scheduled time.
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.state_utils import load_state, save_state

MEMORY_DIR = Path("/home/openclaw/clawd/memory")
STATE_FILE = MEMORY_DIR / "missed-crons-state.json"
CRON_LIST_FILE = Path("/tmp/cron-list-for-missed-check.json")
RESULTS_FILE = Path("/tmp/missed-crons-results.txt")

DEFAULT_STATE = {"lastCheck": None, "notifiedJobs": {}}


def main():
    print("=== MISSED CRON JOBS CHECK ===")
    print(f"Current time: {datetime.now(timezone.utc).isoformat()}")

    state = load_state(STATE_FILE, DEFAULT_STATE)
    current_time_ms = int(time.time() * 1000)

    if not CRON_LIST_FILE.exists():
        print(f"⚠️ No cron list file found at {CRON_LIST_FILE}")
        print("This script expects the cron list to be provided via the main session")
        print("✅ Check complete (no data to analyze)")
        return 0

    try:
        cron_data = json.loads(CRON_LIST_FILE.read_text())
    except Exception as e:
        print(f"Error reading cron list: {e}")
        return 1

    # Find missed jobs
    missed_jobs = []
    for job in cron_data.get("jobs", []):
        if not job.get("enabled", True):
            continue

        next_run = job.get("state", {}).get("nextRunAtMs")
        if next_run and int(next_run) < current_time_ms:
            missed_jobs.append(
                {"id": job.get("id"), "name": job.get("name"), "nextRunAtMs": next_run}
            )

    if not missed_jobs:
        print("✅ No missed jobs detected")
        state["lastCheck"] = datetime.now(timezone.utc).isoformat()
        save_state(STATE_FILE, state)
        return 0

    print(f"\n⚠️ MISSED JOBS FOUND: {len(missed_jobs)}")

    notified_now = []

    for job in missed_jobs:
        job_id = job.get("id")
        job_name = job.get("name", "Unknown")

        if not job_id:
            continue

        # Check if we already notified recently (within 1 hour)
        last_notified = state.get("notifiedJobs", {}).get(job_id)
        if last_notified:
            try:
                last_dt = datetime.fromisoformat(last_notified)
                hours_since = (
                    datetime.now(timezone.utc) - last_dt
                ).total_seconds() / 3600
                if hours_since < 1:
                    print(f"Skipping {job_name} (notified {hours_since:.1f} hours ago)")
                    continue
            except (ValueError, TypeError) as e:
                logger.debug(f"Could not parse timestamp '{last_notified}': {e}")

        notified_now.append(job_id)

        # Queue for processing
        with open(RESULTS_FILE, "a") as f:
            f.write(f"RUN_JOB:{job_id}:{job_name}\n")

        state["notifiedJobs"][job_id] = datetime.now(timezone.utc).isoformat()
        print(f"✅ Queued {job_name} for immediate run")

    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    save_state(STATE_FILE, state)

    print("\nMissed job check complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
