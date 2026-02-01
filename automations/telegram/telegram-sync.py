#!/usr/bin/env python3
"""
Telegram to Obsidian Live Sync

Uses telethon to sync Telegram messages to Obsidian markdown files.
- DMs → /projects/Notes/Telegram/DMs/<contact_name>.md
- Groups → /projects/Notes/Telegram/Groups/<group_name>.md

First run requires interactive authentication (phone number + code).

Environment variables:
    TELEGRAM_API_ID        - Telegram API ID
    TELEGRAM_API_HASH      - Telegram API hash
    OBSIDIAN_TELEGRAM_DIR  - Output directory (default: ~/clawd/notes/Telegram)
    TELEGRAM_STATE_FILE    - State file path
    TELEGRAM_SESSION_DIR   - Session directory
"""

import asyncio
import fcntl
import json
import os
import re
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import (
    User, Chat, Channel,
    Message as TelegramMessage,
    MessageMediaPhoto, MessageMediaDocument,
    MessageService
)

# Load environment
load_dotenv()

# === Configuration ===

API_ID = os.environ.get("TELEGRAM_API_ID")
API_HASH = os.environ.get("TELEGRAM_API_HASH")

if not API_ID or not API_HASH:
    print("ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set", file=sys.stderr)
    print("Create a .env file with your credentials", file=sys.stderr)
    sys.exit(1)

OBSIDIAN_DIR = Path(os.environ.get(
    "OBSIDIAN_TELEGRAM_DIR",
    os.path.expanduser("~/clawd/notes/Telegram")
))
STATE_FILE = Path(os.environ.get(
    "TELEGRAM_STATE_FILE",
    os.path.expanduser("~/.telegram-sync/state.json")
))
SESSION_DIR = Path(os.environ.get(
    "TELEGRAM_SESSION_DIR",
    os.path.expanduser("~/.telegram-sync/session")
))

# Create directories
SESSION_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
(OBSIDIAN_DIR / "DMs").mkdir(parents=True, exist_ok=True)
(OBSIDIAN_DIR / "Groups").mkdir(parents=True, exist_ok=True)

SESSION_NAME = str(SESSION_DIR / "telegram")

# Rate limiting
INITIAL_SYNC_LIMIT = 100  # Messages per chat on initial sync
SYNC_BATCH_DELAY = 0.5    # Delay between chat syncs


# === State Management ===

class SyncState:
    """Track last synced message ID per chat."""
    
    def __init__(self, path: Path):
        self._path = path
        self._data = self._load()
    
    def _load(self) -> dict:
        if not self._path.exists():
            return {"chats": {}, "initial_sync_done": False}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, IOError) as e:
            print(f"WARN: Failed to load state: {e}", file=sys.stderr)
            return {"chats": {}, "initial_sync_done": False}
    
    def save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.rename(self._path)
    
    def get_last_msg_id(self, chat_id: int) -> Optional[int]:
        chat_key = str(chat_id)
        return self._data.get("chats", {}).get(chat_key, {}).get("last_msg_id")
    
    def update(self, chat_id: int, msg_id: int, name: str) -> None:
        chat_key = str(chat_id)
        if "chats" not in self._data:
            self._data["chats"] = {}
        
        current = self._data["chats"].get(chat_key, {}).get("last_msg_id", 0)
        if msg_id > current:
            self._data["chats"][chat_key] = {
                "last_msg_id": msg_id,
                "name": name,
                "updated": datetime.now(timezone.utc).isoformat()
            }
            self.save()
    
    @property
    def initial_sync_done(self) -> bool:
        return self._data.get("initial_sync_done", False)
    
    @initial_sync_done.setter
    def initial_sync_done(self, value: bool) -> None:
        self._data["initial_sync_done"] = value
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


def write_message_to_file(path: Path, formatted_msg: str, chat_name: str, is_group_chat: bool) -> None:
    """Append message to file with locking."""
    if not formatted_msg:
        return
    
    # Create file with header if new
    if not path.exists():
        chat_type = "group" if is_group_chat else "chat"
        header = f"# {chat_name}\n\n_Telegram {chat_type} - live sync via telethon_\n\n---\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header)
    
    # Append with file locking
    with path.open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write("\n" + formatted_msg)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# === Main Sync Logic ===

class TelegramSync:
    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self.state = SyncState(STATE_FILE)
        self.me = None
        self._shutdown = False
    
    async def start(self):
        """Initialize and connect client."""
        self.client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
        await self.client.start()
        self.me = await self.client.get_me()
        print(f"Logged in as: {get_entity_name(self.me)} ({self.me.phone})", file=sys.stderr)
    
    async def initial_sync(self):
        """Sync recent messages from all dialogs."""
        if self.state.initial_sync_done:
            print("Initial sync already done, skipping...", file=sys.stderr)
            return
        
        print("Starting initial sync...", file=sys.stderr)
        dialog_count = 0
        message_count = 0
        
        async for dialog in self.client.iter_dialogs():
            if self._shutdown:
                break
            
            entity = dialog.entity
            if isinstance(entity, MessageService):
                continue
            
            chat_name = get_entity_name(entity)
            is_group_chat = is_group(entity)
            last_synced = self.state.get_last_msg_id(dialog.id)
            
            # Get recent messages
            messages = []
            async for msg in self.client.iter_messages(
                dialog.id,
                limit=INITIAL_SYNC_LIMIT,
                min_id=last_synced or 0
            ):
                if isinstance(msg, TelegramMessage) and (msg.text or msg.media):
                    messages.append(msg)
            
            if not messages:
                continue
            
            # Sort by date (oldest first)
            messages.sort(key=lambda m: m.date)
            
            # Write to file
            path = get_chat_file(chat_name, is_group_chat)
            
            for msg in messages:
                try:
                    sender = await self.client.get_entity(msg.sender_id) if msg.sender_id else self.me
                    sender_name = get_entity_name(sender)
                except Exception:
                    sender_name = "Unknown"
                
                is_outgoing = msg.out
                formatted = format_message(msg, sender_name, is_outgoing)
                write_message_to_file(path, formatted, chat_name, is_group_chat)
                message_count += 1
            
            # Update state with latest message
            self.state.update(dialog.id, messages[-1].id, chat_name)
            dialog_count += 1
            
            print(f"  Synced {len(messages)} messages from {chat_name}", file=sys.stderr)
            await asyncio.sleep(SYNC_BATCH_DELAY)  # Rate limiting
        
        self.state.initial_sync_done = True
        print(f"Initial sync complete: {message_count} messages from {dialog_count} chats", file=sys.stderr)
    
    async def handle_new_message(self, event):
        """Handle incoming message event."""
        msg = event.message
        
        # Skip service messages
        if isinstance(msg, MessageService) or (not msg.text and not msg.media):
            return
        
        try:
            chat = await event.get_chat()
            sender = await event.get_sender() if msg.sender_id else self.me
        except Exception as e:
            print(f"WARN: Could not get chat/sender: {e}", file=sys.stderr)
            return
        
        chat_name = get_entity_name(chat)
        sender_name = get_entity_name(sender) if sender else "Unknown"
        is_group_chat = is_group(chat)
        is_outgoing = msg.out
        
        # Format and write
        formatted = format_message(msg, sender_name, is_outgoing)
        if not formatted:
            return
        
        path = get_chat_file(chat_name, is_group_chat)
        write_message_to_file(path, formatted, chat_name, is_group_chat)
        
        # Update state
        self.state.update(chat.id, msg.id, chat_name)
        
        # Log
        direction = "→" if is_outgoing else "←"
        preview = (msg.text[:40] + "...") if msg.text and len(msg.text) > 40 else (msg.text or "[Media]")
        print(f"{direction} [{chat_name}] {preview}", file=sys.stderr)
    
    async def run(self):
        """Main run loop."""
        await self.start()
        
        # Do initial sync
        await self.initial_sync()
        
        # Register handler for new messages
        @self.client.on(events.NewMessage)
        async def handler(event):
            await self.handle_new_message(event)
        
        print("Listening for new messages... (Ctrl+C to stop)", file=sys.stderr)
        
        # Run until disconnected
        await self.client.run_until_disconnected()
    
    async def shutdown(self):
        """Graceful shutdown."""
        self._shutdown = True
        if self.client:
            await self.client.disconnect()
        print("\nShutdown complete", file=sys.stderr)


async def main():
    sync = TelegramSync()
    
    # Handle signals
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(sync.shutdown()))
    
    try:
        await sync.run()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        await sync.shutdown()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
