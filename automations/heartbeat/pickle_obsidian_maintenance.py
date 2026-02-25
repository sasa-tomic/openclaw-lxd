#!/usr/bin/env python3
"""Pickle Obsidian Maintenance Service - Wrapper Script.

This is a thin wrapper that invokes the actual implementation.
It exists in heartbeat/ because it's called by the main obsidian_maintenance.py.

Architecture:
    heartbeat/obsidian_maintenance.py  <- MAIN ENTRY POINT
        └─ heartbeat/pickle_obsidian_maintenance.py  <- WRAPPER (this file)
               └─ obsidian/pickle_obsidian_maintenance.py  <- ACTUAL IMPLEMENTATION
                      (contains opencode prompts, rule definitions, Telegram notifications)

Usage:
    python pickle_obsidian_maintenance.py [--mode scan|apply] [--rule-id <id>]

Modes:
    scan  - Read-only analysis, no changes
    apply - Apply improvements (default)

Rule IDs:
    daily_maintenance    - Full daily pass (default)
    quick_scan           - Fast hourly check
    knowledge_extraction - Extract from daily notes
    organization         - Reorganize vault structure
    link_maintenance     - Fix and add wikilinks
    archive_cleanup      - Move stale content to archive

RECOMMENDATION: Consider renaming this file to clarify its role, e.g.:
    - pickle_obsidian_wrapper.py
    - run_pickle_maintenance.py
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

OBSIDIAN_DIR = Path("/projects/automations/obsidian")
PYTHON_SCRIPT = OBSIDIAN_DIR / "pickle_obsidian_maintenance.py"
RULES_FILE = OBSIDIAN_DIR / "vault-rules.json"
LOG_DIR = Path("/var/log/pickle")


def main():
    parser = argparse.ArgumentParser(description="Pickle Obsidian Maintenance")
    parser.add_argument("--mode", default="apply", choices=["scan", "apply"])
    parser.add_argument("--rule-id", default="daily_maintenance")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    if not PYTHON_SCRIPT.exists():
        print(f"ERROR: Python script not found: {PYTHON_SCRIPT}", file=sys.stderr)
        return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().isoformat()
    log_file = LOG_DIR / f"obsidian-maintenance-{datetime.now().strftime('%Y%m%d')}.log"

    print(f"=== PICKLE OBSIDIAN MAINTENANCE ===")
    print(f"Timestamp: {timestamp}")
    print(f"Mode: {args.mode}")
    print(f"Rule: {args.rule_id}")
    print()

    # Run the actual maintenance script
    cmd = [
        sys.executable,
        str(PYTHON_SCRIPT),
        "--mode",
        args.mode,
        "--rules",
        str(RULES_FILE),
        "--rule-id",
        args.rule_id,
        "--timeout",
        str(args.timeout),
    ]

    try:
        result = subprocess.run(cmd, capture_output=False, timeout=args.timeout + 60)
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        print("ERROR: Maintenance timed out", file=sys.stderr)
        exit_code = 124
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        exit_code = 1

    print()
    print(f"Exit code: {exit_code}")
    print(f"Completed: {datetime.now().isoformat()}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
