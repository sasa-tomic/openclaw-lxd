#!/usr/bin/env python3
"""Pickle Obsidian Maintenance Service - Actual Implementation.

This is the real implementation of the LLM-powered vault maintenance.
It uses opencode CLI to inspect and improve the Obsidian vault based on rules.

Architecture:
    heartbeat/obsidian_maintenance.py  <- MAIN ENTRY POINT
        └─ heartbeat/pickle_obsidian_maintenance.py  <- WRAPPER
               └─ obsidian/pickle_obsidian_maintenance.py  <- ACTUAL IMPLEMENTATION (this file)

opencode has full file system access and can read, edit, move, and create notes.

Usage:
    python3 pickle_obsidian_maintenance.py [--mode scan|apply] [--rules path/to/rules.json]

Modes:
    scan  - Analyze and report findings (read-only, no file changes)
    apply - Apply improvements (opencode can edit/move/create files)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import (
    NOTES_DIR as NOTES_DIR_STR,
    MEMORY_DIR as MEMORY_DIR_STR,
    OPENCLAW_BIN,
    OPENCODE_BIN,
    TELEGRAM_TARGET,
)
from lib.telegram_utils import send_telegram

NOTES_DIR = Path(NOTES_DIR_STR)
MEMORY_DIR = Path(MEMORY_DIR_STR)
PICKLE_DIR = NOTES_DIR / "Pickle"

SKIP_DIRS = {
    ".obsidian",
    ".trash",
    ".stfolder",
    "Signal",
    "WhatsApp",
    "Telegram",
    "Reference",  # No TODOs from reference materials
    "_reports",   # No TODOs from auto-generated reports
    "Archive",    # Already archived; don't include in active maintenance scope
}

DEFAULT_RULES = [
    {
        "id": "daily_maintenance",
        "name": "Daily Vault Maintenance",
        "description": "Daily maintenance pass focused on recently modified notes",
        "enabled": True,
        "recent_days": 7,
        "oldest_notes": 5,
        "tasks": [
            "Fix broken wikilinks - find [[links]] pointing to non-existent notes and either create the missing note, fix the link to point to an existing similar note, or remove if obsolete",
            "Add missing wikilinks - find plain text mentions of note titles and convert them to [[wikilinks]]",
            "Clean up TODOs - mark completed tasks, flag stale items older than 2 weeks",
            "Extract knowledge from daily notes - find important decisions, insights, or recurring topics that deserve permanent notes",
            "Remove intra-note duplicates - within each scoped note, delete repeated sections or content that restates the same thing; keep the clearest version",
            "Remove cross-note duplicates - if a scoped note duplicates content from another note, remove the duplicate and replace with a [[wikilink]]; you may read other notes for comparison but only modify files in scope (plus the merge target if appending)",
            "Consolidate near-empty notes - for any scoped note under ~150 words: (a) if covered elsewhere, delete and update incoming links; (b) if valuable but thin, append to the most relevant note then delete; (c) if trivial, delete without merging",
        ],
    },
    {
        "id": "quick_scan",
        "name": "Quick Hourly Scan",
        "description": "Fast scan for urgent issues only",
        "enabled": True,
        "tasks": [
            "Fix broken wikilinks only - quick pass to fix obviously broken links",
            "Update any obviously stale TODOs - tasks clearly done but not marked",
        ],
    },
]


def load_rules(rules_path: Path | None) -> dict:
    if rules_path and rules_path.exists():
        return json.loads(rules_path.read_text())
    return {"rules": DEFAULT_RULES}


def _iter_vault_notes(vault_path: Path):
    """Yield (mtime, path) for all non-skipped notes in the vault."""
    for p in vault_path.rglob("*.md"):
        parts = set(p.relative_to(vault_path).parts)
        if any(d in parts for d in SKIP_DIRS) or any(part.startswith(".") for part in p.parts):
            continue
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        except FileNotFoundError:
            continue
        yield mtime, p


def get_recent_notes(vault_path: Path, days: int) -> list[Path]:
    """Return notes modified in the last `days` days, excluding skipped dirs."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [p for mtime, p in _iter_vault_notes(vault_path) if mtime >= cutoff]


def get_oldest_notes(vault_path: Path, count: int) -> list[tuple[Path, datetime]]:
    """Return the `count` least-recently-modified notes, oldest first."""
    all_notes = sorted(_iter_vault_notes(vault_path), key=lambda x: x[0])
    return [(p, mtime) for mtime, p in all_notes[:count]]


def build_prompt(rule: dict, mode: str, vault_path: Path) -> str:
    read_only = mode == "scan"
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    tasks_list = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(rule["tasks"]))

    # Build scope: recently modified notes + oldest untouched notes
    recent_days = rule.get("recent_days")
    oldest_count = rule.get("oldest_notes", 0)

    recent_notes: list[Path] = []
    oldest_notes: list[tuple[Path, datetime]] = []

    if recent_days:
        recent_notes = get_recent_notes(vault_path, recent_days)
    if oldest_count:
        oldest_notes = get_oldest_notes(vault_path, oldest_count)

    scope_section = ""
    if recent_days or oldest_count:
        parts: list[str] = []

        if recent_days:
            if recent_notes:
                note_list = "\n".join(
                    f"- {p.relative_to(vault_path)}" for p in sorted(recent_notes)
                )
                parts.append(
                    f"**Recently modified (last {recent_days} days) — {len(recent_notes)} notes:**\n{note_list}"
                )
            else:
                parts.append(f"**Recently modified (last {recent_days} days):** none")

        if oldest_count and oldest_notes:
            note_list = "\n".join(
                f"- {p.relative_to(vault_path)}  (last modified {mtime.strftime('%Y-%m-%d')})"
                for p, mtime in oldest_notes
            )
            parts.append(
                f"**Oldest untouched notes — {len(oldest_notes)} notes (apply consolidation/cleanup rules to these):**\n{note_list}"
            )

        all_scoped = {p for p in recent_notes} | {p for p, _ in oldest_notes}
        scope_section = f"""
# Scope

Work ONLY on the {len(all_scoped)} notes listed below. Do NOT scan the entire vault.
You may read other notes for context (e.g. to check for duplicates or find a merge target),
but only write changes to files in this list (plus any merge-target file when appending content).

{chr(10).join(parts)}
"""

    prompt = f"""You are Pickle, the Obsidian vault maintenance assistant.

# Your Task

Perform the following maintenance tasks on the Obsidian vault at {vault_path}:

{tasks_list}

# Vault Information

- Vault root: {vault_path}
- Today's date: {date_str}
- Skip these directories: {", ".join(SKIP_DIRS)}
{scope_section}
# Rules

1. NEVER delete content or files. If something should be removed, move it to Archive/
2. NEVER modify files in: {", ".join(SKIP_DIRS)}
3. For daily notes in Daily/, only extract knowledge - don't reorganize them
4. When creating new notes, use descriptive titles and place them in appropriate folders
5. Preserve all existing content and wikilinks
6. When in doubt, be conservative - prefer suggestions over changes

"""

    if read_only:
        prompt += """# Mode: SCAN (Read-Only)

You are in SCAN mode. Do NOT modify any files.
- Read and analyze the vault
- Identify issues and opportunities
- Prepare a detailed report of what you found
- List what changes you WOULD make if in apply mode

"""
    else:
        prompt += """# Mode: APPLY

You are in APPLY mode. You MAY modify files to improve the vault.
- Fix broken wikilinks
- Add missing wikilinks  
- Update TODOs
- Create new notes for extracted knowledge
- Move misplaced notes to better locations

"""

    prompt += """# Output Requirements

After completing your analysis/changes, provide a summary in this format:

## Summary
<Brief 2-3 sentence overview>

## Changes Made (or Would Make in Scan Mode)
- [ ] <change description> - <file path>
...

## Issues Found
- <issue> - <file path or general>
...

## Suggestions for Manual Review
- <suggestion>
...

Start by exploring the vault structure, then proceed with the maintenance tasks.
"""

    return prompt


def run_opencode(
    prompt: str, workdir: Path, timeout: int = 600
) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"

    result = subprocess.run(
        [OPENCODE_BIN, "run", prompt, "--dir", str(workdir)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )

    return result.returncode, result.stdout, result.stderr


def save_report(rule_id: str, output: str, report_path: Path | None = None) -> Path:
    reports_dir = PICKLE_DIR / "_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if report_path is None:
        report_path = reports_dir / f"{date_str}-{rule_id}-report.md"

    full_report = f"""# {rule_id} - {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

{output}
"""
    report_path.write_text(full_report)

    results_path = MEMORY_DIR / f"obsidian-{rule_id}-results.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "rule_id": rule_id,
                "output": output,
            },
            indent=2,
        )
    )

    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Pickle Obsidian Maintenance Service")
    parser.add_argument(
        "--mode", choices=["scan", "apply"], default="apply", help="Operation mode"
    )
    parser.add_argument("--rules", type=Path, help="Path to rules JSON file")
    parser.add_argument("--rule-id", default="daily_maintenance", help="Rule ID to run")
    parser.add_argument(
        "--no-telegram", action="store_true", help="Skip Telegram notification"
    )
    parser.add_argument("--timeout", type=int, default=600, help="Timeout in seconds")
    args = parser.parse_args()

    print(f"=== PICKLE OBSIDIAN MAINTENANCE ({args.mode}) ===", flush=True)
    print(f"Time: {datetime.now(timezone.utc).isoformat()}", flush=True)
    print(f"Rule: {args.rule_id}", flush=True)

    if not Path(OPENCODE_BIN).exists():
        print(f"ERROR: opencode not found at {OPENCODE_BIN}", file=sys.stderr)
        return 1

    rules_config = load_rules(args.rules)

    rule = None
    for r in rules_config.get("rules", []):
        if r["id"] == args.rule_id:
            rule = r
            break

    if not rule:
        print(f"ERROR: Rule '{args.rule_id}' not found", file=sys.stderr)
        return 1

    if not rule.get("enabled", True):
        print(f"Rule '{args.rule_id}' is disabled, skipping", flush=True)
        return 0

    prompt = build_prompt(rule, args.mode, NOTES_DIR)

    print(f"Running opencode with {args.timeout}s timeout...", flush=True)

    try:
        returncode, stdout, stderr = run_opencode(
            prompt, NOTES_DIR, timeout=args.timeout
        )
    except subprocess.TimeoutExpired:
        print(f"ERROR: opencode timed out after {args.timeout}s", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: opencode failed: {e}", file=sys.stderr)
        return 1

    output = stdout.strip()
    if stderr:
        output += f"\n\n[stderr]\n{stderr}"

    print("\n" + "=" * 60)
    print(output[-8000:] if len(output) > 8000 else output)
    print("=" * 60)

    report_path = save_report(args.rule_id, output)
    print(f"\nReport saved to: {report_path}", flush=True)

    if not args.no_telegram:
        mode_emoji = "🔍" if args.mode == "scan" else "✅"
        summary = output
        if "## Summary" in output:
            summary = output.split("## Summary")[1].split("##")[0].strip()
        elif len(output) > 500:
            summary = output[-500:]

        telegram_msg = (
            f"{mode_emoji} Obsidian {args.mode}: {rule['name']}\n\n{summary[:1500]}"
        )
        send_telegram(telegram_msg)

    return returncode


if __name__ == "__main__":
    sys.exit(main())
