#!/usr/bin/env python3
"""Obsidian Notes Maintenance Script - Main Entry Point.

This is the primary entry point for daily vault maintenance, invoked by cron.
It performs basic checks and then delegates LLM-powered maintenance to the
Pickle service.

Architecture:
    heartbeat/obsidian_maintenance.py  <- MAIN ENTRY POINT (this file)
        │  - Performs non-LLM checks (cleanup queue, root files, linker, wacli)
        │  - Calls heartbeat/pickle_obsidian_maintenance.py for LLM tasks
        └─ heartbeat/pickle_obsidian_maintenance.py  <- WRAPPER
               └─ obsidian/pickle_obsidian_maintenance.py  <- ACTUAL IMPLEMENTATION

This script runs daily to keep the vault organized.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.state_utils import load_state, save_state

NOTES_DIR = Path("/projects/Notes")
OBSIDIAN_DIR = Path("/projects/automations/obsidian")
MEMORY_DIR = Path("/home/openclaw/clawd/memory")
STATE_FILE = MEMORY_DIR / "obsidian-maintenance-state.json"
ORG_DIR = NOTES_DIR / ".organization"
CHANGELOG = ORG_DIR / "changelog.md"
RESULTS_FILE = Path("/tmp/obsidian-maintenance-results.txt")

VERBOSE = os.environ.get("VERBOSE", "0") == "1"

DEFAULT_STATE = {"lastMaintenance": None}


def ensure_dirs():
    """Ensure required directories exist."""
    ORG_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def ensure_changelog():
    """Ensure changelog exists."""
    if not CHANGELOG.exists():
        CHANGELOG.write_text(f"""# Obsidian Vault Changelog

## {datetime.now().date()}
- Created changelog
""")


def get_cleanup_items() -> list[str]:
    """Extract pending cleanup items from README.md."""
    readme = ORG_DIR / "README.md"
    if not readme.exists():
        return []

    content = readme.read_text()
    items = []
    in_cleanup = False

    for line in content.split("\n"):
        if "## Cleanup Queue" in line:
            in_cleanup = True
            continue
        if in_cleanup:
            if line.startswith("## "):
                break
            if "Queue cleared" in line:
                break
            if line.strip().startswith("- [ ]"):
                items.append(line.strip())

    return items


def get_root_files() -> list[Path]:
    """Find root-level .md files that may need organizing."""
    ignore = {"TODO.md", "🏠 Home.md"}
    files = []

    for path in NOTES_DIR.iterdir():
        if path.is_file() and path.suffix == ".md" and path.name not in ignore:
            files.append(path)

    return sorted(files)


def run_linker() -> dict:
    """Run link_and_conflict_scan.py and return results."""
    linker = OBSIDIAN_DIR / "link_and_conflict_scan.py"
    if not linker.exists():
        return {"linked": 0, "conflicts": 0}

    try:
        result = subprocess.run(
            [sys.executable, str(linker)], capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception as e:
        if VERBOSE:
            print(f"Linker error: {e}")

    return {"linked": 0, "conflicts": 0}


def check_wacli() -> list[str]:
    """Check WhatsApp sync health."""
    issues = []

    try:
        result = subprocess.run(
            ["wacli", "doctor"], capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr

        # wacli doctor output format: "AUTHENTICATED  true" (multiple spaces, no colon)
        import re

        if not re.search(r"AUTHENTICATED\s+true", output):
            print("❌ WhatsApp sync: NOT AUTHENTICATED")
            issues.append("- ❌ WhatsApp sync not authenticated (needs QR re-link)")
            return issues

        print("✅ WhatsApp sync: Authenticated")

        # Check service
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "wacli-sync"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("✅ wacli-sync service: Running")
        else:
            print("⚠️ wacli-sync service: Not running")
            issues.append("- ⚠️ wacli-sync service not running (needs attention)")

    except FileNotFoundError:
        print("⚠️ wacli not found")

    return issues


def run_llm_maintenance() -> str:
    """Run opencode-powered maintenance."""
    pickle_maint = OBSIDIAN_DIR / "pickle_obsidian_maintenance.py"
    if not pickle_maint.exists():
        return ""

    try:
        result = subprocess.run(
            [
                sys.executable,
                str(pickle_maint),
                "--mode",
                "apply",
                "--rule-id",
                "daily_maintenance",
                "--no-telegram",
            ],
            capture_output=True,
            text=True,
            timeout=900,
        )
        output = result.stdout

        # Extract summary
        if "## Summary" in output:
            lines = output.split("\n")
            for i, line in enumerate(lines):
                if "## Summary" in line:
                    if i + 1 < len(lines):
                        return lines[i + 1].strip()

        return "opencode maintenance completed"
    except subprocess.TimeoutExpired:
        return "LLM maintenance timed out"
    except Exception as e:
        return f"LLM maintenance error: {e}"


def main():
    print("=== OBSIDIAN NOTES MAINTENANCE ===")
    date = datetime.now().isoformat()
    if VERBOSE:
        print(f"Date: {date}")

    ensure_dirs()
    ensure_changelog()
    state = load_state(STATE_FILE, DEFAULT_STATE)

    changes = []

    # 1. Check cleanup queue
    cleanup_items = get_cleanup_items()
    if cleanup_items:
        count = len(cleanup_items)
        if VERBOSE:
            print(f"\nCleanup queue items: {count}")
            for item in cleanup_items[:10]:
                print(f"  {item}")
        changes.append(
            f"- Reviewed cleanup queue in .organization/README.md ({count} items)"
        )
    elif VERBOSE:
        print("No cleanup items in queue.")

    # 2. Check root files
    root_files = get_root_files()
    if root_files:
        count = len(root_files)
        if VERBOSE:
            print(f"\nRoot-level .md files (excluding TODO/Home): {count}")
            for f in root_files[:20]:
                print(f"  {f.name}")
        changes.append(
            f"- Found root-level files that may need organization ({count} files)"
        )
    elif VERBOSE:
        print("No root-level files found (excluding TODO/Home).")

    # 3. Run linker
    if VERBOSE:
        print("\nRunning wikilink scanner...")
    link_result = run_linker()

    if link_result.get("linked"):
        count = (
            len(link_result["linked"])
            if isinstance(link_result["linked"], list)
            else link_result["linked"]
        )
        changes.append(f"- Added wikilinks in {count} recently modified note(s)")

    if link_result.get("conflicts"):
        count = (
            len(link_result["conflicts"])
            if isinstance(link_result["conflicts"], list)
            else link_result["conflicts"]
        )
        changes.append(f"- ⚠️ Possible conflicts found ({count}) — review recommended")
        Path("/tmp/obsidian-linker-report.json").write_text(
            json.dumps(link_result, indent=2)
        )

    # 4. Check WhatsApp sync
    if VERBOSE:
        print("\nChecking WhatsApp sync health...")
    wa_issues = check_wacli()
    changes.extend(wa_issues)

    # 5. Weekly tasks (Friday)
    if datetime.now().weekday() == 4:  # Friday
        if VERBOSE:
            print("\nFriday weekly tasks:")

        # Check TODO.md
        todo_file = NOTES_DIR / "TODO.md"
        if todo_file.exists():
            content = todo_file.read_text()
            todo_count = len(re.findall(r"^- \[", content, re.MULTILINE))
            done_count = len(
                re.findall(r"^- \[x\]", content, re.MULTILINE | re.IGNORECASE)
            )
            pending = todo_count - done_count

            if VERBOSE:
                print(f"TODO.md: {pending} pending items")

            if pending > 20:
                changes.append(
                    f"- TODO.md has {pending} pending items (consider review)"
                )

    # 6. LLM maintenance
    if VERBOSE:
        print("\nRunning opencode maintenance...")
    llm_summary = run_llm_maintenance()
    if llm_summary:
        changes.append(f"- opencode: {llm_summary}")
        if VERBOSE:
            print(llm_summary)

    # Update state
    state["lastMaintenance"] = date
    save_state(STATE_FILE, state)

    # Log changes
    if changes:
        print("\nChanges/observations logged:")
        for change in changes:
            print(f"  {change}")

        # Append to changelog
        with open(CHANGELOG, "a") as f:
            f.write(f"\n## {datetime.now().date()}\n")
            for change in changes:
                f.write(f"{change}\n")

        # Write results for cron handler
        RESULTS_FILE.write_text("MAINTENANCE:" + "\n".join(changes))
    else:
        print("No changes or observations to report.")

    print("\nMaintenance complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
