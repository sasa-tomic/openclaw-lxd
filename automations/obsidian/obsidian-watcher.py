#!/usr/bin/env python3
"""
Obsidian Note Watcher Service

Monitors /projects/Notes for markdown file changes and wakes the OpenClaw
main session to process them.

Features:
- Debouncing: Waits 2 seconds after last change before triggering
- Skip patterns: .obsidian/, .trash/, temp files, sync-conflict files
- Cooldown: Won't re-wake for the same file within 60 seconds (loop prevention)
"""

import os
import time
import logging
import threading
from pathlib import Path
from typing import Optional

import subprocess
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Configuration
WATCH_PATH = "/projects/Notes"
DEBOUNCE_SECONDS = 2.0

# Skip patterns
SKIP_DIRS = {".obsidian", ".trash", ".stversions", ".sync"}
SKIP_PATTERNS = {"sync-conflict", ".tmp", ".swp", ".swo", "~", ".DS_Store"}

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


# Track files we've recently woken for (to prevent loops)
recently_woken: dict[str, float] = {}
COOLDOWN_SECONDS = 60.0  # Don't re-wake for same file within this window


def was_recently_woken(filepath: str) -> bool:
    """Check if we recently woke for this file (prevent loops)."""
    global recently_woken
    now = time.time()
    
    # Clean up old entries
    recently_woken = {f: t for f, t in recently_woken.items() 
                      if now - t < COOLDOWN_SECONDS}
    
    if filepath in recently_woken:
        age = now - recently_woken[filepath]
        logger.debug(f"Skipping (recently woken {age:.1f}s ago): {filepath}")
        return True
    return False


def mark_as_woken(filepath: str):
    """Mark a file as recently woken."""
    recently_woken[filepath] = time.time()


def wake_main_session(filepath: str) -> bool:
    """Send wake request to OpenClaw main session via CLI."""
    try:
        # Make path relative for cleaner display
        relative_path = filepath
        if filepath.startswith(WATCH_PATH):
            relative_path = filepath[len(WATCH_PATH):].lstrip("/")
        
        message = f"📝 Note changed: {relative_path}\n\nReview the change and update MEMORY.md, TODO.md, or other relevant files if needed."
        
        logger.info(f"Waking main session for: {relative_path}")
        
        # Use openclaw agent CLI to send message to main session
        result = subprocess.run(
            [
                "openclaw", "agent",
                "--session-id", "main",
                "--message", message,
                "--channel", "webchat"
            ],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            logger.info("Wake request successful")
            mark_as_woken(filepath)
            return True
        else:
            logger.error(f"Wake request failed: {result.stderr or result.stdout}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error("Wake request timed out")
        return False
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return False


class DebouncedHandler(FileSystemEventHandler):
    """
    File system event handler with debouncing.
    
    Collects events and waits for DEBOUNCE_SECONDS of quiet time
    before triggering the wake call.
    """
    
    def __init__(self):
        super().__init__()
        self.pending_files: dict[str, float] = {}  # filepath -> last_event_time
        self.lock = threading.Lock()
        self.timer: Optional[threading.Timer] = None
    
    def _schedule_wake(self):
        """Schedule or reschedule the wake callback."""
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(DEBOUNCE_SECONDS, self._trigger_wake)
            self.timer.start()
    
    def _trigger_wake(self):
        """Trigger wake for all pending files."""
        with self.lock:
            if not self.pending_files:
                return
            
            # Get the most recently changed file
            # (we report just one to avoid spamming)
            latest_file = max(self.pending_files.keys(), 
                            key=lambda f: self.pending_files[f])
            count = len(self.pending_files)
            self.pending_files.clear()
        
        if count > 1:
            logger.info(f"Batched {count} file changes, reporting: {latest_file}")
        
        wake_main_session(latest_file)
    
    def _handle_event(self, event):
        """Handle a file system event."""
        if event.is_directory:
            return
        
        filepath = event.src_path
        
        # Skip based on path patterns
        if should_skip_path(filepath):
            return
        
        # Skip if we recently woke for this file (loop prevention)
        if was_recently_woken(filepath):
            return
        
        logger.info(f"Change detected: {filepath}")
        
        with self.lock:
            self.pending_files[filepath] = time.time()
        
        self._schedule_wake()
    
    def on_modified(self, event):
        self._handle_event(event)
    
    def on_created(self, event):
        self._handle_event(event)


def main():
    """Main entry point."""
    logger.info(f"Starting Obsidian watcher on: {WATCH_PATH}")
    logger.info(f"Debounce: {DEBOUNCE_SECONDS}s, Cooldown: {COOLDOWN_SECONDS}s")
    
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
