#!/usr/bin/env python3
"""Apply LLM organization suggestions to the Obsidian vault.

DEPRECATED: replaced by llm_organizer_auto.py (one-pass apply).
Kept here for reference.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone

QUEUE_PATH = "/projects/Notes/Pickle/organization-report.md"

AGENT_MESSAGE = f"""You are operating as an Obsidian vault organizer.

Task: APPLY safe organization improvements directly to the vault.

Default behavior preference:
- Automatically do in-place improvements only (reword/restructure within the same file, add headings, add links).
- Creating new files via split is OK (but keep in the same folder unless obviously a better existing folder).
- DO NOT move/rename files automatically. Instead: write a note in the report + changelog and 'ping' the user in the summary output.

Input queue:
- {QUEUE_PATH}

Instructions:
1) Read the queue file and interpret the suggestions.
2) Apply improvements directly (move/rename/split/link) where it clearly improves organization.
3) Hard constraints:
   - NEVER modify anything under: WhatsApp/, Signal/, Telegram/, Daily/, .obsidian/, .trash/, .organization/
   - NEVER delete content. No rm. If you must remove something, move to Archive/.
   - Avoid churn: don’t rename just for style.
   - DO NOT move/rename files automatically.
4) Cap the amount of work: apply at most 6 concrete changes per run.
5) Record what you did:
   - Append a short bullet list to /projects/Notes/.organization/changelog.md under today’s date.
   - Include source paths and target paths.

If a suggestion is ambiguous, skip it and leave a TODO in the queue note (do not ask the user).

Output:
- Print a concise summary of changes applied.
- Do not use the message tool.
"""


def main() -> int:
    print("=== OBSIDIAN LLM ORGANIZER (APPLY) ===")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")

    cmd = [
        "openclaw",
        "agent",
        "--agent",
        "main",
        "--message",
        AGENT_MESSAGE,
        "--timeout",
        "900",
        "--json",
    ]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=920)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        return r.returncode

    # Print a short tail of the agent's summary.
    print(r.stdout[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
