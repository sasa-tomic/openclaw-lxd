"""Centralized configuration module.

All configuration values are read from environment variables with sensible defaults.
"""

import os
from pathlib import Path

TELEGRAM_TARGET = os.environ.get("TELEGRAM_TARGET", "5996479639")
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", "/home/openclaw/.npm-global/bin/openclaw")
OPENCODE_BIN = os.environ.get("OPENCODE_BIN", "/home/openclaw/.opencode/bin/opencode")
HIMALAYA_BIN = os.environ.get("HIMALAYA_BIN", "/home/openclaw/.local/bin/himalaya")
CHROMIUM_BIN = os.environ.get("CHROMIUM_BIN", "/usr/bin/chromium")
NOTES_DIR = os.environ.get("NOTES_DIR", "/projects/Notes")
MEMORY_DIR = os.environ.get("MEMORY_DIR", "/home/openclaw/clawd/memory")
HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/home/openclaw/clawd/HEARTBEAT.md")

TWITTER_BASE_URL = "https://x.com"
TWITTER_API_BASE = "https://x.com/i/api"

TWITTER_DB_URL = os.environ.get("TWITTER_DB_URL", "")


def validate_binaries() -> None:
    """Check if required binaries exist and print warnings for missing ones."""
    import sys

    binaries = {
        "OPENCLAW_BIN": OPENCLAW_BIN,
        "OPENCODE_BIN": OPENCODE_BIN,
    }

    for name, path in binaries.items():
        if not os.path.exists(path):
            print(
                f"WARNING: {name} binary not found at {path}",
                file=sys.stderr,
            )


def validate_config(require_telegram: bool = False) -> bool:
    """Validate configuration. Returns True if all required items present."""
    import logging

    logger = logging.getLogger(__name__)

    all_ok = True

    for name, path in [("OPENCLAW_BIN", OPENCLAW_BIN), ("OPENCODE_BIN", OPENCODE_BIN)]:
        if not Path(path).exists():
            logger.error(f"{name} not found at {path}")
            all_ok = False

    return all_ok
