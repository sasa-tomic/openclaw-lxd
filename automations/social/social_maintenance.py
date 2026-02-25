#!/usr/bin/env python3
"""
Social Connection Maintenance - Extract personal details from messenger archives.

Uses opencode CLI to process specific contacts:
1. Python identifies contacts needing attention
2. opencode extracts details from chats and writes People/ profiles
3. opencode drafts reconnection messages and adds to TODO.md

Usage:
    python3 social_maintenance.py [--dry-run]

State file: /projects/Notes/Pickle/social-maintenance-state.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import (
    NOTES_DIR as NOTES_DIR_STR,
    OPENCLAW_BIN,
    OPENCODE_BIN,
    TELEGRAM_TARGET,
)
from lib.state_utils import load_state, save_state
from lib.telegram_utils import send_telegram

NOTES_DIR = Path(NOTES_DIR_STR)
PEOPLE_DIR = NOTES_DIR / "People"
SIGNAL_DMS = NOTES_DIR / "Signal" / "DMs"
SIGNAL_GROUPS = NOTES_DIR / "Signal" / "Groups"
WHATSAPP_DMS = NOTES_DIR / "WhatsApp" / "DMs"
WHATSAPP_GROUPS = NOTES_DIR / "WhatsApp" / "Groups"
PICKLE_DIR = NOTES_DIR / "Pickle"
STATE_FILE = PICKLE_DIR / "social-maintenance-state.json"
TODO_FILE = NOTES_DIR / "TODO.md"

RECONNECT_INTERVAL_DAYS = 45
EXTRACT_INTERVAL_DAYS = 30
MAX_CONTACTS_PER_RUN = 2

DEFAULT_STATE = {"contacts": {}, "last_run": None}


def get_chat_files() -> list[tuple[Path, str]]:
    """Get all chat files with their platform."""
    files = []
    for p in SIGNAL_DMS.glob("*.md"):
        files.append((p, "Signal"))
    for p in WHATSAPP_DMS.glob("*.md"):
        files.append((p, "WhatsApp"))
    return files


def extract_name_from_chat(
    content: str, filepath: Path
) -> tuple[str | None, str | None]:
    """Extract name and linked People file from chat content or filename.

    Returns (name, people_file_relative_path) or (None, None).
    """
    people_file = None

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]
            for line in fm.split("\n"):
                if line.startswith("name:"):
                    pass  # We'll extract from heading instead
                elif line.startswith("people_file:"):
                    people_file = line.split(":", 1)[1].strip().strip("\"'")

    for heading in re.findall(r"^#\s+(.+)$", content, re.MULTILINE)[:1]:
        if heading and not heading.startswith("+"):
            return heading.strip(), people_file

    name = filepath.stem
    if name.startswith("+"):
        return None, None
    return name, people_file


def normalize_name(name: str, state: dict) -> str:
    """Normalize name to match existing People/ file or state entry."""
    if not name:
        return name

    for existing_name in state.get("contacts", {}).keys():
        if name.lower() == existing_name.lower():
            return existing_name
        existing_first = existing_name.split()[0] if existing_name else ""
        if existing_first and name.lower() == existing_first.lower():
            return existing_name

    for p in PEOPLE_DIR.glob("*.md"):
        pname = p.stem
        if name.lower() == pname.lower():
            return pname
        pfirst = pname.split()[0] if pname else ""
        if pfirst and name.lower() == pfirst.lower():
            return pname

    return name


def get_people_file(name: str) -> Path | None:
    """Find existing People/ file for a contact."""
    for p in PEOPLE_DIR.glob("*.md"):
        if p.stem.lower() == name.lower():
            return p
        first_name = name.split()[0] if name else ""
        if first_name and len(first_name) >= 3:
            if p.stem.lower().startswith(first_name.lower()):
                return p
    return None


def identify_contacts_to_process(state: dict) -> list[dict]:
    """Identify which contacts need processing."""
    now = datetime.now(timezone.utc)
    candidates = []

    for chat_path, platform in get_chat_files():
        content = chat_path.read_text()
        name, linked_people_file = extract_name_from_chat(content, chat_path)

        if not name:
            continue

        from contact_utils import is_valid_contact_name

        if not is_valid_contact_name(name):
            continue

        if linked_people_file:
            linked_name = Path(linked_people_file).stem
            name = linked_name
        else:
            name = normalize_name(name, state)

        if linked_people_file:
            people_file = PEOPLE_DIR / linked_people_file
            if not people_file.exists():
                people_file = None
        else:
            people_file = get_people_file(name)
        contact_state = state.get("contacts", {}).get(name, {})

        last_extracted = contact_state.get("last_extracted")
        last_reconnect = contact_state.get("last_reconnect")

        need_extract = True
        need_reconnect = people_file is not None

        if last_extracted:
            try:
                dt = datetime.fromisoformat(last_extracted.replace("Z", "+00:00"))
                if now - dt < timedelta(days=EXTRACT_INTERVAL_DAYS):
                    need_extract = False
            except (ValueError, TypeError):
                pass

        if last_reconnect:
            try:
                dt = datetime.fromisoformat(last_reconnect.replace("Z", "+00:00"))
                if now - dt < timedelta(days=RECONNECT_INTERVAL_DAYS):
                    need_reconnect = False
            except (ValueError, TypeError):
                pass

        if need_extract or need_reconnect:
            existing_content = ""
            if people_file and people_file.exists():
                existing_content = people_file.read_text()

            candidates.append(
                {
                    "name": name,
                    "chat_file": str(chat_path.relative_to(NOTES_DIR)),
                    "people_file": str(people_file.relative_to(NOTES_DIR))
                    if people_file
                    else None,
                    "platform": platform,
                    "need_extract": need_extract,
                    "need_reconnect": need_reconnect,
                    "existing_profile": existing_content[:3000]
                    if existing_content
                    else None,
                }
            )

    new_contacts = [c for c in candidates if c["need_extract"] and not c["people_file"]]
    existing_needs_extract = [
        c for c in candidates if c["need_extract"] and c["people_file"]
    ]
    needs_reconnect = [
        c for c in candidates if not c["need_extract"] and c["need_reconnect"]
    ]

    prioritized = new_contacts + existing_needs_extract + needs_reconnect
    return prioritized[:MAX_CONTACTS_PER_RUN]


def build_process_prompt(contacts: list[dict], dry_run: bool) -> str:
    """Build prompt for processing specific contacts."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    contacts_json = json.dumps(contacts, indent=2)

    prompt = f"""You are Pickle, the social connection maintenance assistant for Saša Tomić (Mr. T).

# Today's Date
{date_str}

# Your Task

Process the following {len(contacts)} contact(s) and:
1. Extract personal details from their chat history
2. Create/update their People/ profile
3. If needing reconnection, draft a message and add to TODO.md

# Contacts to Process

Each contact includes:
- `name`: Contact name
- `chat_file`: Path to their DM history
- `people_file`: Path to their People/ profile (null if new)
- `existing_profile`: Current profile content (null if new contact) - MERGE THIS, don't replace
- `need_extract`: Whether to extract new details
- `need_reconnect`: Whether to draft a reconnection message

```json
{contacts_json}
```

# Directories

- DMs: Signal/DMs/ and WhatsApp/DMs/
- Groups: Signal/Groups/ and WhatsApp/Groups/
- People profiles: People/
- TODO file: TODO.md
- State file: {str(STATE_FILE.relative_to(NOTES_DIR))}

# Extraction Rules

For each contact:

1. **Read their DM file** from the path in `chat_file`

2. **Scan group chats** for additional context:
   - Search Signal/Groups/*.md for messages from/to this contact
   - Search WhatsApp/Groups/*.md for their name/number
   - Groups often reveal: professional context, mutual contacts, interests, life events

3. **Extract EVERYTHING personal:**
   - Personal: name, nicknames, location, family (partner, kids, pets), birthday, languages
   - Professional: job, company, role, projects, skills, career info
   - Interests: hobbies, sports, music, travel, food
   - Important Dates: birthdays, anniversaries, upcoming events
   - Life Events: recent events, challenges, changes
   - Relationship: how you know each other, mutual contacts
   - Follow-ups: things to ask about later

4. **Merge with existing profile:**
   - If `existing_profile` is provided, you MUST preserve ALL existing information
   - Add new findings alongside existing - never delete content
   - Merge duplicates (same info stated twice = keep once)
   - Update stale info with newer data from chats
   - Reorganize structure for consistency, but keep ALL facts
   - Update the "Last updated" date at the bottom

5. **Write People/{{Name}}.md:**

```markdown
# {{Name}}

{{2-3 sentence summary}}

## Personal
- **Location:** ...
- **Family:** ...

## Professional
- **Role:** ...

## Interests
- ...

## Important Dates
- {{date}}: {{event}} (annual)

## Relationship
- **Context:** ...

## Follow-ups
- [ ] {{thing to ask about}}

## Conversation Starters
- {{topics they care about}}

---
*Last updated: {date_str}*
```

4. **Update chat file frontmatter** with `people_extracted: {date_str}`

# Reconnection Messages

For contacts where `need_reconnect: true`:

1. Read their People/ profile and recent chat
2. Draft a NATURAL 1-2 sentence message that:
   - References something specific from their profile/last chat
   - Sounds like Mr. T (casual, lowercase, contractions)
   - Uses Serbian for Serbian contacts
   - NO generic "just checking in"

3. Add to TODO.md under "## Social":
```markdown
- [ ] **Reconnect:** {{Name}} — _"{{message}}"_ → [{{file}}](People/{{file}})
```

# State File

After processing, update {str(STATE_FILE.relative_to(NOTES_DIR))}:
```json
{{
  "contacts": {{
    "{{name}}": {{
      "last_extracted": "{{ISO timestamp}}",
      "last_reconnect": "{{ISO timestamp if reconnect drafted}}",
      "people_file": "{{filename}}.md",
      "platform": "{{platform}}"
    }}
  }},
  "last_run": "{{ISO timestamp}}"
}}
```

"""

    if dry_run:
        prompt += """# DRY RUN
Do NOT write files. Report what you WOULD do.
"""

    prompt += """# Output Format

End with:
```
## Summary
<Brief overview>

## Processed
- {{Name}}: {{what was done}}

## Drafted Messages
- {{Name}}: "{{message}}"
```"""

    return prompt


def run_opencode(
    prompt: str, workdir: Path, timeout: int = 180
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Social connection maintenance")
    parser.add_argument("--dry-run", action="store_true", help="Don't write changes")
    parser.add_argument("--timeout", type=int, default=180, help="Timeout in seconds")
    parser.add_argument(
        "--no-telegram", action="store_true", help="Skip Telegram notification"
    )
    args = parser.parse_args()

    print("=== SOCIAL MAINTENANCE ===")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    if args.dry_run:
        print("DRY RUN - no changes will be made")

    if not Path(OPENCODE_BIN).exists():
        print(f"ERROR: opencode not found at {OPENCODE_BIN}", file=sys.stderr)
        return 1

    state = load_state(STATE_FILE, DEFAULT_STATE)

    contacts = identify_contacts_to_process(state)

    if not contacts:
        print("No contacts need processing")
        return 0

    print(f"Processing {len(contacts)} contact(s):")
    for c in contacts:
        print(f"  - {c['name']} ({c['platform']})")

    prompt = build_process_prompt(contacts, args.dry_run)

    print(f"\nRunning opencode with {args.timeout}s timeout...")

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
    print(output[-4000:] if len(output) > 4000 else output)
    print("=" * 60)

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    if not args.dry_run:
        save_state(STATE_FILE, state)

    if not args.no_telegram:
        summary = output
        if "## Summary" in output:
            parts = output.split("## Summary")
            if len(parts) > 1:
                summary = parts[1].split("##")[0].strip()
        elif len(output) > 400:
            summary = output[-400:]

        emoji = "🔍" if args.dry_run else "👥"
        telegram_msg = f"{emoji} Social Maintenance\n\n{summary[:1200]}"
        send_telegram(telegram_msg)

    return returncode


if __name__ == "__main__":
    sys.exit(main())
