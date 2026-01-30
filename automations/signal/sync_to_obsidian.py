#!/usr/bin/env python3
"""
Signal to Obsidian Live Sync

Reads JSON messages from signal-cli and appends them to Obsidian markdown files.

Usage:
    signal-cli -o json receive -t 60 | python3 sync_to_obsidian.py

Environment variables:
    OBSIDIAN_SIGNAL_DIR  - Output directory (default: ~/clawd/notes/Signal)
    SIGNAL_STATE_FILE    - State file path (default: ~/.signal-cli/obsidian-sync-state.json)
    SIGNAL_CONTACTS_FILE - Contacts cache (default: ~/.signal-cli/contacts.json)

Format matches signal-export:
    [YYYY-MM-DD HH:MM:SS] Sender: Message text
"""

import fcntl
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# === Configuration ===

OBSIDIAN_DIR = Path(os.environ.get(
    "OBSIDIAN_SIGNAL_DIR",
    os.path.expanduser("~/clawd/notes/Signal")
))
STATE_FILE = Path(os.environ.get(
    "SIGNAL_STATE_FILE",
    os.path.expanduser("~/.signal-cli/obsidian-sync-state.json")
))
CONTACTS_FILE = Path(os.environ.get(
    "SIGNAL_CONTACTS_FILE",
    os.path.expanduser("~/.signal-cli/contacts.json")
))


# === Data Types ===

@dataclass
class Message:
    timestamp: datetime
    sender_number: str
    sender_name: str
    text: str
    group_id: Optional[str]
    group_name: Optional[str]
    attachments: list[str]
    is_outgoing: bool

    @property
    def is_group(self) -> bool:
        return self.group_id is not None

    @property
    def chat_id(self) -> str:
        if self.is_group:
            return self.group_id or "unknown_group"
        return self.sender_number or "unknown"

    @property
    def display_sender(self) -> str:
        return "Me" if self.is_outgoing else (self.sender_name or self.sender_number or "Unknown")


# === Contacts Cache ===

class ContactsCache:
    def __init__(self, path: Path):
        self._path = path
        self._cache: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            for contact in data:
                number = contact.get("number", "")
                name = contact.get("name") or contact.get("profileName") or ""
                if number and name:
                    self._cache[number] = name
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"WARN: Failed to load contacts: {e}", file=sys.stderr)

    def get_name(self, number: str) -> str:
        if not number:
            return "Unknown"
        return self._cache.get(number, number)

    def update(self, number: str, name: str) -> None:
        if number and name and name != number:
            self._cache[number] = name


# === State Management ===

class SyncState:
    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if not self._path.exists():
            return {"chats": {}}
        try:
            return json.loads(self._path.read_text())
        except json.JSONDecodeError as e:
            print(f"WARN: Failed to load state, starting fresh: {e}", file=sys.stderr)
            return {"chats": {}}

    def save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.rename(self._path)  # Atomic replace

    def get_last_timestamp(self, chat_id: str) -> Optional[int]:
        return self._data.get("chats", {}).get(chat_id, {}).get("ts")

    def update(self, chat_id: str, timestamp: int, name: str) -> None:
        if "chats" not in self._data:
            self._data["chats"] = {}
        self._data["chats"][chat_id] = {"ts": timestamp, "name": name}
        self.save()


# === File Operations ===

def sanitize_filename(name: str) -> str:
    """Convert name to safe filename."""
    if not name:
        return "Unknown"
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    result = result.strip().strip('.')
    return result[:80] or "Unknown"


def find_chat_file(msg: Message, contacts: ContactsCache) -> Path:
    """Find or create the appropriate chat file."""
    base_dir = OBSIDIAN_DIR / ("Groups" if msg.is_group else "DMs")
    base_dir.mkdir(parents=True, exist_ok=True)

    if msg.is_group:
        search_name = msg.group_name or msg.group_id or "UnknownGroup"
    else:
        search_name = contacts.get_name(msg.sender_number)

    safe_name = sanitize_filename(search_name)
    target = base_dir / f"{safe_name}.md"

    if target.exists():
        return target

    # Try partial match (first name only)
    first_name = search_name.split()[0] if search_name else ""
    if first_name and len(first_name) >= 2:
        for existing in base_dir.glob("*.md"):
            if existing.stem.lower().startswith(first_name.lower()):
                return existing

    return target


def write_message_atomic(path: Path, msg: Message, contacts: ContactsCache) -> None:
    """Append message to file with locking."""
    ts = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    sender = msg.display_sender
    content = msg.text or ""

    if msg.attachments:
        att_text = " ".join(f"[{a}]" for a in msg.attachments)
        content = f"{att_text}  {content}".strip() if content else att_text

    if not content:
        return

    line = f"\n[{ts}] {sender}: {content}  \n"

    # Create file with header if new
    if not path.exists():
        chat_name = msg.group_name if msg.is_group else contacts.get_name(msg.sender_number)
        header = f"# {chat_name}\n\n_Signal chat - live sync via signal-cli_\n\n---\n"
        path.write_text(header)

    # Append with file locking
    with path.open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# === Message Parser ===

def parse_envelope(data: dict, contacts: ContactsCache) -> Optional[Message]:
    """Parse a signal-cli JSON envelope."""
    envelope = data.get("envelope")
    if not envelope:
        return None

    sender_number = envelope.get("sourceNumber") or envelope.get("source") or ""
    sender_name = envelope.get("sourceName") or ""
    my_account = data.get("account", "")

    if sender_name and sender_number:
        contacts.update(sender_number, sender_name)

    # Incoming data message
    dm = envelope.get("dataMessage")
    if dm:
        timestamp_ms = dm.get("timestamp") or envelope.get("timestamp") or 0
        if timestamp_ms < 1000000000000:  # Sanity check: must be after year 2001
            return None

        text = dm.get("message") or ""
        group_info = dm.get("groupInfo") or dm.get("groupV2")
        group_id = group_info.get("groupId") if group_info else None
        group_name = (group_info.get("groupName") or group_info.get("title")) if group_info else None
        attachments = [a.get("contentType", "file").split("/")[0] for a in dm.get("attachments", [])]

        return Message(
            timestamp=datetime.fromtimestamp(timestamp_ms / 1000),
            sender_number=sender_number,
            sender_name=contacts.get_name(sender_number),
            text=text,
            group_id=group_id,
            group_name=group_name,
            attachments=attachments,
            is_outgoing=(sender_number == my_account),
        )

    # Outgoing sync message
    sync = envelope.get("syncMessage", {})
    sent = sync.get("sentMessage")
    if sent:
        timestamp_ms = sent.get("timestamp") or 0
        if timestamp_ms < 1000000000000:
            return None

        text = sent.get("message") or ""
        dest_number = sent.get("destinationNumber") or sent.get("destination") or ""
        group_info = sent.get("groupInfo") or sent.get("groupV2")
        group_id = group_info.get("groupId") if group_info else None
        group_name = (group_info.get("groupName") or group_info.get("title")) if group_info else None
        attachments = [a.get("contentType", "file").split("/")[0] for a in sent.get("attachments", [])]

        return Message(
            timestamp=datetime.fromtimestamp(timestamp_ms / 1000),
            sender_number=dest_number,
            sender_name=contacts.get_name(dest_number),
            text=text,
            group_id=group_id,
            group_name=group_name,
            attachments=attachments,
            is_outgoing=True,
        )

    return None


# === Main ===

def process_line(line: str, state: SyncState, contacts: ContactsCache) -> bool:
    """Process a single JSON line. Returns True if message was written."""
    data = json.loads(line)
    msg = parse_envelope(data, contacts)

    if not msg or not msg.text:
        return False

    msg_ts = int(msg.timestamp.timestamp() * 1000)
    last_ts = state.get_last_timestamp(msg.chat_id)
    if last_ts and msg_ts <= last_ts:
        return False

    path = find_chat_file(msg, contacts)
    write_message_atomic(path, msg, contacts)
    state.update(msg.chat_id, msg_ts, msg.group_name or msg.sender_name)

    direction = "→" if msg.is_outgoing else "←"
    chat = msg.group_name or msg.sender_name
    preview = (msg.text[:40] + "...") if len(msg.text) > 40 else msg.text
    print(f"{direction} [{chat}] {preview} -> {path.name}", file=sys.stderr)
    return True


def main() -> int:
    state = SyncState(STATE_FILE)
    contacts = ContactsCache(CONTACTS_FILE)
    count = 0
    errors = 0

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            if process_line(line, state, contacts):
                count += 1
        except json.JSONDecodeError:
            continue  # Skip non-JSON lines (e.g., signal-cli warnings)
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            errors += 1
            if errors > 100:
                print("FATAL: Too many errors, exiting", file=sys.stderr)
                return 1

    if count > 0:
        print(f"Synced {count} messages", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
