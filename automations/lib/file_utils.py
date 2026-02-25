"""File utility functions for safe file operations."""

import fcntl
import os
import re
from pathlib import Path


def sanitize_filename(name: str) -> str:
    """
    Sanitize a string for use as a filename.

    Removes characters invalid on most filesystems, strips whitespace and dots,
    and truncates to a maximum length.

    Args:
        name: The input string to sanitize.

    Returns:
        A sanitized filename string, or "Unknown" if the result is empty.
    """
    invalid_chars = r'<>:"/\\|\?*\x00-\x1f'
    sanitized = re.sub(f"[{invalid_chars}]", "", name)
    sanitized = sanitized.strip(" .")
    sanitized = sanitized[:80]
    return sanitized if sanitized else "Unknown"


def append_with_lock(path: Path, content: str) -> None:
    """
    Append content to a file with an exclusive lock.

    Uses fcntl to acquire an exclusive lock before writing to prevent
    race conditions when multiple processes write to the same file.

    Args:
        path: Path to the file to append to.
        content: The content to append.
    """
    try:
        with open(path, "a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(content)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError as e:
        raise OSError(f"Failed to append to {path}: {e}") from e
