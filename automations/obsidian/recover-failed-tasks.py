#!/usr/bin/env python3
"""Recover failed tasks from notes and add to queue."""

import json
import os
import re
import time
from pathlib import Path

NOTES_PATH = "/projects/Notes"
QUEUE_FILE = "/home/openclaw/clawd/memory/todoist-queue.json"

PROJECT_MAPPING = {
    "axiom": "Axiom GmbH",
    "voki": "VoKI",
    "voKI": "VoKI",
    "voxtral": "VoKI",
    "decent cloud": "Decent Cloud",
    "decentcloud": "Decent Cloud",
    "personal": "Personal",
    "family": "Personal",
    "kids": "Personal",
}


def detect_project(text: str) -> str | None:
    text_lower = text.lower()
    for keyword, project in PROJECT_MAPPING.items():
        if keyword in text_lower:
            return project
    return None


def extract_pending_tasks():
    pending_pattern = re.compile(
        r"^(.*?)\s*❌\s*<!-- todoist:pending -->", re.MULTILINE
    )

    queue = []
    notes_dir = Path(NOTES_PATH)

    for md_file in notes_dir.rglob("*.md"):
        if any(skip in str(md_file) for skip in [".obsidian", ".trash", ".stversions"]):
            continue

        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
        except (IOError, OSError) as e:
            print(f"Warning: Could not read {md_file}: {e}")
            continue

        relative_path = str(md_file.relative_to(notes_dir))

        for match in pending_pattern.finditer(content):
            task_text = match.group(1).strip()
            task_text = re.sub(r"^- \[ \]\s*", "", task_text)
            task_text = re.sub(r"\s+", " ", task_text).strip()

            if len(task_text) < 5:
                continue

            project = detect_project(task_text)

            task_text_with_context = f"{task_text} [[{relative_path}]]"

            queue.append(
                {
                    "content": task_text_with_context,
                    "project": project,
                    "priority": 3,
                    "due_date": None,
                    "source_file": relative_path,
                    "queued_at": time.time(),
                    "retry_count": 0,
                    "recovered": True,
                }
            )

    return queue


def main():
    queue = extract_pending_tasks()
    print(f"Found {len(queue)} pending tasks")

    for task in queue[:5]:
        print(f"  - [{task['project'] or 'Inbox'}] {task['content'][:60]}...")

    if queue:
        os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
        with open(QUEUE_FILE, "w") as f:
            json.dump(queue, f, indent=2)
        print(f"\nSaved to {QUEUE_FILE}")


if __name__ == "__main__":
    main()
