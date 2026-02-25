#!/usr/bin/env python3
"""
Social Connection Maintenance - Batch Runner.

Processes all contacts in parallel using multiple opencode instances.
Each instance handles 1 contact for speed.

Usage:
    python3 social_batch.py [--workers N] [--dry-run]

Default workers: 3 (parallel opencode instances)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.state_utils import load_state, save_state

NOTES_DIR = Path("/projects/Notes")
PEOPLE_DIR = NOTES_DIR / "People"
SIGNAL_DMS = NOTES_DIR / "Signal" / "DMs"
SIGNAL_GROUPS = NOTES_DIR / "Signal" / "Groups"
WHATSAPP_DMS = NOTES_DIR / "WhatsApp" / "DMs"
WHATSAPP_GROUPS = NOTES_DIR / "WhatsApp" / "Groups"
PICKLE_DIR = NOTES_DIR / "Pickle"
STATE_FILE = PICKLE_DIR / "social-maintenance-state.json"
TODO_FILE = NOTES_DIR / "TODO.md"
OPENCODE_BIN = "/home/openclaw/.opencode/bin/opencode"

DEFAULT_WORKERS = 3
OPENCODE_TIMEOUT = 180

DEFAULT_STATE = {"contacts": {}, "last_run": None}


def merge_state_updates(state: dict, updates: list[dict]) -> None:
    """Merge updates from parallel workers into main state."""
    for update in updates:
        if not update:
            continue
        for name, data in update.get("contacts", {}).items():
            if name not in state["contacts"]:
                state["contacts"][name] = {}
            state["contacts"][name].update(data)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(STATE_FILE, state)


def get_all_contacts(state: dict) -> list[dict]:
    """Get ALL contacts that need processing (no limit)."""
    from social_maintenance import (
        get_chat_files,
        extract_name_from_chat,
        get_people_file,
    )
    from datetime import timedelta

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
            name = Path(linked_people_file).stem
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
                if now - dt < timedelta(days=30):
                    need_extract = False
            except (ValueError, TypeError):
                pass

        if last_reconnect:
            try:
                dt = datetime.fromisoformat(last_reconnect.replace("Z", "+00:00"))
                if now - dt < timedelta(days=45):
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
                    "existing_profile": existing_content[:4000]
                    if existing_content
                    else None,
                }
            )

    new_first = sorted(
        candidates, key=lambda c: (c["people_file"] is not None, c["name"])
    )
    return new_first


def build_single_contact_prompt(contact: dict, dry_run: bool) -> str:
    """Build prompt for processing a single contact."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = f"""You are Pickle, the social connection maintenance assistant for Saša Tomić (Mr. T).

# Today's Date
{date_str}

# Your Task

Process ONE contact and:
1. Extract personal details from their chat history (DM + groups)
2. Create/update their People/ profile
3. Draft reconnection message if needed

# Contact to Process

```json
{json.dumps(contact, indent=2)}
```

# Directories

- DMs: Signal/DMs/ and WhatsApp/DMs/
- Groups: Signal/Groups/ and WhatsApp/Groups/
- People profiles: People/
- TODO file: TODO.md
- State file: {str(STATE_FILE.relative_to(NOTES_DIR))}

# Extraction Steps

1. **Read DM file** from `chat_file` path

2. **Scan group chats** for this person:
   - Grep Signal/Groups/*.md for their name
   - Grep WhatsApp/Groups/*.md for their name/number
   - Groups reveal: professional context, mutual contacts, interests

3. **Extract personal details:**
   - Personal: name, location, family, birthday, languages
   - Professional: job, company, projects, skills
   - Interests: hobbies, sports, travel
   - Important Dates: birthdays, anniversaries
   - Relationship: how you know them, mutual contacts
   - Follow-ups: things to ask about

4. **MERGE with existing profile:**
   - If `existing_profile` is provided, READ IT and PRESERVE ALL content
   - Add new findings, never delete existing info
   - Merge duplicates, update stale info
   - Reorganize for consistency but keep all facts

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

6. **Update DM file frontmatter** with:
   - `people_extracted: {date_str}`
   - `people_file: {{Name}}.md`

# Reconnection Message

If `need_reconnect: true`:
1. Read their People/ profile and recent chat
2. Draft 1-2 sentence message:
   - Reference something specific from profile/chat
   - Casual, lowercase, contractions
   - Serbian for Serbian contacts
   - NO generic "just checking in"
3. Add to TODO.md under "## Social":
   ```markdown
   - [ ] **Reconnect:** {{Name}} — _"{{message}}"_ → [{{file}}](People/{{file}})
   ```

# State Update

Append to {str(STATE_FILE.relative_to(NOTES_DIR))}:
```json
{{"contacts": {{"{contact["name"]}": {{
  "last_extracted": "{{ISO timestamp}}",
  "last_reconnect": "{{ISO timestamp if drafted}}",
  "people_file": "{{filename}}.md",
  "platform": "{contact["platform"]}"
}}}}}}
```

"""

    if dry_run:
        prompt += """# DRY RUN
Do NOT write files. Report what you WOULD do.
"""

    prompt += """# Output

End with a brief summary:
```
## Done
- Profile: created/updated {{Name}}.md
- Reconnect: {{message or "not needed"}}
```"""

    return prompt


def process_single_contact(contact: dict, dry_run: bool) -> dict | None:
    """Process one contact with opencode. Returns state update."""
    name = contact["name"]
    print(f"  [{contact['platform']}] {name}...", flush=True)

    prompt = build_single_contact_prompt(contact, dry_run)

    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"

    try:
        result = subprocess.run(
            [OPENCODE_BIN, "run", prompt, "--dir", str(NOTES_DIR)],
            capture_output=True,
            text=True,
            timeout=OPENCODE_TIMEOUT,
            env=env,
        )

        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"

        if result.returncode == 0:
            print(f"    ✅ {name}", flush=True)
            return {
                "contacts": {
                    name: {
                        "last_extracted": datetime.now(timezone.utc).isoformat(),
                        "last_reconnect": datetime.now(timezone.utc).isoformat()
                        if contact.get("need_reconnect")
                        else None,
                        "people_file": f"{name}.md",
                        "platform": contact["platform"],
                    }
                }
            }
        else:
            print(f"    ❌ {name} (exit {result.returncode})", flush=True)
            return None

    except subprocess.TimeoutExpired:
        print(f"    ⏱️ {name} (timeout)", flush=True)
        return None
    except Exception as e:
        print(f"    ❌ {name} ({e})", flush=True)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Social maintenance batch runner")
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS, help="Parallel workers"
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't write changes")
    parser.add_argument("--limit", type=int, help="Limit number of contacts to process")
    args = parser.parse_args()

    print("=== SOCIAL MAINTENANCE (BATCH) ===")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print(f"Workers: {args.workers}")
    if args.dry_run:
        print("DRY RUN - no changes will be made")

    if not Path(OPENCODE_BIN).exists():
        print(f"ERROR: opencode not found at {OPENCODE_BIN}", file=sys.stderr)
        return 1

    state = load_state(STATE_FILE, DEFAULT_STATE)
    contacts = get_all_contacts(state)

    if args.limit:
        contacts = contacts[: args.limit]

    if not contacts:
        print("No contacts need processing")
        return 0

    print(f"\nProcessing {len(contacts)} contacts with {args.workers} workers...\n")

    state_updates = []
    completed = 0
    failed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_single_contact, c, args.dry_run): c
            for c in contacts
        }

        for future in concurrent.futures.as_completed(futures):
            contact = futures[future]
            try:
                update = future.result()
                if update:
                    state_updates.append(update)
                    completed += 1
                else:
                    failed += 1
            except Exception as e:
                print(f"    ❌ {contact['name']} (exception: {e})", flush=True)
                failed += 1

    print(f"\n{'=' * 50}")
    print(f"Completed: {completed}")
    print(f"Failed: {failed}")

    if state_updates and not args.dry_run:
        merge_state_updates(state, state_updates)
        print(f"State updated with {len(state_updates)} contacts")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
