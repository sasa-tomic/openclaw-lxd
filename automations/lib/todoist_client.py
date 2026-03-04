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
        """Get tasks, optionally filtered. Returns list of task dicts.

        Handles pagination to fetch ALL tasks.
        """
        if cls.is_rate_limited():
            return []

        all_tasks = []
        cursor = None

        try:
            while True:
                params = dict(filters)
                if cursor:
                    params["cursor"] = cursor

                resp = requests.get(
                    f"{API_BASE}/tasks",
                    headers=cls._headers(),
                    params=params,
                    timeout=30,
                )

                if resp.status_code == 429:
                    retry = int(resp.headers.get("Retry-After", 60))
                    cls._set_rate_limit(retry)
                    return all_tasks if all_tasks else []

                resp.raise_for_status()
                data = resp.json()

                if isinstance(data, dict) and "results" in data:
                    all_tasks.extend(data["results"])
                    cursor = data.get("next_cursor")
                    if not cursor:
                        break
                elif isinstance(data, list):
                    all_tasks.extend(data)
                    break
                else:
                    break

            return all_tasks
        except Exception as e:
            logger.error(f"Failed to get tasks: {e}")
            return all_tasks if all_tasks else []

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
    def check_duplicate_with_llm(
        cls, new_task_content: str
    ) -> tuple[bool, str, list[dict]]:
        """Use LLM to check if new task is duplicate of existing tasks.

        Returns:
            (is_duplicate: bool, reasoning: str, duplicate_tasks: list[dict])
        """
        from lib.llm_utils import call_llm, extract_json
        from difflib import SequenceMatcher
        import json

        tasks = cls.get_tasks()
        if not tasks:
            return False, "No existing tasks to compare", []

        new_task_clean = new_task_content.split("[[")[0].strip()

        candidate_tasks = []
        for task in tasks:
            task_content = task.get("content", "").split("[[")[0].strip()
            if len(task_content) < 5:
                continue

            ratio = SequenceMatcher(
                None, new_task_clean.lower(), task_content.lower()
            ).ratio()
            if ratio >= 0.3:
                candidate_tasks.append((task, ratio))

        if not candidate_tasks:
            return False, "No similar tasks found", []

        candidate_tasks.sort(key=lambda x: x[1], reverse=True)
        top_candidates = candidate_tasks[:10]

        candidates_summary = []
        for task, score in top_candidates:
            task_content = task.get("content", "").split("[[")[0].strip()
            candidates_summary.append(
                {
                    "task_id": task.get("id"),
                    "content": task_content,
                    "similarity": round(score, 2),
                }
            )

        prompt = f"""OBJECTIVE DUPLICATE DETECTION TASK

You are evaluating whether a NEW task is a duplicate of EXISTING tasks.

NEW TASK TO ADD:
"{new_task_clean}"

SIMILAR EXISTING TASKS (pre-filtered by text similarity):
{json.dumps(candidates_summary, indent=2)}

INSTRUCTIONS:
1. Compare the NEW task against each EXISTING task
2. Determine if they represent the SAME underlying action/commitment
3. Be OBJECTIVE and STRICT in your evaluation

DEFINITION OF DUPLICATE:
- Same core action targeting the SAME entity/person (e.g., "Call John" vs "Phone John about project")
- Same task with minor rewording (e.g., "Fix bug in login" vs "Repair login bug")

NOT A DUPLICATE:
- Actions involving DIFFERENT people/entities: "Call John" vs "Call Mary" = NOT DUPLICATE
- Different actions to same person: "Call John" vs "Email John" = NOT DUPLICATE
- Sequential or related tasks: "Draft report" vs "Review report" = NOT DUPLICATE
- Same general area but different specifics: "Fix login bug" vs "Fix signup bug" = NOT DUPLICATE
- Tasks mentioning different people/names = NEVER DUPLICATES

CRITICAL: If tasks mention different names, people, or entities, they are NEVER duplicates!

RESPONSE FORMAT (JSON):
{{
  "is_duplicate": true/false,
  "reasoning": "Brief, objective explanation",
  "duplicate_task_ids": [list of task_id values that are duplicates, or empty list]
}}

Be EXTREMELY CONSERVATIVE. When in doubt, return false.

Output ONLY the JSON object."""

        success, response = call_llm(prompt, timeout=60)

        if not success:
            logger.error(f"LLM duplicate check failed: {response}")
            return False, f"LLM error: {response}", []

        json_str = extract_json(response)
        if not json_str:
            logger.error(f"Failed to extract JSON from LLM response: {response}")
            return False, "Invalid LLM response format", []

        try:
            result = json.loads(json_str)
            is_duplicate = result.get("is_duplicate", False)
            reasoning = result.get("reasoning", "No reasoning provided")
            duplicate_ids = result.get("duplicate_task_ids", [])

            duplicate_tasks = [
                task for task in tasks if task.get("id") in duplicate_ids
            ]

            logger.info(
                f"LLM duplicate check: is_duplicate={is_duplicate}, reasoning={reasoning[:100]}"
            )

            return is_duplicate, reasoning, duplicate_tasks

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response: {e}")
            return False, "Failed to parse LLM response", []

    @classmethod
    def get_projects(cls) -> list[dict]:
        """Get all projects. Handles pagination to fetch ALL projects."""
        if cls.is_rate_limited():
            return []

        all_projects = []
        cursor = None

        try:
            while True:
                params = {}
                if cursor:
                    params["cursor"] = cursor

                resp = requests.get(
                    f"{API_BASE}/projects",
                    headers=cls._headers(),
                    params=params,
                    timeout=30,
                )

                if resp.status_code == 429:
                    retry = int(resp.headers.get("Retry-After", 60))
                    cls._set_rate_limit(retry)
                    return all_projects if all_projects else []

                resp.raise_for_status()
                data = resp.json()

                if isinstance(data, dict) and "results" in data:
                    all_projects.extend(data["results"])
                    cursor = data.get("next_cursor")
                    if not cursor:
                        break
                elif isinstance(data, list):
                    all_projects.extend(data)
                    break
                else:
                    break

            return all_projects
        except Exception as e:
            logger.error(f"Failed to get projects: {e}")
            return all_projects if all_projects else []
