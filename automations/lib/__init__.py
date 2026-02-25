"""Shared utilities for automations codebase.

This package contains common modules used across multiple automation scripts:
- config: Centralized configuration constants
- telegram_utils: Telegram messaging functions
- state_utils: JSON state file management
"""

from .telegram_utils import send_telegram, chunk_message
from .state_utils import load_state, save_state, update_state

__all__ = [
    "send_telegram",
    "chunk_message",
    "load_state",
    "save_state",
    "update_state",
]
