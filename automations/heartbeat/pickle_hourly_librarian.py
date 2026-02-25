#!/usr/bin/env python3
"""Hourly librarian pass:
- Add safe wikilinks + conflict scan (link_and_conflict_scan.py)
- Scan last hour messenger logs for follow-ups (read-only)
- Quick LLM maintenance via opencode (every 4 hours)

Exit codes:
- 0: Success (including partial failures that were logged)
- 1: Critical failure (unable to run)

Logs all errors, never silently swallows failures.
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HB_DIR = Path("/projects/automations/heartbeat")
OBSIDIAN_DIR = Path("/projects/automations/obsidian")


def run_command(
    cmd: list[str], description: str, check: bool = False, timeout: int = 300
) -> subprocess.CompletedProcess:
    """Run a command, log output, optionally raise on failure."""
    print(f"Running: {description}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            print(f"  ⚠️ {description} failed (exit {result.returncode})")
            if result.stderr:
                print(f"  stderr: {result.stderr[:500]}")
            if check:
                raise RuntimeError(f"{description} failed: {result.stderr}")
        else:
            print(f"  ✓ {description} completed")
        return result
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ {description} timed out")
        if check:
            raise
        return subprocess.CompletedProcess(cmd, 1, "", "Timeout")
    except Exception as e:
        print(f"  ⚠️ {description} error: {e}")
        if check:
            raise
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def main():
    errors = []

    # 1) Links + conflicts (fast, always run)
    linker = OBSIDIAN_DIR / "link_and_conflict_scan.py"
    if linker.exists():
        result = run_command([sys.executable, str(linker)], "Link and conflict scan")
        if result.returncode != 0:
            errors.append("link_and_conflict_scan failed")
    else:
        print(f"⚠️ Linker not found: {linker}")
        errors.append("linker not found")

    # 2) Chat follow-ups (last 60m) - using v2 (LLM-validated)
    chat_followups = HB_DIR / "chat_followups_v2.py"
    if chat_followups.exists():
        env = os.environ.copy()
        env["MINUTES"] = "60"
        result = run_command(
            [sys.executable, str(chat_followups)],
            "Chat followups scan (LLM-validated)",
            timeout=120,  # LLM calls take longer
        )
        if result.returncode != 0:
            errors.append("chat_followups_v2 failed")
    else:
        # Fallback to old version if v2 doesn't exist
        chat_followups_old = HB_DIR / "chat_followups.py"
        if chat_followups_old.exists():
            env = os.environ.copy()
            env["MINUTES"] = "60"
            result = run_command(
                [sys.executable, str(chat_followups_old)], "Chat followups scan (regex)"
            )
            if result.returncode != 0:
                errors.append("chat_followups failed")

    # 3) Quick LLM maintenance (every 4 hours) - MANDATORY with retries
    hour = datetime.now().hour
    if hour in (0, 4, 8, 12, 16, 20):
        pickle_maint = OBSIDIAN_DIR / "pickle_obsidian_maintenance.py"
        if pickle_maint.exists():
            max_retries = 5
            retry_delay = 10  # seconds between retries
            
            for attempt in range(1, max_retries + 1):
                print(f"\nLLM maintenance attempt {attempt}/{max_retries}")
                
                result = run_command(
                    [
                        sys.executable,
                        str(pickle_maint),
                        "--mode",
                        "apply",
                        "--rule-id",
                        "quick_scan",
                        "--timeout",
                        "1080",  # 18 minutes for opencode (doubled)
                        "--no-telegram",
                    ],
                    f"Quick LLM maintenance (attempt {attempt}/{max_retries})",
                    timeout=1200,  # 20 minute wrapper timeout
                )
                
                if result.returncode == 0:
                    print(f"  ✓ LLM maintenance succeeded on attempt {attempt}")
                    break
                else:
                    if attempt < max_retries:
                        print(f"  ⚠️ Attempt {attempt} failed, retrying in {retry_delay}s...")
                        import time
                        time.sleep(retry_delay)
                    else:
                        print(f"  ❌ All {max_retries} attempts failed")
                        errors.append(f"quick_scan maintenance failed after {max_retries} attempts")

    # Report summary
    if errors:
        print(f"\n❌ FAILED: {', '.join(errors)}")
        return 1

    print("\n✓ Hourly librarian pass complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
