#!/usr/bin/env python3
"""Obsidian Note Review Script

Runs every 30-60 minutes to review recently modified notes.
Outputs observations to /tmp/obsidian-note-review-results.txt for the cron wrapper.
"""

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.state_utils import load_state, save_state

NOTES_DIR = Path("/projects/Notes")
MEMORY_DIR = Path("/home/openclaw/clawd/memory")
STATE_FILE = MEMORY_DIR / "obsidian-note-review-state.json"
RESULTS_FILE = Path("/tmp/obsidian-note-review-results.txt")

DEFAULT_STATE = {"lastCheck": None}


def find_recently_modified_notes(minutes: int = 60) -> list[str]:
    """Find notes modified in the last N minutes. Returns relative paths."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    notes = []

    for path in NOTES_DIR.rglob("*.md"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            if mtime > cutoff:
                rel_path = str(path.relative_to(NOTES_DIR))
                notes.append(rel_path)
        except Exception:
            continue

    return sorted(notes)


def check_patterns(content: str, patterns: list[tuple[str, str]]) -> list[str]:
    """Check content against patterns and return matching observations."""
    matches = []
    for pattern, observation in patterns:
        if re.search(pattern, content, re.IGNORECASE):
            matches.append(observation)
    return matches


def review_note(rel_path: str) -> list[str]:
    """Review a single note and return observations."""
    full_path = NOTES_DIR / rel_path

    # Skip chat logs
    if any(skip in rel_path for skip in ["Signal/", "WhatsApp/", "Telegram/"]):
        return ["Chat log - skipping (handled elsewhere)"]

    try:
        content = full_path.read_text()
    except Exception as e:
        return [f"Could not read file: {e}"]

    patterns = [
        (
            r"\b(TODO|FIXME|REMEMBER|don't forget)\b",
            "📝 Contains TODO markers that might need extraction",
        ),
        (
            r"\bby\s+(tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|deadline|due date|meeting|call",
            "⏰ May contain time-sensitive items",
        ),
        (r"\?\?|\.\.\.|\[todo\]|\[check\]|unclear", "❓ May need clarification"),
        (r"\b(voki|decent cloud|axiom)\b", "🔗 Relates to active project"),
    ]

    return check_patterns(content, patterns)


def main():
    print("=== OBSIDIAN NOTE REVIEW ===")

    state = load_state(STATE_FILE, DEFAULT_STATE)

    # Find recently modified notes
    changed_files = find_recently_modified_notes(60)

    if not changed_files:
        print("No changes in last 60 minutes.")
        state["lastCheck"] = datetime.now(timezone.utc).isoformat()
        save_state(STATE_FILE, state)
        return 0

    print("Found changes in:")
    for f in changed_files:
        print(f"  {f}")
    print()

    observations = []

    for rel_path in changed_files:
        print(f"---")
        print(f"Reviewing: {rel_path}")

        note_observations = review_note(rel_path)

        for obs in note_observations:
            if "skipping" in obs.lower():
                print(f"  {obs}")
            else:
                observations.append(f"📝 {rel_path}: {obs}")

    # Update state
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    save_state(STATE_FILE, state)

    # Write results for cron wrapper
    if observations:
        print()
        print("Observations to report:")
        for obs in observations:
            print(f"  {obs}")

        RESULTS_FILE.write_text("OBSERVATIONS:" + "\n".join(observations))
    else:
        print("No significant observations.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
