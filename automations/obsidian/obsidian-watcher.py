#!/usr/bin/env python3
"""
Obsidian Note Watcher Service - Aggressive Task Extraction

Monitors /projects/Notes for ALL changes including:
- Notes in vault
- Chat messages (Signal, WhatsApp, Telegram)
- Daily notes

Aggressively extracts tasks and adds to Todoist:
- Action items, TODOs, commitments
- Follow-ups from conversations
- Questions needing answers
- Meeting/action items from chats

Deduplication: Checks existing Todoist tasks and updates instead of duplicating.

Configuration: /projects/automations/obsidian/HOW_TO_ADD_TODOS.md
"""

from __future__ import annotations

import os
import re
import sys
import time
import logging
import threading
import json
import subprocess
import hashlib
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.config import (
    NOTES_DIR as NOTES_DIR_STR,
    OPENCLAW_BIN,
    TELEGRAM_TARGET,
)
from lib.todoist_client import TodoistClient

WATCH_PATH = NOTES_DIR_STR
DEBOUNCE_SECONDS = 3.0
TELEGRAM_CHAT_ID = TELEGRAM_TARGET

SKIP_DIRS = {".obsidian", ".trash", ".stversions", ".sync", "Reference", "_reports"}
SKIP_PATH_PREFIXES = {
    "Pickle/Twitter/threads",
    "Pickle/_reports",
}
SKIP_PATTERNS = {"sync-conflict", ".tmp", ".swp", ".swo", "~", ".DS_Store"}
SKIP_FILES = {"MEMORY.md", "HEARTBEAT.md", "changelog.md", "Clawd.md"}

IGNORE_GROUPS = {
    "F-Droid Translators",
    "Weblate translators",
}

CHAT_DIRS = {"Signal", "WhatsApp", "Telegram"}

STATE_FILE = "/home/openclaw/clawd/memory/obsidian-watcher-state.json"
LASTRUN_FILE = "/home/openclaw/clawd/memory/obsidian-watcher-lastrun"
COOLDOWN_SECONDS = 120.0

PROJECT_MAPPING = {
    "axiom": "Axiom GmbH",
    "voKI": "VoKI",
    "voki": "VoKI",
    "voxtral": "VoKI",
    "decent cloud": "Decent Cloud",
    "decentcloud": "Decent Cloud",
    "personal": "Personal",
    "family": "Personal",
    "kids": "Personal",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


skip_dedup_this_run: bool = False

PICKLE_ANNOTATION_MARKERS = {
    "added": "✅",
    "updated": "🔄",
    "failed": "❌",
    "skipped": "⏭️",
}

PROCESSED_MARKER_PATTERN = re.compile(
    r"\s*[✅❌🔄⏭️❓]\s*(?:<!\s*\[?[\w-]+\]?\s*|<!--\s*todoist:[^>]*-->\s*)*$",
    re.IGNORECASE,
)


def strip_task_markers(text: str) -> str:
    """Normalize task content for comparison - remove timestamps, markers, etc."""
    result = text
    result = PROCESSED_MARKER_PATTERN.sub("", result)
    result = re.sub(r"\s*<!--\s*todoist:[^>]*-->\s*$", "", result, flags=re.IGNORECASE)
    result = re.sub(r"\s*<!\s*\[?[\w-]+\]?\s*$", "", result)
    for marker in PICKLE_ANNOTATION_MARKERS.values():
        result = result.replace(marker, "")
    result = re.sub(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", "", result)
    result = re.sub(r"Matrix Telegram Bridge:?\s*", "", result)
    result = " ".join(result.split())
    return result.strip().lower()


def load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.debug(f"Could not load state file: {e}")
    return {}


def save_state(state: dict):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.error(f"Could not save state file: {e}")


def is_task_processed(filepath: str, task_content: str) -> bool:
    """Check if a task from a file has already been processed."""
    state = load_state()
    processed = state.get("processed_tasks", {})
    content_hash = hashlib.md5(strip_task_markers(task_content).encode()).hexdigest()[
        :12
    ]
    key = f"{filepath}:{content_hash}"
    return key in processed


def mark_task_processed(filepath: str, task_content: str):
    """Mark a task as processed."""
    state = load_state()
    if "processed_tasks" not in state:
        state["processed_tasks"] = {}
    content_hash = hashlib.md5(strip_task_markers(task_content).encode()).hexdigest()[
        :12
    ]
    key = f"{filepath}:{content_hash}"
    state["processed_tasks"][key] = time.time()
    now = time.time()
    state["processed_tasks"] = {
        k: v for k, v in state["processed_tasks"].items() if now - v < 86400 * 7
    }
    save_state(state)


def should_skip_path(filepath: str) -> bool:
    path = Path(filepath)

    if path.suffix.lower() != ".md":
        return True

    for part in path.parts:
        if part in SKIP_DIRS:
            return True

    try:
        relative = str(path.relative_to(WATCH_PATH))
    except ValueError:
        relative = filepath
    for prefix in SKIP_PATH_PREFIXES:
        if relative.startswith(prefix):
            return True

    filename = path.name.lower()
    for pattern in SKIP_PATTERNS:
        if pattern in filename:
            return True

    for skip_file in SKIP_FILES:
        if skip_file.lower() in filename:
            return True

    for ignored in IGNORE_GROUPS:
        if ignored in filepath:
            return True

    # Skip if any ancestor directory (up to WATCH_PATH) contains a .notodos file
    watch_root = Path(WATCH_PATH)
    for parent in path.parents:
        if (parent / ".notodos").exists():
            return True
        if parent == watch_root:
            break

    return False


def is_chat_file(filepath: str) -> bool:
    """Check if file is from a chat sync folder."""
    path = Path(filepath)
    return any(part in CHAT_DIRS for part in path.parts)


def was_recently_processed(filepath: str) -> bool:
    now = time.time()
    state = load_state()
    cleaned = {
        f: t
        for f, t in state.items()
        if isinstance(t, (int, float)) and now - t < COOLDOWN_SECONDS
    }
    save_state(cleaned)

    if filepath in cleaned:
        age = now - cleaned[filepath]
        logger.debug(f"Skipping (processed {age:.1f}s ago): {filepath}")
        return True
    return False


def mark_as_processed(filepath: str):
    state = load_state()
    state[filepath] = time.time()
    save_state(state)


def find_files_since_lastrun() -> list[str]:
    """Find .md files modified since the last run using the lastrun timestamp file.

    Returns an empty list on first run (no lastrun file exists yet).
    """
    if not os.path.exists(LASTRUN_FILE):
        logger.info("No lastrun file — skipping startup backfill (first run)")
        return []
    try:
        result = subprocess.run(
            ["find", WATCH_PATH, "-newer", LASTRUN_FILE, "-name", "*.md", "-type", "f"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        files = [f for f in files if not should_skip_path(f)]
        return files
    except Exception as e:
        logger.error(f"Failed to find files since last run: {e}")
        return []


def touch_lastrun_file():
    """Update the lastrun file mtime to now."""
    try:
        os.makedirs(os.path.dirname(LASTRUN_FILE), exist_ok=True)
        Path(LASTRUN_FILE).touch()
    except Exception as e:
        logger.error(f"Failed to touch lastrun file: {e}")


def add_todoist_task(
    content: str,
    project: str | None = None,
    priority: int = 4,
    due_date: str | None = None,
    source_file: str | None = None,
) -> tuple[bool, str, Optional[str]]:
    """Add a task to Todoist via REST API. Returns (success, action_taken, task_id)."""
    global skip_dedup_this_run

    if TodoistClient.is_rate_limited():
        return False, "Rate limited", None

    try:
        existing = None
        if not skip_dedup_this_run:
            existing = TodoistClient.find_similar_task(content)

        if existing:
            task_id = existing["id"]
            current_priority = existing.get("priority", 4)

            if current_priority <= 2:
                logger.info(f"Task {task_id} already has priority {current_priority}")
                return True, "Already prioritized", task_id

            logger.info(f"Bumping task {task_id} priority from {current_priority} to 2")

            if TodoistClient.update_task(task_id, priority=2):
                logger.info(f"Bumped task {task_id} priority to 2")
                return True, "Updated existing task", task_id
            else:
                return False, "Failed to update", None

        success, task = TodoistClient.create_task(
            content=content,
            priority=priority,
            due_string=due_date,
        )

        if success:
            logger.info(f"Added Todoist task: {content[:50]}...")
            return True, "Added new task", task.get("id") if task else None
        else:
            return False, "Failed to create", None

    except Exception as e:
        logger.error(f"Failed to add Todoist task: {e}")
        return False, f"Error: {e}", None


def detect_project(text: str) -> Optional[str]:
    """Detect project from text content."""
    text_lower = text.lower()
    for keyword, project in PROJECT_MAPPING.items():
        if keyword in text_lower:
            return project
    return None


def filter_already_processed_lines(content: str) -> str:
    """Remove lines that already carry a todoist annotation or processed emoji marker."""
    filtered = []
    for line in content.split("\n"):
        if "<!-- todoist:" in line:
            continue
        # Line has a processed-emoji marker (✅ 🔄 ❌ ⏭️) followed by an HTML comment
        if re.search(r"[✅🔄❌⏭️]\s*<!--", line):
            continue
        filtered.append(line)
    return "\n".join(filtered)


def extract_tasks_with_llm(content: str, filepath: str, is_chat: bool) -> list[dict]:
    """Use LLM directly for intelligent task extraction."""
    from lib.llm_utils import call_llm

    try:
        relative_path = filepath.replace(WATCH_PATH + "/", "")
        context = "chat conversation" if is_chat else "note"
        chat_context = (
            "GROUP CHAT - be extremely selective"
            if is_chat and "Groups" in filepath
            else ""
        )

        content = filter_already_processed_lines(content)

        prompt = f"""Analyze this {context} and extract ONLY tasks that require action from the reader/owner.

Source: {relative_path}
{chat_context}
Recent messages:
{content[:2000]}

IDENTIFYING THE OWNER/READER:
- Messages labeled "Me:" are from the owner
- First-person statements ("I'll", "I need to", "I should") indicate owner's messages
- In groups, the owner may appear by their username/nickname

CRITICAL RULES - FOLLOW STRICTLY:
1. For GROUP CHATS, extract tasks when:
   - Owner was @mentioned, @tagged, or directly asked by name
   - OR owner sent the message (their own commitments, "I'll", "I need to", "we should" when owner says it)
2. For DMs: Extract commitments the owner made or requests addressed to them
3. SKIP other people's messages in groups unless owner is mentioned
4. SKIP system messages, bridge notifications, timestamps, media placeholders
5. SKIP questions from others that don't need owner's answer

If uncertain, SKIP - false negatives are better than false positives.

For each ACTUAL task, output JSON:
{{"content": "task description", "priority": 1-4, "due": "date or null"}}

Priority: 1=urgent/today, 2=this week, 3=soon, 4=someday

If NO clear tasks for the owner, output nothing at all.

Output ONLY JSON lines, nothing else."""

        success, response = call_llm(prompt, timeout=120)

        if not success:
            logger.error(f"LLM extraction failed: {response}")
            return []

        tasks = []
        for line in response.strip().split("\n"):
            line = line.strip()
            if line.startswith("{") and "content" in line:
                try:
                    task = json.loads(line.rstrip(","))
                    if "content" in task and len(str(task["content"])) > 5:
                        tasks.append(
                            {
                                "content": str(task["content"]),
                                "due": task.get("due"),
                                "priority": task.get("priority", 4),
                                "source": "llm",
                            }
                        )
                except json.JSONDecodeError:
                    continue

        return tasks

    except Exception as e:
        logger.error(f"LLM extraction failed: {e}")
        return []


def dedupe_tasks(tasks: list[dict]) -> list[dict]:
    """Remove duplicate tasks within this batch."""
    seen = set()
    unique = []
    for task in tasks:
        key = strip_task_markers(task["content"]).lower()[:50]
        if key not in seen:
            seen.add(key)
            unique.append(task)
    return unique


def annotate_note_with_results(
    filepath: str, task_results: list[tuple[dict, str, str, Optional[str]]]
):
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        lines = content.split("\n")
        modified = False

        for task, status, _, todoist_id in task_results:
            task_text = task.get("content", "")
            if len(task_text) < 10:
                continue

            task_words = set(task_text.lower().split()[:6])
            if len(task_words) < 2:
                continue

            visible_emoji = PICKLE_ANNOTATION_MARKERS.get(status, "❓")
            task_id_comment = (
                f"<!-- todoist:{todoist_id} -->"
                if todoist_id
                else "<!-- todoist:pending -->"
            )

            for i, line in enumerate(lines):
                if "todoist:" in line:
                    continue

                line_lower = line.lower()
                line_words = set(line_lower.split())
                overlap = len(task_words & line_words)

                if overlap >= min(3, len(task_words)):
                    stripped = line.rstrip()
                    annotation = f" {visible_emoji} {task_id_comment}"
                    if not stripped.endswith("-->"):
                        lines[i] = stripped + annotation
                        modified = True
                    break

        if modified:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            footer = f"\n\n<!-- 🥒 Pickle processed: {timestamp} -->\n"

            if footer.strip() not in content:
                lines.append(footer)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            logger.info(f"Annotated {filepath} with {len(task_results)} task markers")

    except Exception as e:
        logger.error(f"Failed to annotate note: {e}")


def send_telegram_summary(
    filepath: str, task_results: list[tuple[dict, str, str, Optional[str]]]
):
    relative_path = filepath.replace(WATCH_PATH + "/", "")
    is_chat = is_chat_file(filepath)
    source_type = "chat" if is_chat else "note"

    lines = [f"📝 {source_type.title()}: {relative_path}"]

    added = [(t, s, r, i) for t, s, r, i in task_results if s == "added"]
    updated = [(t, s, r, i) for t, s, r, i in task_results if s == "updated"]
    failed = [(t, s, r, i) for t, s, r, i in task_results if s == "failed"]
    skipped = [(t, s, r, i) for t, s, r, i in task_results if s == "skipped"]

    if added:
        lines.append(f"\n✅ Added {len(added)} new tasks:")
        for task, _, _, tid in added[:5]:
            task_line = f"• {task['content'][:50]}"
            if tid:
                task_line += f" [{tid}]"
            lines.append(task_line)
        if len(added) > 5:
            lines.append(f"  ... and {len(added) - 5} more")

    if failed:
        lines.append(f"\n❌ Failed {len(failed)} tasks:")
        for task, _, reason, _ in failed[:3]:
            lines.append(f"• {task['content'][:40]}: {reason[:30]}")
        if len(failed) > 3:
            lines.append(f"  ... and {len(failed) - 3} more")

    if skipped and len(skipped) > 3:
        lines.append(f"\n⏭️ Skipped {len(skipped)} (too short)")

    if not added and not failed:
        return

    message = "\n".join(lines)

    try:
        subprocess.Popen(
            [
                OPENCLAW_BIN,
                "message",
                "send",
                "--channel",
                "telegram",
                "--target",
                TELEGRAM_CHAT_ID,
                "--message",
                message,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")


def process_note_change(filepath: str) -> bool:
    """Analyze note/chat and extract tasks aggressively."""
    global skip_dedup_this_run
    skip_dedup_this_run = False

    try:
        if not os.path.exists(filepath):
            return False

        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        if len(content) < 20:
            return False

        relative_path = filepath.replace(WATCH_PATH + "/", "")
        is_chat = is_chat_file(filepath)

        logger.info(f"Analyzing {'chat' if is_chat else 'note'}: {relative_path}")

        lines = content.split("\n")
        recent_content = "\n".join(lines[-100:]) if len(lines) > 100 else content

        tasks = extract_tasks_with_llm(recent_content, filepath, is_chat)

        tasks = dedupe_tasks(tasks)

        if not tasks:
            logger.debug("No tasks found")
            mark_as_processed(filepath)
            return True

        task_results: list[tuple[dict, str, str, Optional[str]]] = []

        for task in tasks[:15]:
            content_clean = re.sub(r"\s+", " ", task["content"]).strip()
            content_clean = content_clean.replace('"', "'")
            if len(content_clean) < 5:
                task_results.append((task, "skipped", "too short", None))
                continue

            if is_task_processed(relative_path, content_clean):
                logger.debug(f"Skipping already-processed task: {content_clean[:40]}")
                continue

            priority = task.get("priority", 4)
            due = task.get("due")
            project = detect_project(content_clean)

            content_with_context = f"{content_clean} [[{relative_path}]]"

            success, action, todoist_id = add_todoist_task(
                content=content_with_context,
                project=project,
                priority=priority,
                due_date=due,
                source_file=relative_path,
            )

            if success:
                mark_task_processed(relative_path, content_clean)
                time.sleep(2)
                if "new" in action.lower():
                    task_results.append((task, "added", action, todoist_id))
                elif "already" in action.lower():
                    pass
                else:
                    task_results.append((task, "updated", action, todoist_id))
            else:
                reason = (
                    action.replace("Failed: ", "")
                    if action.startswith("Failed:")
                    else action
                )
                task_results.append((task, "failed", reason, None))

        if task_results:
            logger.info(f"Processed {len(task_results)} tasks from {relative_path}")
            annotate_note_with_results(filepath, task_results)
            send_telegram_summary(filepath, task_results)

        mark_as_processed(filepath)
        return True

    except Exception as e:
        logger.error(f"Error processing note: {e}")
        import traceback

        traceback.print_exc()
        return False


class DebouncedHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self.pending_files: dict[str, float] = {}
        self.lock = threading.Lock()
        self.timer: Optional[threading.Timer] = None

    def _schedule_process(self):
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(DEBOUNCE_SECONDS, self._trigger_process)
            self.timer.start()

    def _trigger_process(self):
        with self.lock:
            if not self.pending_files:
                return

            files_to_process = list(self.pending_files.keys())
            self.pending_files.clear()

        for filepath in files_to_process:
            process_note_change(filepath)

    def _handle_event(self, event):
        if event.is_directory:
            return

        filepath = event.src_path

        if should_skip_path(filepath):
            return

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
    logger.info(f"Starting Obsidian Watcher (Aggressive Mode)")
    logger.info(f"Watch path: {WATCH_PATH}")
    logger.info(f"Including chats: {CHAT_DIRS}")
    logger.info(f"Tasks go to: Todoist via REST API (with deduplication)")
    logger.info(f"Debounce: {DEBOUNCE_SECONDS}s, Cooldown: {COOLDOWN_SECONDS}s")

    if not os.path.isdir(WATCH_PATH):
        logger.error(f"Watch path does not exist: {WATCH_PATH}")
        return 1

    # Collect backlog BEFORE touching, so we capture everything since last run.
    # Touch immediately after so the next run's window starts from now.
    backlog = find_files_since_lastrun()
    touch_lastrun_file()

    event_handler = DebouncedHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCH_PATH, recursive=True)

    if backlog:
        logger.info(f"Backfilling {len(backlog)} files modified since last run...")
        for filepath in backlog:
            process_note_change(filepath)
        logger.info("Startup backfill complete")

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
