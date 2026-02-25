#!/usr/bin/env python3
"""
Shared functions for Telegram sync and backfill scripts.
"""

import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.file_utils import sanitize_filename

from telethon.tl.types import (
    User,
    Chat,
    Channel,
    Message as TelegramMessage,
    MessageMediaPhoto,
    MessageMediaDocument,
)

OBSIDIAN_DIR = Path(
    os.environ.get(
        "OBSIDIAN_TELEGRAM_DIR", os.path.expanduser("~/clawd/notes/Telegram")
    )
)


def get_entity_name(entity) -> str:
    if isinstance(entity, User):
        parts = [entity.first_name or "", entity.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        return name or entity.username or f"User_{entity.id}"
    elif isinstance(entity, (Chat, Channel)):
        return entity.title or f"Group_{entity.id}"
    return f"Unknown_{getattr(entity, 'id', 'entity')}"


def is_group(entity) -> bool:
    return isinstance(entity, (Chat, Channel))


def get_media_description(message: TelegramMessage) -> str:
    if not message.media:
        return ""

    if isinstance(message.media, MessageMediaPhoto):
        return "[Photo]"
    elif isinstance(message.media, MessageMediaDocument):
        doc = message.media.document
        if doc:
            for attr in doc.attributes:
                if hasattr(attr, "file_name"):
                    return f"[File: {attr.file_name}]"
            mime = getattr(doc, "mime_type", "")
            if "video" in mime:
                return "[Video]"
            elif "audio" in mime:
                return "[Audio]"
            elif "voice" in mime:
                return "[Voice]"
            return "[Document]"

    return "[Media]"


def format_message(msg: TelegramMessage, sender_name: str, is_outgoing: bool) -> str:
    ts = msg.date.strftime("%Y-%m-%d %H:%M:%S")
    sender = "Me" if is_outgoing else sender_name

    content_parts = []

    media_desc = get_media_description(msg)
    if media_desc:
        content_parts.append(media_desc)

    if msg.text:
        content_parts.append(msg.text)

    content = " ".join(content_parts)
    if not content:
        return ""

    return f"[{ts}] {sender}: {content}  \n"


def get_chat_file(chat_name: str, is_group_chat: bool) -> Path:
    subdir = "Groups" if is_group_chat else "DMs"
    safe_name = sanitize_filename(chat_name)
    return OBSIDIAN_DIR / subdir / f"{safe_name}.md"
