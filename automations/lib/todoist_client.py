"""Todoist REST API client."""

import json
import time
import logging
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TODOIST_CONFIG = Path.home() / ".config/todoist/config.json"
API_BASE = "https://api.todoist.com/api/v1"


def _load_token() -> str:
    """Load Todoist token from CLI config."""
    if not TODOIST_CONFIG.exists():
        raise FileNotFoundError(f"Todoist config not found at {TODOIST_CONFIG}")
    return json.loads(TODOIST_CONFIG.read_text())["token"]


class TodoistClient:
    """Todoist REST API client with rate limiting."""

    _token: Optional[str] = None
    _rate_limited_until: float = 0

    @classmethod
    def _get_token(cls) -> str:
        if cls._token is None:
            cls._token = _load_token()
        return cls._token

    @classmethod
    def _headers(cls) -> dict:
        return {
            "Authorization": f"Bearer {cls._get_token()}",
            "Content-Type": "application/json",
        }

    @classmethod
    def is_rate_limited(cls) -> bool:
        return time.time() < cls._rate_limited_until

    @classmethod
    def _set_rate_limit(cls, retry_after: int):
        cls._rate_limited_until = time.time() + retry_after
        logger.warning(f"Rate limited for {retry_after}s")

    @classmethod
    def get_tasks(cls, **filters) -> list[dict]:
        """Get tasks, optionally filtered. Returns list of task dicts."""
        if cls.is_rate_limited():
            return []

        try:
            resp = requests.get(
                f"{API_BASE}/tasks",
                headers=cls._headers(),
                params=filters,
                timeout=30,
            )

            if resp.status_code == 429:
                retry = int(resp.headers.get("Retry-After", 60))
                cls._set_rate_limit(retry)
                return []

            resp.raise_for_status()
            data = resp.json()
            # API returns {"results": [...], "next_cursor": ...}
            if isinstance(data, dict) and "results" in data:
                return data["results"]
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"Failed to get tasks: {e}")
            return []

    @classmethod
    def create_task(
        cls,
        content: str,
        project_id: Optional[str] = None,
        section_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        priority: int = 4,
        due_string: Optional[str] = None,
        labels: Optional[list[str]] = None,
    ) -> tuple[bool, Optional[dict]]:
        """Create a task. Returns (success, task_dict or None)."""
        if cls.is_rate_limited():
            return False, None

        data = {"content": content, "priority": priority}
        if project_id:
            data["project_id"] = project_id
        if section_id:
            data["section_id"] = section_id
        if parent_id:
            data["parent_id"] = parent_id
        if due_string:
            data["due_string"] = due_string
        if labels:
            data["labels"] = labels

        try:
            resp = requests.post(
                f"{API_BASE}/tasks",
                headers=cls._headers(),
                json=data,
                timeout=30,
            )

            if resp.status_code == 429:
                retry = int(resp.headers.get("Retry-After", 60))
                cls._set_rate_limit(retry)
                return False, None

            resp.raise_for_status()
            return True, resp.json()
        except Exception as e:
            logger.error(f"Failed to create task: {e}")
            return False, None

    @classmethod
    def update_task(cls, task_id: str, **updates) -> bool:
        """Update a task. Returns success."""
        if cls.is_rate_limited():
            return False

        try:
            resp = requests.post(
                f"{API_BASE}/tasks/{task_id}",
                headers=cls._headers(),
                json=updates,
                timeout=30,
            )

            if resp.status_code == 429:
                retry = int(resp.headers.get("Retry-After", 60))
                cls._set_rate_limit(retry)
                return False

            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to update task {task_id}: {e}")
            return False

    @classmethod
    def complete_task(cls, task_id: str) -> bool:
        """Mark task as complete. Returns success."""
        if cls.is_rate_limited():
            return False

        try:
            resp = requests.post(
                f"{API_BASE}/tasks/{task_id}/close",
                headers=cls._headers(),
                timeout=30,
            )

            if resp.status_code == 429:
                retry = int(resp.headers.get("Retry-After", 60))
                cls._set_rate_limit(retry)
                return False

            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to complete task {task_id}: {e}")
            return False

    @classmethod
    def find_similar_task(cls, content: str, threshold: float = 0.65) -> Optional[dict]:
        """Find a similar task by content matching. Returns task or None."""
        from difflib import SequenceMatcher

        tasks = cls.get_tasks()
        content_lower = content.lower()

        for task in tasks:
            task_content = task.get("content", "").lower()
            task_content = task_content.split("[[")[0].strip()
            content_clean = content_lower.split("[[")[0].strip()

            ratio = SequenceMatcher(None, task_content, content_clean).ratio()
            if ratio >= threshold:
                return task

        return None

    @classmethod
    def get_projects(cls) -> list[dict]:
        """Get all projects."""
        if cls.is_rate_limited():
            return []

        try:
            resp = requests.get(
                f"{API_BASE}/projects",
                headers=cls._headers(),
                timeout=30,
            )

            if resp.status_code == 429:
                retry = int(resp.headers.get("Retry-After", 60))
                cls._set_rate_limit(retry)
                return []

            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "results" in data:
                return data["results"]
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"Failed to get projects: {e}")
            return []
