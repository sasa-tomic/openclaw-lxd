#!/usr/bin/env python3
"""
WhatsApp to Obsidian Sync

Syncs WhatsApp chats from wacli to Obsidian markdown files.

Usage:
    python3 sync_to_obsidian.py

Environment variables:
    OBSIDIAN_WHATSAPP_DIR - Output directory (default: ~/clawd/notes/WhatsApp)
    WHATSAPP_STATE_FILE   - State file path (default: ~/.wacli/obsidian-sync-state.json)
    WACLI_PATH            - wacli binary path (default: ~/homebrew/bin/wacli)
"""

import fcntl
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# === Configuration ===

OBSIDIAN_DIR = Path(os.environ.get(
    "OBSIDIAN_WHATSAPP_DIR",
    os.path.expanduser("~/clawd/notes/WhatsApp")
))
STATE_FILE = Path(os.environ.get(
    "WHATSAPP_STATE_FILE",
    os.path.expanduser("~/.wacli/obsidian-sync-state.json")
))
WACLI = os.environ.get(
    "WACLI_PATH",
    os.path.expanduser("~/homebrew/bin/wacli")
)


# === CLI Wrapper ===

def wacli(*args: str) -> Optional[dict]:
    """Run wacli command and return JSON output."""
    cmd = [WACLI, *args, "--json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        print(f"WARN: wacli {' '.join(args)} failed: {e}", file=sys.stderr)
        return None


# === Data Types ===

@dataclass
class Message:
    timestamp: str  # ISO format from wacli
    sender_jid: str
    sender_name: str
    text: str
    media_type: Optional[str]
    from_me: bool

    @property
    def datetime(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None


# === Contacts Cache ===

class ContactsCache:
    def __init__(self):
        self._contacts: dict[str, str] = {}
        self._groups: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        # Load contacts
        data = wacli("contacts", "list", "--limit", "1000")
        if data:
            for contact in data.get("data", data) if isinstance(data, dict) else data:
                jid = contact.get("JID", "")
                name = contact.get("Name") or contact.get("PushName") or ""
                if jid and name:
                    self._contacts[jid] = name

        # Load groups
        data = wacli("groups", "list")
        if data:
            for group in data.get("data", data) if isinstance(data, dict) else data:
                jid = group.get("JID", "")
                name = group.get("Name", "")
                if jid and name:
                    self._groups[jid] = name

    def get_name(self, jid: str) -> str:
        if not jid:
            return "Unknown"

        # Check groups first
        if "@g.us" in jid and jid in self._groups:
            return self._groups[jid]

        # Check contacts
        if jid in self._contacts:
            return self._contacts[jid]

        # Try phone number prefix match
        phone = jid.split("@")[0]
        for contact_jid, name in self._contacts.items():
            if contact_jid.startswith(phone):
                return name

        # Fallback to formatted phone/JID
        if "@s.whatsapp.net" in jid:
            return f"+{phone}" if len(phone) > 8 else phone
        if "@g.us" in jid:
            return f"Group {jid}"
        return jid


# === State Management ===

class SyncState:
    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except json.JSONDecodeError:
            return {}

    def save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.rename(self._path)

    def get_last_ts(self, jid: str) -> Optional[str]:
        return self._data.get(jid, {}).get("ts")

    def update(self, jid: str, ts: str, name: str) -> None:
        self._data[jid] = {"ts": ts, "name": name}
        self.save()


# === File Operations ===

def sanitize_filename(name: str) -> str:
    if not name:
        return "Unknown"
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    result = re.sub(r'\s+', ' ', result).strip().strip('.')
    return result[:80] or "Unknown"


def format_time(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except (ValueError, AttributeError):
        return "??:??"


def format_date(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return "unknown"


def write_messages(path: Path, messages: list[str], chat_jid: str, chat_name: str, chat_type: str) -> None:
    """Write messages to file with locking."""
    # Create file with header if new
    if not path.exists():
        header = f"""---
type: whatsapp-{chat_type}
jid: {chat_jid}
name: {chat_name}
synced: {datetime.now().isoformat()}
---

# {chat_name}

"""
        path.write_text(header)

    # Append with locking
    content = "\n".join(messages)
    with path.open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(content)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# === Sync Logic ===

def sync_chat(jid: str, name: str, chat_type: str, state: SyncState, contacts: ContactsCache) -> int:
    """Sync a single chat. Returns number of new messages."""
    # Fetch messages
    data = wacli("messages", "list", "--chat", jid, "--limit", "500")
    if not data:
        return 0

    messages_data = data.get("data", {}).get("messages", [])
    if not messages_data:
        return 0

    # Sort by timestamp
    messages_data.sort(key=lambda m: m.get("Timestamp", ""))

    last_ts = state.get_last_ts(jid)
    newest_ts = None
    current_date = ""
    output_lines: list[str] = []
    new_count = 0

    for msg in messages_data:
        ts = msg.get("Timestamp", "")
        if not ts:
            continue

        # Skip already synced
        if last_ts and ts <= last_ts:
            continue

        newest_ts = ts

        # Sender
        if msg.get("FromMe"):
            sender = "Me"
        else:
            sender_jid = msg.get("SenderJID", "")
            sender = contacts.get_name(sender_jid)

        # Date header
        msg_date = format_date(ts)
        if msg_date != current_date:
            current_date = msg_date
            output_lines.append(f"\n## {msg_date}\n")

        # Content
        text = msg.get("DisplayText") or msg.get("Text") or ""
        media_type = msg.get("MediaType") or ""

        if media_type:
            content = f"*[{media_type}]* {text}".strip() if text else f"*[{media_type}]*"
        elif text:
            content = text
        else:
            content = "*(empty)*"

        # Format message
        msg_time = format_time(ts)
        output_lines.append(f"\n**{msg_time} - {sender}**")
        output_lines.append(content)
        output_lines.append("")
        new_count += 1

    if output_lines and newest_ts:
        # Determine output path
        base_dir = OBSIDIAN_DIR / ("Groups" if chat_type == "group" else "DMs")
        base_dir.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(name)
        path = base_dir / f"{safe_name}.md"

        write_messages(path, output_lines, jid, name, chat_type)
        state.update(jid, newest_ts, name)
        print(f"  ✓ {new_count} new messages", file=sys.stderr)

    return new_count


def main() -> int:
    print("WhatsApp → Obsidian Sync", file=sys.stderr)
    print("=" * 40, file=sys.stderr)
    print(f"Started: {datetime.now()}", file=sys.stderr)
    print("", file=sys.stderr)

    # Load caches
    print("Loading contacts & groups...", file=sys.stderr)
    contacts = ContactsCache()
    state = SyncState(STATE_FILE)

    # Get chats
    print("Fetching chats...", file=sys.stderr)
    data = wacli("chats", "list", "--limit", "200")
    if not data:
        print("ERROR: Could not fetch chats. Is wacli authenticated?", file=sys.stderr)
        return 1

    chats = data.get("data", data) if isinstance(data, dict) else data

    # Process chats
    print("", file=sys.stderr)
    print("Syncing chats:", file=sys.stderr)
    total_new = 0
    errors = 0

    for chat in chats:
        jid = chat.get("JID", "")

        # Skip special JIDs
        if not jid or jid == "status@broadcast" or "@lid" in jid:
            continue

        chat_type = "group" if "@g.us" in jid else "dm"
        name = chat.get("Name") or contacts.get_name(jid)

        print(f"- {name} ({chat_type})", file=sys.stderr)

        try:
            total_new += sync_chat(jid, name, chat_type, state, contacts)
        except Exception as e:
            print(f"  ⚠ Failed: {e}", file=sys.stderr)
            errors += 1

    print("", file=sys.stderr)
    print(f"Sync complete: {datetime.now()}", file=sys.stderr)
    print(f"Total: {total_new} new messages, {errors} errors", file=sys.stderr)

    return 1 if errors > 10 else 0


if __name__ == "__main__":
    sys.exit(main())
