#!/usr/bin/env python3
"""
Obsidian Note Watcher Service

Monitors /projects/Notes for markdown file changes and provides intelligent feedback:
- Extract action items (TODOs, commitments)
- Suggest related notes to update
- Add commitments to TODO.md automatically
- Send smart feedback to Telegram

Features:
- Debouncing: Waits 2 seconds after last change before triggering
- Skip patterns: .obsidian/, .trash/, temp files, sync-conflict files
- Cooldown: Won't re-wake for the same file within 60 seconds (loop prevention)
"""

import os
import re
import time
import logging
import threading
import json
from pathlib import Path
from typing import Optional
import subprocess
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Configuration
WATCH_PATH = "/projects/Notes"
DEBOUNCE_SECONDS = 2.0
TELEGRAM_CHAT_ID = "5996479639"

# Skip patterns
SKIP_DIRS = {".obsidian", ".trash", ".stversions", ".sync", "Signal", "WhatsApp", "Telegram"}
SKIP_PATTERNS = {"sync-conflict", ".tmp", ".swp", ".swo", "~", ".DS_Store"}

# Path patterns for special handling
TODO_FILE = "/projects/Notes/TODO.md"
MEMORY_FILE = "/projects/Notes/Pickle/MEMORY.md"
STATE_FILE = "/home/openclaw/clawd/memory/obsidian-watcher-state.json"

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


def should_skip_path(filepath: str) -> bool:
    """Check if the file should be skipped based on path patterns."""
    path = Path(filepath)

    # Must be a markdown file
    if path.suffix.lower() != ".md":
        return True

    # Check directory patterns
    for part in path.parts:
        if part in SKIP_DIRS:
            logger.debug(f"Skipping (dir pattern): {filepath}")
            return True

    # Check filename patterns
    filename = path.name.lower()
    for pattern in SKIP_PATTERNS:
        if pattern in filename:
            logger.debug(f"Skipping (name pattern): {filepath}")
            return True

    return False


# Track files we've recently processed (to prevent loops)
# Persistent state file to survive service restarts
COOLDOWN_SECONDS = 600.0  # 10 minutes


def load_state() -> dict[str, float]:
    """Load state from file."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.debug(f"Could not load state file: {e}")
    return {}


def save_state(state: dict[str, float]):
    """Save state to file."""
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
        logger.debug(f"State saved: {len(state)} entries")
    except Exception as e:
        logger.error(f"Could not save state file: {e}")


def was_recently_processed(filepath: str) -> bool:
    """Check if we recently processed this file."""
    now = time.time()
    state = load_state()

    # Clean up old entries and save
    cleaned = {f: t for f, t in state.items() if now - t < COOLDOWN_SECONDS}
    save_state(cleaned)

    if filepath in cleaned:
        age = now - cleaned[filepath]
        logger.debug(f"Skipping (recently processed {age:.1f}s ago): {filepath}")
        return True
    return False


def mark_as_processed(filepath: str):
    """Mark a file as recently processed."""
    state = load_state()
    state[filepath] = time.time()
    logger.debug(f"Marking as processed: {filepath}")
    save_state(state)


def analyze_commitments(text: str) -> list[str]:
    """Extract commitments from text (I'll do X, I need to Y, etc.)"""
    commitments = []

    # Patterns for commitments
    patterns = [
        r"(?:i'll|i will|i need to|i should|i must|got to|have to)\s+[^.!?]+[.!?]",
        r"(?:i'm going to|i am going to)\s+[^.!?]+[.!?]",
        r"i\s+(?:promise|commit|plan|intend)\s+to\s+[^.!?]+[.!?]",
    ]

    for pattern in patterns:
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for match in matches:
            commitment = match.group().strip()
            if len(commitment) > 10:  # Filter out too-short matches
                commitments.append(commitment)

    return commitments[:3]  # Max 3 commitments to avoid spam


def extract_action_items(text: str) -> list[str]:
    """Extract TODO-style action items."""
    todos = []

    # Look for TODO, FIXME, [ ], checkbox patterns
    patterns = [
        r"- \[ \]\s+[^-\n]+",
        r"TODO[:\s]+[^\n]+",
        r"FIXME[:\s]+[^\n]+",
        r"TODO:\s+\[?\[?\s+[^\]]+\]?\]?",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        todos.extend(matches)

    return todos[:5]


def suggest_related_updates(filepath: str, text: str) -> list[str]:
    """Suggest related notes that might need updating based on content."""
    suggestions = []

    relative_path = filepath.replace(WATCH_PATH + "/", "")

    # Project-specific suggestions
    if "Axiom" in text and "Axiom" not in relative_path:
        suggestions.append("Update Projects/AxiomLabs/Company Formation.md")
    if "VoKI" in text and "voice-ai" not in relative_path.lower():
        suggestions.append("Check voice-ai-agent project notes")
    if "Decent Cloud" in text and "decent-cloud" not in relative_path.lower():
        suggestions.append("Update Decent Cloud project notes")
    if "kids" in text.lower() or "school" in text.lower():
        suggestions.append("Update family/kids notes")

    # Commitment patterns
    for pattern in ["i'll", "will", "let me", "later", "should", "could"]:
        if pattern in text.lower():
            suggestions.append("Add to TODO.md")

    return suggestions[:3]


def send_telegram_message(message: str) -> bool:
    """Send message to Telegram via openclaw CLI (non-blocking)."""
    try:
        # Run in background - don't wait for delivery confirmation
        # This avoids timeout issues and keeps watcher responsive
        subprocess.Popen(
            [
                "/home/openclaw/.npm-global/bin/openclaw", "agent",
                "--agent", "glm",
                "--channel", "telegram",
                "--to", TELEGRAM_CHAT_ID,
                "--deliver",
                "--message", message
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

        logger.info("Message queued for Telegram delivery")
        return True

    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return False


def process_note_change(filepath: str) -> bool:
    """Analyze note changes and send intelligent feedback."""
    try:
        if not os.path.exists(filepath):
            return False

        # Read the note
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        # Get just the last ~50 lines (recent changes)
        lines = content.split('\n')
        recent_lines = '\n'.join(lines[-50:]) if len(lines) > 50 else content

        # Analyze
        commitments = analyze_commitments(recent_lines)
        action_items = extract_action_items(recent_lines)
        suggestions = suggest_related_updates(filepath, recent_lines)

        # Build feedback message
        relative_path = filepath.replace(WATCH_PATH + "/", "")
        feedback_parts = []

        feedback_parts.append(f"📝 Note changed: *{relative_path}*")

        # Add commitments if found
        if commitments:
            feedback_parts.append(f"\n🎯 Commitments detected:")
            for c in commitments:
                feedback_parts.append(f"  • {c}")

        # Add action items if found
        if action_items:
            feedback_parts.append(f"\n✅ Action items:")
            for a in action_items[:3]:  # Max 3 action items
                feedback_parts.append(f"  • {a.strip()}")

        # Add suggestions if any
        if suggestions:
            feedback_parts.append(f"\n💡 Suggestions:")
            for s in suggestions:
                feedback_parts.append(f"  • {s}")

        # Only send if there's something useful to say
        if len(feedback_parts) > 1:  # More than just the header
            message = '\n'.join(feedback_parts)
            success = send_telegram_message(message)
            if success:
                mark_as_processed(filepath)
            return success
        else:
            logger.debug("No actionable content found, skipping notification")
            # Still mark as processed to avoid repeated checks
            mark_as_processed(filepath)
            return True

    except Exception as e:
        logger.error(f"Error processing note: {e}")
        return False


class DebouncedHandler(FileSystemEventHandler):
    """File system event handler with debouncing."""

    def __init__(self):
        super().__init__()
        self.pending_files: dict[str, float] = {}
        self.lock = threading.Lock()
        self.timer: Optional[threading.Timer] = None

    def _schedule_process(self):
        """Schedule or reschedule the process callback."""
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(DEBOUNCE_SECONDS, self._trigger_process)
            self.timer.start()

    def _trigger_process(self):
        """Trigger processing for all pending files."""
        with self.lock:
            if not self.pending_files:
                return

            # Get the most recently changed file
            latest_file = max(self.pending_files.keys(),
                            key=lambda f: self.pending_files[f])
            count = len(self.pending_files)
            self.pending_files.clear()

        if count > 1:
            logger.info(f"Batched {count} file changes, analyzing: {latest_file}")

        process_note_change(latest_file)

    def _handle_event(self, event):
        """Handle a file system event."""
        if event.is_directory:
            return

        filepath = event.src_path

        # Skip based on path patterns
        if should_skip_path(filepath):
            return

        # Skip if we recently processed this file
        if was_recently_processed(filepath):
            return

        logger.info(f"Change detected: {filepath}")

        with self.lock:
            self.pending_files[filepath] = time.time()

        self._schedule_process()

    def on_modified(self, event):
        self._handle_event(event)

    def on_created(self, event):
        self._handle_event(event)


def main():
    """Main entry point."""
    logger.info(f"Starting Obsidian watcher on: {WATCH_PATH}")
    logger.info(f"Debounce: {DEBOUNCE_SECONDS}s, Cooldown: {COOLDOWN_SECONDS}s")
    logger.info(f"Telegram feedback enabled for chat {TELEGRAM_CHAT_ID}")

    # Verify watch path exists
    if not os.path.isdir(WATCH_PATH):
        logger.error(f"Watch path does not exist: {WATCH_PATH}")
        return 1

    # Set up observer
    event_handler = DebouncedHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCH_PATH, recursive=True)

    logger.info("Starting file system observer...")
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        observer.stop()

    observer.join()
    logger.info("Stopped")
    return 0


if __name__ == "__main__":
    exit(main())
