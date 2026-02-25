"""JSON state file management utilities.

Provides atomic read/write operations for JSON state files used by
various automation scripts.

Standard State File Location Convention:
=========================================

State files should be placed in the following locations based on their purpose:

1. Automation State (Primary):
   /home/openclaw/clawd/memory/<feature>-state.json
   - Used for general automation state that needs to persist across runs
   - Examples: dev-tasks state, pipeline state, etc.

2. Notes-Related Features:
   /projects/Notes/Pickle/<feature>-state.json
   - Used for features that interact with the Notes system
   - Examples: obsidian state, note processing state

3. Temporary State:
   /tmp/<feature>-state.json
   - Used for transient state that doesn't need long-term persistence
   - Examples: cache files, short-lived process state

Naming Convention:
  - Always use: <feature>-state.json
  - Use lowercase with hyphens for feature names
  - Examples: unified-pipeline-state.json, social-state.json
"""

import json
import os
import tempfile
from pathlib import Path


def load_state(state_file: Path, default: dict = None) -> dict:
    """Load JSON state from file.

    Args:
        state_file: Path to the state file
        default: Default state if file doesn't exist (default: {})

    Returns:
        Loaded state dict or default
    """
    if default is None:
        default = {}

    if not state_file.exists():
        return default.copy()

    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return default.copy()


def save_state(state_file: Path, state: dict) -> None:
    """Save state to JSON file with atomic write.

    Writes to a temp file first, then renames to ensure atomicity.

    Args:
        state_file: Path to the state file
        state: State dict to save
    """
    state_file.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=state_file.parent, prefix=state_file.name + ".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, str(state_file))
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def update_state(state_file: Path, updates: dict) -> dict:
    """Load state, apply updates, save, and return new state.

    Args:
        state_file: Path to the state file
        updates: Dict of updates to apply

    Returns:
        Updated state dict
    """
    state = load_state(state_file)
    state.update(updates)
    save_state(state_file, state)
    return state


class JsonStateFile:
    """Base class for JSON state file management with atomic writes.

    Extend this class for specific use cases (e.g., message sync state).
    """

    def __init__(self, path: Path, default: dict = None):
        self._path = path
        self._default = default or {}
        self._data = self._load()

    def _load(self) -> dict:
        if not self._path.exists():
            return self._default.copy()
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return self._default.copy()

    def save(self) -> None:
        """Save state atomically."""
        save_state(self._path, self._data)

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value

    def update(self, updates: dict) -> None:
        self._data.update(updates)

    @property
    def data(self) -> dict:
        return self._data
