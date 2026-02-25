#!/usr/bin/env python3
"""
Log Error Monitor - Checks logs for errors and invokes opencode to fix them.

Uses flock to prevent concurrent runs, timeout to limit execution time.

Configuration:
- STATE_FILE: Tracks seen errors
- OPENCODE_BIN: Path to opencode binary
- TIMEOUT_SECS: Max time for opencode to run (default 600)
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)
from lib.config import OPENCODE_BIN, OPENCLAW_BIN, TELEGRAM_TARGET
from lib.state_utils import load_state, save_state
from lib.telegram_utils import send_telegram

STATE_FILE = Path("/home/openclaw/clawd/memory/log-error-monitor-state.json")
LOCK_FILE = "/tmp/log-error-monitor.lock"
AUTOMATIONS_DIR = "/projects/automations"

TIMEOUT_SECS = 600
ERROR_COOLDOWN_HOURS = 4
MAX_ERRORS_PER_RUN = 10

SERVICES_TO_MONITOR = [
    "obsidian-watcher",
    "message-watcher",
    "openclaw-gateway",
    "signal-obsidian-sync",
    "telegram-obsidian-sync",
    "wacli-sync",
]


_STATE_DEFAULT = {"seenErrors": {}, "lastRun": None}


def get_journal_errors(since_hours: int = 1) -> list[dict]:
    errors = []
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")

    for service in SERVICES_TO_MONITOR:
        try:
            result = subprocess.run(
                [
                    "journalctl",
                    "--user",
                    "-u",
                    service,
                    "--since",
                    since_str,
                    "-n",
                    "100",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            for line in result.stdout.strip().split("\n"):
                if (
                    "[ERROR]" in line
                    or "[CRITICAL]" in line
                    or "error:" in line.lower()
                ):
                    errors.append(
                        {
                            "service": service,
                            "line": line.strip(),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )
        except Exception as e:
            logger.warning(f"Failed to get logs for {service}: {e}")

    return errors


def dedupe_errors(errors: list[dict], state: dict) -> list[dict]:
    unique = []
    seen_keys = set()

    for error in errors:
        error_key = error["line"][:100]
        if error_key in seen_keys:
            continue
        seen_keys.add(error_key)

        seen_time = state["seenErrors"].get(error_key)
        if seen_time:
            try:
                seen_dt = datetime.fromisoformat(seen_time)
                if datetime.now(timezone.utc) - seen_dt < timedelta(
                    hours=ERROR_COOLDOWN_HOURS
                ):
                    continue
            except (ValueError, TypeError):
                logger.debug(f"Could not parse timestamp: {seen_time}")

        unique.append(error)
        state["seenErrors"][error_key] = datetime.now(timezone.utc).isoformat()

    return unique[:MAX_ERRORS_PER_RUN]


def invoke_opencode_fix(errors: list[dict]) -> tuple[bool, str]:
    errors_by_service = defaultdict(list)
    for e in errors:
        errors_by_service[e["service"]].append(e["line"])

    error_summary = "\n".join(
        f"## {svc}\n" + "\n".join(f"- {e[:200]}" for e in lines[:5])
        for svc, lines in errors_by_service.items()
    )

    prompt = f"""Fix errors in my automation services.

## Errors detected in last hour:
{error_summary}

## CRITICAL: DO NOT GUESS - ALWAYS VERIFY

1. If the error involves a CLI command, RUN IT FIRST to verify the interface:
   - Run `<cli> --help` or `<cli> -h` to see actual arguments
   - Run `<cli> list` or similar to test current behavior
   - Only then modify the code to match the actual CLI interface

2. Read the source file causing the error
3. Identify the exact line and the incorrect invocation
4. Fix it to match the verified CLI interface
5. Test the fix by running the corrected command

## Example for Todoist CLI errors:
```bash
# First verify:
todoist --help
todoist add --help
# Then fix the code to use correct syntax
```

## Context:
- Services are in {AUTOMATIONS_DIR}/
- obsidian-watcher is at {AUTOMATIONS_DIR}/obsidian/obsidian-watcher.py

## Rules:
- ALWAYS run the CLI to verify before changing code
- Only make minimal fixes
- Summarize what was verified and changed"""

    cmd = [
        "timeout",
        str(TIMEOUT_SECS),
        OPENCODE_BIN,
        "run",
        prompt,
        "--dir",
        AUTOMATIONS_DIR,
    ]
    logger.info(f"Running: {' '.join(cmd[:4])}... (prompt {len(prompt)} chars)")

    try:
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECS + 30,
            env=env,
        )

        combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
        logger.info(
            f"Return code: {result.returncode}, stdout: {len(result.stdout)}B, stderr: {len(result.stderr)}B"
        )

        if result.returncode == 124:
            return False, f"opencode timed out after {TIMEOUT_SECS}s"
        elif result.returncode != 0:
            return (
                False,
                f"opencode failed (rc={result.returncode}):\n{combined_output[:1000]}",
            )

        if not result.stdout.strip() and not result.stderr.strip():
            return False, "opencode returned empty output"

        return True, combined_output.strip()

    except subprocess.TimeoutExpired:
        return False, f"opencode timed out after {TIMEOUT_SECS}s"
    except Exception as e:
        return False, f"Failed to run opencode: {e}"


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )

    lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, BlockingIOError):
        logger.warning("Another instance is already running, exiting")
        return 1

    try:
        logger.info("=== Log Error Monitor ===")
        logger.info(f"Time: {datetime.now(timezone.utc).isoformat()}")

        state = load_state(STATE_FILE, _STATE_DEFAULT)

        errors = get_journal_errors(since_hours=1)
        logger.info(f"Found {len(errors)} total errors in last hour")

        unique_errors = dedupe_errors(errors, state)
        logger.info(f"After dedup: {len(unique_errors)} new unique errors")

        if not unique_errors:
            logger.info("No new errors to process")
            state["lastRun"] = datetime.now(timezone.utc).isoformat()
            save_state(STATE_FILE, state)
            return 0

        for error in unique_errors:
            logger.info(f"  - [{error['service']}] {error['line'][:150]}")

        logger.info(f"\nInvoking opencode to fix (timeout: {TIMEOUT_SECS}s)...")
        success, output = invoke_opencode_fix(unique_errors)

        state["lastRun"] = datetime.now(timezone.utc).isoformat()
        save_state(STATE_FILE, state)

        if success:
            logger.info("\n=== opencode output ===")
            if output:
                logger.info(output[:3000])
            else:
                logger.info("(empty output)")

            message = f"🔧 **Log Error Monitor**\n\n"
            message += (
                f"Found {len(unique_errors)} errors, invoked opencode to fix.\n\n"
            )
            message += f"**Result:**\n{output[:1500]}"
            send_telegram(message)
        else:
            logger.error(f"\n=== Failed ===\n{output}")

            message = f"⚠️ **Log Error Monitor**\n\n"
            message += (
                f"Found {len(unique_errors)} errors but fix failed:\n{output[:500]}"
            )
            send_telegram(message)

        logger.info("\nLog error monitor complete.")
        return 0

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
