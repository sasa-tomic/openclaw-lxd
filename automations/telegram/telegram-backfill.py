#!/usr/bin/env python3
"""
Telegram Historical Backfill

Fetches ALL historical messages from Telegram and backfills them into
existing Obsidian files. Designed to be resumable and rate-limit friendly.

Uses existing session and .env from telegram-sync.py.

Features:
- Fetches all messages (no limit), oldest first
- Appends to existing files without duplicates
- Handles FloodWaitError gracefully
- Saves progress to resume if interrupted
- Shows detailed progress

Usage:
    cd /projects/automations/telegram
    source .venv/bin/activate
    python telegram-backfill.py [--chat "Chat Name"] [--force]
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    User, Chat, Channel,
    Message as TelegramMessage,
    MessageMediaPhoto, MessageMediaDocument,
)

# Load environment
load_dotenv()

# === Configuration ===

API_ID = os.environ.get("TELEGRAM_API_ID")
API_HASH = os.environ.get("TELEGRAM_API_HASH")
# Chats to skip (case-insensitive partial match)
SKIP_CHATS = {"omnity"}

if not API_ID or not API_HASH:
    print("ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set", file=sys.stderr)
    sys.exit(1)

OBSIDIAN_DIR = Path(os.environ.get(
    "OBSIDIAN_TELEGRAM_DIR",
    os.path.expanduser("~/clawd/notes/Telegram")
))
SESSION_DIR = Path(os.environ.get(
    "TELEGRAM_SESSION_DIR",
    os.path.expanduser("~/.telegram-sync/session")
))
STATE_DIR = Path(__file__).parent / ".state"
PROGRESS_FILE = STATE_DIR / "backfill-progress.json"

SESSION_NAME = str(SESSION_DIR / "telegram")

# Rate limiting
BATCH_SIZE = 100  # Messages per API call
BATCH_DELAY = 1.5  # Seconds between batches
CHAT_DELAY = 2.0   # Seconds between chats


# === Progress Tracking ===

class BackfillProgress:
    """Track backfill progress for resumability."""
    
    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()
    
    def _load(self) -> dict:
        if not self._path.exists():
            return {"chats": {}, "started": None, "completed": None}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, IOError):
            return {"chats": {}, "started": None, "completed": None}
    
    def save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.rename(self._path)
    
    def is_chat_done(self, chat_id: int) -> bool:
        """Check if chat has been fully backfilled."""
        return self._data.get("chats", {}).get(str(chat_id), {}).get("done", False)
    
    def get_chat_offset(self, chat_id: int) -> int:
        """Get the last processed offset for a chat (for resuming)."""
        return self._data.get("chats", {}).get(str(chat_id), {}).get("offset_id", 0)
    
    def update_chat(self, chat_id: int, name: str, offset_id: int, total: int, done: bool = False) -> None:
        """Update progress for a chat."""
        chat_key = str(chat_id)
        if "chats" not in self._data:
            self._data["chats"] = {}
        self._data["chats"][chat_key] = {
            "name": name,
            "offset_id": offset_id,
            "total_fetched": total,
            "done": done,
            "updated": datetime.now(timezone.utc).isoformat()
        }
        self.save()
    
    def mark_started(self) -> None:
        self._data["started"] = datetime.now(timezone.utc).isoformat()
        self.save()
    
    def mark_completed(self) -> None:
        self._data["completed"] = datetime.now(timezone.utc).isoformat()
        self.save()
    
    def reset_chat(self, chat_id: int) -> None:
        """Reset progress for a specific chat."""
        chat_key = str(chat_id)
        if chat_key in self._data.get("chats", {}):
            del self._data["chats"][chat_key]
            self.save()


# === Helper Functions ===

def sanitize_filename(name: str) -> str:
    """Convert name to safe filename."""
    if not name:
        return "Unknown"
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    result = result.strip().strip('.')
    return result[:80] or "Unknown"


def get_entity_name(entity) -> str:
    """Get display name for a user/chat/channel."""
    if isinstance(entity, User):
        parts = [entity.first_name or "", entity.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        return name or entity.username or f"User_{entity.id}"
    elif isinstance(entity, (Chat, Channel)):
        return entity.title or f"Group_{entity.id}"
    return f"Unknown_{getattr(entity, 'id', 'entity')}"


def is_group(entity) -> bool:
    """Check if entity is a group/channel."""
    return isinstance(entity, (Chat, Channel))


def get_media_description(message: TelegramMessage) -> str:
    """Get a text description of message media."""
    if not message.media:
        return ""
    
    if isinstance(message.media, MessageMediaPhoto):
        return "[Photo]"
    elif isinstance(message.media, MessageMediaDocument):
        doc = message.media.document
        if doc:
            for attr in doc.attributes:
                if hasattr(attr, 'file_name'):
                    return f"[File: {attr.file_name}]"
            mime = getattr(doc, 'mime_type', '')
            if 'video' in mime:
                return "[Video]"
            elif 'audio' in mime:
                return "[Audio]"
            elif 'voice' in mime:
                return "[Voice]"
            return "[Document]"
    
    return "[Media]"


def format_message(msg: TelegramMessage, sender_name: str, is_outgoing: bool) -> str:
    """Format message for Obsidian."""
    ts = msg.date.strftime("%Y-%m-%d %H:%M:%S")
    sender = "Me" if is_outgoing else sender_name
    
    content_parts = []
    
    # Media
    media_desc = get_media_description(msg)
    if media_desc:
        content_parts.append(media_desc)
    
    # Text
    if msg.text:
        content_parts.append(msg.text)
    
    content = " ".join(content_parts)
    if not content:
        return ""
    
    return f"[{ts}] {sender}: {content}  \n"


def get_chat_file(chat_name: str, is_group_chat: bool) -> Path:
    """Get path to chat markdown file."""
    subdir = "Groups" if is_group_chat else "DMs"
    safe_name = sanitize_filename(chat_name)
    return OBSIDIAN_DIR / subdir / f"{safe_name}.md"


def parse_existing_messages(path: Path) -> tuple[str, list[str], Optional[datetime]]:
    """
    Parse existing file to extract header, messages, and oldest timestamp.
    Returns (header, existing_lines, oldest_timestamp).
    """
    if not path.exists():
        return "", [], None
    
    content = path.read_text()
    lines = content.split('\n')
    
    # Find header (everything up to and including ---)
    header_lines = []
    message_lines = []
    found_separator = False
    
    for line in lines:
        if not found_separator:
            header_lines.append(line)
            if line.strip() == '---':
                found_separator = True
        else:
            message_lines.append(line)
    
    header = '\n'.join(header_lines)
    
    # Find oldest timestamp in existing messages
    oldest_ts = None
    ts_pattern = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
    
    for line in message_lines:
        match = ts_pattern.match(line.strip())
        if match:
            try:
                ts = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                ts = ts.replace(tzinfo=timezone.utc)
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts
            except ValueError:
                continue
    
    return header, message_lines, oldest_ts


def get_existing_message_ids(path: Path, client_me_id: int) -> set[tuple[str, str]]:
    """
    Extract (timestamp, content_preview) tuples from existing file to detect duplicates.
    """
    existing = set()
    if not path.exists():
        return existing
    
    content = path.read_text()
    # Match: [2024-01-01 12:34:56] Sender: content
    pattern = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] ([^:]+): (.+?)  $', re.MULTILINE)
    
    for match in pattern.finditer(content):
        ts = match.group(1)
        sender = match.group(2)
        content_preview = match.group(3)[:50]  # First 50 chars for matching
        existing.add((ts, content_preview))
    
    return existing


# === Main Backfill Logic ===

class TelegramBackfill:
    def __init__(self, target_chat: Optional[str] = None, force: bool = False):
        self.client: Optional[TelegramClient] = None
        self.progress = BackfillProgress(PROGRESS_FILE)
        self.me = None
        self.target_chat = target_chat
        self.force = force
        self._shutdown = False
    
    async def start(self):
        """Initialize and connect client."""
        self.client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
        await self.client.start()
        self.me = await self.client.get_me()
        print(f"✓ Logged in as: {get_entity_name(self.me)}", file=sys.stderr)
    
    async def get_sender_name(self, msg: TelegramMessage) -> str:
        """Get sender name for a message, with caching."""
        if not msg.sender_id:
            return "Unknown"
        try:
            sender = await self.client.get_entity(msg.sender_id)
            return get_entity_name(sender)
        except Exception:
            return f"User_{msg.sender_id}"
    
    async def backfill_chat(self, dialog, chat_idx: int, total_chats: int) -> int:
        """Backfill all historical messages for a single chat."""
        entity = dialog.entity
        chat_id = dialog.id
        chat_name = get_entity_name(entity)
        is_group_chat = is_group(entity)
        
        # Skip chats in SKIP_CHATS list
        if any(skip.lower() in chat_name.lower() for skip in SKIP_CHATS):
            print(f"  [{chat_idx}/{total_chats}] {chat_name}: Skipped (in SKIP_CHATS)")
            return 0
        
        # Skip if already done (unless force)
        if self.progress.is_chat_done(chat_id) and not self.force:
            print(f"  [{chat_idx}/{total_chats}] {chat_name}: Already backfilled, skipping")
            return 0
        
        # Get file path and existing messages
        path = get_chat_file(chat_name, is_group_chat)
        header, existing_lines, oldest_existing_ts = parse_existing_messages(path)
        existing_ids = get_existing_message_ids(path, self.me.id)
        
        print(f"  [{chat_idx}/{total_chats}] {chat_name}:", end=" ", flush=True)
        
        if oldest_existing_ts:
            print(f"oldest existing: {oldest_existing_ts.strftime('%Y-%m-%d')}", end=" ", flush=True)
        
        # Fetch all messages older than oldest existing
        all_messages = []
        offset_id = self.progress.get_chat_offset(chat_id) if not self.force else 0
        batch_count = 0
        
        try:
            async for msg in self.client.iter_messages(
                chat_id,
                limit=None,  # Fetch ALL
                offset_id=offset_id,
                reverse=True  # Oldest first
            ):
                if self._shutdown:
                    # Save progress before exit
                    if all_messages:
                        self.progress.update_chat(chat_id, chat_name, msg.id, len(all_messages), done=False)
                    return len(all_messages)
                
                if not isinstance(msg, TelegramMessage) or (not msg.text and not msg.media):
                    continue
                
                # Skip if message already exists (by timestamp + content)
                ts_str = msg.date.strftime("%Y-%m-%d %H:%M:%S")
                content_preview = (msg.text or "")[:50]
                if (ts_str, content_preview) in existing_ids:
                    continue
                
                # Skip messages newer than oldest existing (they should already be synced)
                if oldest_existing_ts and msg.date.replace(tzinfo=timezone.utc) >= oldest_existing_ts:
                    continue
                
                all_messages.append(msg)
                
                # Progress indicator
                if len(all_messages) % 100 == 0:
                    print(f"{len(all_messages)}...", end=" ", flush=True)
                
                # Rate limiting between batches
                batch_count += 1
                if batch_count >= BATCH_SIZE:
                    batch_count = 0
                    await asyncio.sleep(BATCH_DELAY)
        
        except FloodWaitError as e:
            print(f"\n⚠ Rate limited! Waiting {e.seconds}s...", file=sys.stderr)
            # Save progress before waiting
            if all_messages:
                last_msg = all_messages[-1]
                self.progress.update_chat(chat_id, chat_name, last_msg.id, len(all_messages), done=False)
            await asyncio.sleep(e.seconds + 1)
            # Continue where we left off next run
            return len(all_messages)
        
        if not all_messages:
            print("no new messages")
            self.progress.update_chat(chat_id, chat_name, 0, 0, done=True)
            return 0
        
        print(f"fetched {len(all_messages)} new messages", end=" ", flush=True)
        
        # Format all messages
        formatted_messages = []
        sender_cache = {}
        
        for msg in all_messages:
            # Cache sender lookups
            if msg.sender_id not in sender_cache:
                sender_cache[msg.sender_id] = await self.get_sender_name(msg)
            
            sender_name = sender_cache[msg.sender_id]
            is_outgoing = msg.out
            formatted = format_message(msg, sender_name, is_outgoing)
            if formatted:
                formatted_messages.append(formatted)
        
        if not formatted_messages:
            print("(all empty)")
            self.progress.update_chat(chat_id, chat_name, 0, 0, done=True)
            return 0
        
        # Write to file: header + new messages + existing messages
        # Messages are already in chronological order (oldest first due to reverse=True)
        
        if not header:
            # Create header if file didn't exist
            chat_type = "group" if is_group_chat else "chat"
            header = f"# {chat_name}\n\n_Telegram {chat_type} - live sync via telethon_\n\n---"
        
        # Join new messages
        new_content = '\n'.join(formatted_messages)
        
        # Existing content (skip empty lines at start)
        existing_content = '\n'.join(existing_lines).strip()
        
        # Combine: header + new messages + existing messages
        if existing_content:
            full_content = f"{header}\n\n{new_content}\n{existing_content}"
        else:
            full_content = f"{header}\n\n{new_content}"
        
        # Write atomically
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix('.tmp')
        tmp_path.write_text(full_content)
        tmp_path.rename(path)
        
        print(f"✓ wrote {len(formatted_messages)} messages")
        
        # Mark as done
        self.progress.update_chat(chat_id, chat_name, 0, len(formatted_messages), done=True)
        
        return len(formatted_messages)
    
    async def run(self):
        """Main backfill run."""
        await self.start()
        self.progress.mark_started()
        
        print("\n📥 Starting Telegram historical backfill...\n", file=sys.stderr)
        
        # Get all dialogs
        dialogs = []
        async for dialog in self.client.iter_dialogs():
            dialogs.append(dialog)
        
        # Filter by target chat if specified
        if self.target_chat:
            target_lower = self.target_chat.lower()
            dialogs = [d for d in dialogs if target_lower in get_entity_name(d.entity).lower()]
            if not dialogs:
                print(f"ERROR: No chat found matching '{self.target_chat}'", file=sys.stderr)
                return 1
        
        print(f"Found {len(dialogs)} chats to process\n")
        
        total_messages = 0
        
        for idx, dialog in enumerate(dialogs, 1):
            if self._shutdown:
                print("\n⚠ Interrupted, progress saved")
                break
            
            try:
                count = await self.backfill_chat(dialog, idx, len(dialogs))
                total_messages += count
            except Exception as e:
                chat_name = get_entity_name(dialog.entity)
                print(f"\n  ✗ Error in {chat_name}: {e}", file=sys.stderr)
                continue
            
            # Delay between chats
            if idx < len(dialogs):
                await asyncio.sleep(CHAT_DELAY)
        
        if not self._shutdown:
            self.progress.mark_completed()
            print(f"\n✅ Backfill complete! Total: {total_messages} messages")
        
        return 0
    
    async def shutdown(self):
        """Graceful shutdown."""
        self._shutdown = True
        if self.client:
            await self.client.disconnect()


async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Backfill Telegram history to Obsidian")
    parser.add_argument("--chat", help="Only backfill specific chat (partial name match)")
    parser.add_argument("--force", action="store_true", help="Re-backfill even if already done")
    parser.add_argument("--status", action="store_true", help="Show backfill progress status")
    args = parser.parse_args()
    
    if args.status:
        progress = BackfillProgress(PROGRESS_FILE)
        print(f"Progress file: {PROGRESS_FILE}")
        print(f"Started: {progress._data.get('started', 'Never')}")
        print(f"Completed: {progress._data.get('completed', 'Not yet')}")
        print(f"\nChats:")
        for chat_id, info in progress._data.get("chats", {}).items():
            status = "✓" if info.get("done") else "..."
            print(f"  {status} {info.get('name', chat_id)}: {info.get('total_fetched', 0)} messages")
        return 0
    
    backfill = TelegramBackfill(target_chat=args.chat, force=args.force)
    
    # Handle Ctrl+C
    import signal
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(backfill.shutdown()))
    
    try:
        return await backfill.run()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        await backfill.shutdown()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
