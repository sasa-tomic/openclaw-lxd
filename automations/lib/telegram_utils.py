"""Telegram messaging utilities.

Provides a centralized send_telegram() function with retry logic and
message chunking for long messages.
"""

import subprocess
import sys
import time

from lib.config import TELEGRAM_TARGET, OPENCLAW_BIN


def send_telegram(message: str, retries: int = 3) -> bool:
    """Send message to Telegram via openclaw CLI with retries.

    Uses exponential backoff (2^attempt seconds) between retries.

    Args:
        message: The message to send
        retries: Number of retry attempts (default 3)

    Returns:
        True on success, False on failure
    """
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    OPENCLAW_BIN,
                    "message",
                    "send",
                    "--channel",
                    "telegram",
                    "--target",
                    TELEGRAM_TARGET,
                    "--message",
                    message,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return True
            if attempt < retries - 1:
                time.sleep(2**attempt)
        except subprocess.TimeoutExpired:
            print(
                f"Telegram send timed out (attempt {attempt + 1}/{retries})",
                file=sys.stderr,
            )
            if attempt < retries - 1:
                time.sleep(2**attempt)
        except Exception as e:
            print(f"Failed to send Telegram message: {e}", file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(2**attempt)
    return False


def chunk_message(message: str, max_size: int = 3500) -> list[str]:
    """Split message into chunks that fit Telegram's limits.

    Args:
        message: The message to chunk
        max_size: Maximum size per chunk (default 3500 chars)

    Returns:
        List of message chunks
    """
    if len(message) <= max_size:
        return [message]

    chunks = []
    for i in range(0, len(message), max_size):
        chunk = message[i : i + max_size]
        chunks.append(chunk)
    return chunks
