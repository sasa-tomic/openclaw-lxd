#!/usr/bin/env python3
"""Chat Followups Scanner

Scan messenger logs (Signal/WhatsApp/Telegram) modified in the last N minutes
and extract potential follow-ups / actions.

NOTE: read-only. Does not modify chat logs.
"""

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import NOTES_DIR as _NOTES_DIR

NOTES_DIR = Path(_NOTES_DIR)
MINUTES = int(os.environ.get("MINUTES", "60"))
OUT_NOTE = Path(
    os.environ.get("OUT_NOTE", str(NOTES_DIR / "Pickle" / "chat-followups.md"))
)
OUT_DIR = Path(os.environ.get("OUT_DIR", str(NOTES_DIR / "Pickle/chat-followups")))
MAX_FILES = int(os.environ.get("MAX_FILES", "20"))
TAIL_LINES = int(os.environ.get("TAIL_LINES", "120"))

# Patterns to detect actionable content
PATTERNS = [
    r"\b(i'll|i will|we should|todo|follow up|remind|deadline|due|by (tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|call|meeting|send|pay)\b",
    r"\b(decided|let's do|lets do|agreed|plan is|booked|scheduled|confirmed)\b",
    r"\b(conflict|contradict|actually|wait|no that's wrong|correction)\b",
]


def find_recent_chat_files() -> list[Path]:
    """Find recently modified chat files."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=MINUTES)
    files = []

    for chat_type in ["Signal", "WhatsApp", "Telegram"]:
        chat_dir = NOTES_DIR / chat_type
        if not chat_dir.exists():
            continue

        for path in chat_dir.rglob("*.md"):
            # Skip Reference/ and _reports/ subdirectories entirely
            if "Reference" in path.parts or "_reports" in path.parts:
                continue
                
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime)
                if mtime > cutoff:
                    files.append(path)
            except Exception:
                continue

    return sorted(files)[:MAX_FILES]


def scan_file(path: Path) -> list[str]:
    """Scan a file for matches, return matching lines with line numbers."""
    try:
        content = path.read_text()
        lines = content.strip().split("\n")[-TAIL_LINES:]
    except Exception:
        return []

    matches = []
    combined_pattern = "|".join(f"({p})" for p in PATTERNS)

    for i, line in enumerate(lines, 1):
        if re.search(combined_pattern, line, re.IGNORECASE):
            matches.append(f"L{i}: {line.strip()}")

    return matches[:8]


def main():
    files = find_recent_chat_files()

    if not files:
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sections = []
    found_any = False

    for path in files:
        rel_path = str(path.relative_to(NOTES_DIR))
        hits = scan_file(path)

        if hits:
            found_any = True
            section = f"\n- **{rel_path}**\n"
            for hit in hits:
                section += f"  - {hit}\n"
            sections.append(section)

    if not found_any:
        return 0

    # Write run note
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    run_note = OUT_DIR / f"{run_stamp}.md"

    content = f"""# Chat follow-ups — {stamp}

(Auto-generated from last {MINUTES} minutes of chat logs)

{"".join(sections)}
"""

    # Atomic write
    temp = run_note.with_suffix(".tmp")
    temp.write_text(content)
    temp.replace(run_note)

    # Update index
    if not OUT_NOTE.exists():
        OUT_NOTE.parent.mkdir(parents=True, exist_ok=True)
        OUT_NOTE.write_text("""# Chat follow-ups (index)

Auto-generated. Latest runs are appended below.

""")

    with open(OUT_NOTE, "a") as f:
        f.write(f"- [[Pickle/chat-followups/{run_stamp}|{run_stamp}]]\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
