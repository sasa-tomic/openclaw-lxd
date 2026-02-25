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
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from lib.state_utils import JsonStateFile
from telethon import TelegramClient, events
from telethon.tl.types import Message as TelegramMessage, MessageService

from telegram_common import (
    sanitize_filename,
    get_entity_name,
    is_group,
    get_media_description,
    format_message,
    get_chat_file,
    OBSIDIAN_DIR,
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

STATE_FILE = Path(
    os.environ.get(
        "TELEGRAM_STATE_FILE", os.path.expanduser("~/.telegram-sync/state.json")
    )
)
SESSION_DIR = Path(
    os.environ.get(
        "TELEGRAM_SESSION_DIR", os.path.expanduser("~/.telegram-sync/session")
    )
)

# Create directories
SESSION_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
(OBSIDIAN_DIR / "DMs").mkdir(parents=True, exist_ok=True)
(OBSIDIAN_DIR / "Groups").mkdir(parents=True, exist_ok=True)

SESSION_NAME = str(SESSION_DIR / "telegram")

# Rate limiting
INITIAL_SYNC_LIMIT = 100  # Messages per chat on initial sync
SYNC_BATCH_DELAY = 0.5  # Delay between chat syncs


# === State Management ===


class SyncState(JsonStateFile):
    """Track last synced message ID per chat."""

    def __init__(self, path: Path):
        super().__init__(path, default={"chats": {}, "initial_sync_done": False})

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
                "updated": datetime.now(timezone.utc).isoformat(),
            }
            self.save()

    @property
    def initial_sync_done(self) -> bool:
        return self._data.get("initial_sync_done", False)

    @initial_sync_done.setter
    def initial_sync_done(self, value: bool) -> None:
        self._data["initial_sync_done"] = value
        self.save()


def write_message_to_file(
    path: Path, formatted_msg: str, chat_name: str, is_group_chat: bool
) -> None:
    """Append message to file with locking."""
    if not formatted_msg:
        return

    # Create file with header if new
    if not path.exists():
        chat_type = "group" if is_group_chat else "chat"
        header = (
            f"# {chat_name}\n\n_Telegram {chat_type} - live sync via telethon_\n\n---\n"
        )
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
        print(
            f"Logged in as: {get_entity_name(self.me)} ({self.me.phone})",
            file=sys.stderr,
        )

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
                dialog.id, limit=INITIAL_SYNC_LIMIT, min_id=last_synced or 0
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
                    sender = (
                        await self.client.get_entity(msg.sender_id)
                        if msg.sender_id
                        else self.me
                    )
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

            print(
                f"  Synced {len(messages)} messages from {chat_name}", file=sys.stderr
            )
            await asyncio.sleep(SYNC_BATCH_DELAY)  # Rate limiting

        self.state.initial_sync_done = True
        print(
            f"Initial sync complete: {message_count} messages from {dialog_count} chats",
            file=sys.stderr,
        )

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
        preview = (
            (msg.text[:40] + "...")
            if msg.text and len(msg.text) > 40
            else (msg.text or "[Media]")
        )
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
