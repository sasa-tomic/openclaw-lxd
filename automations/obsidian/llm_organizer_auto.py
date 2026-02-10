#!/usr/bin/env python3
"""One-pass LLM organizer for the Obsidian vault (apply + report).

Behavior:
- LLM inspects recently modified notes and the current folder structure.
- It is allowed to reorganize text AND move/rename/split notes when it improves organization.
- It must NOT delete content or files.

Outputs:
- Writes a visible report: /projects/Notes/Pickle/organization-report.md
- Appends a changelog entry: /projects/Notes/.organization/changelog.md

Hard safety constraints:
- Do NOT touch: WhatsApp/, Signal/, Telegram/, Daily/, .obsidian/, .trash/, .organization/
- No deletions.

This script just runs an OpenClaw agent turn that performs the work.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone

from pathlib import Path

DATE_STR = datetime.now(timezone.utc).strftime("%Y-%m-%d")
REPORT_PATH = f"/projects/Notes/Pickle/{DATE_STR}-organization-report.md"
CHANGELOG_PATH = f"/projects/Notes/Pickle/{DATE_STR}-organization-changelog.md"
INDEX_PATH = "/projects/Notes/Pickle/organization.md"

AGENT_MESSAGE = f"""You are an Obsidian vault organizer. Do a single-pass organize+apply.

IMPORTANT: Obsidian users want date-prefixed organization artifacts.
Use these exact paths for this run:
- Report: {REPORT_PATH}
- Changelog: {CHANGELOG_PATH}
- Index: {INDEX_PATH}

Rolling-queue principle:
- Prefer tracking small in-flight tasks ONLY via the `## Rolling queue (open items)` checkbox list in the daily report.
- Do not create separate TODO notes for organizer work; use the rolling queue.

Goal:
- Improve organization by restructuring text and moving/renaming notes where appropriate.

Hard constraints:
- NEVER modify anything under: WhatsApp/, Signal/, Telegram/, Daily/, .obsidian/, .trash/, .organization/
- NEVER delete content or files. No rm. If something should be removed from an active place, move it to Archive/.

Scope control:
- Focus on recently modified notes (last ~21 days) and obvious misplacements.
- Cap work per run: at most 10 concrete changes (move/rename/split/edit/link).
- Avoid churn: don’t rename unless it clearly improves findability.

Required reporting (use THIS EXACT MODEL):

1) **Daily rolling queue report** at: {REPORT_PATH}
   - Title must start with the date, e.g. `# {DATE_STR} — Organization report (rolling queue)`
   - Sections, in this order:
     A) `## Rolling queue (open items)`
        - Put small in-flight tasks here as checkboxes: `- [ ] <task>`.
        - Keep tasks tiny and actionable ("fix producer truncation", "add link X", "rename note Y").
        - When a task is done, REMOVE it from this section (don’t keep stale open tasks).
        - If empty, write exactly: `- [ ] (none right now)`.
     B) `## Completed / changed today`
        - Bullet list of what you changed (include before/after paths for moves/renames).
     C) `## Skipped`
        - Bullet list of things you decided not to do + 1-line why.
     D) `## Follow-ups / suspected automation bugs`
        - Only real issues. If fixed, mark them as fixed.

2) **Daily changelog (rolling)** at: {CHANGELOG_PATH}
   - Title must start with the date, e.g. `# {DATE_STR} — Organization changelog (rolling)`
   - Append-only bullets; keep them terse.
   - Include moves/renames/splits/edits.

3) **Index** at: {INDEX_PATH}
   - Keep stable sections:
     - `## Today` → link to today's report + changelog.
     - `## Last 7 days` → link reports for the last 7 days (if files exist).
     - `## Snapshots / legacy` (keep existing items).

Notes:
- Splitting notes is allowed.
- Moving text between files is allowed.
- Moves/renames are allowed.
- No deletes.

Output:
- Print a concise summary of changes applied.
- Do NOT call the message tool.
"""


def main() -> int:
    print("=== OBSIDIAN LLM ORGANIZER (ONE-PASS APPLY) ===")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")

    cmd = [
        "openclaw",
        "agent",
        "--agent",
        "main",
        "--message",
        AGENT_MESSAGE,
        "--timeout",
        "1200",
        "--json",
    ]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1240)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        return r.returncode

    print(r.stdout[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
