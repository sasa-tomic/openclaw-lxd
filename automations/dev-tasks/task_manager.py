#!/usr/bin/env python3
"""
Dev Task Manager - Core library for task tracking.

Task files live in Obsidian: /projects/Notes/Pickle/dev-tasks/
Machine state lives in: ~/clawd/memory/dev-task-state.json

Task format in markdown:
## [PRIORITY] Task Title
- **ID:** unique-id
- **Project:** decent-cloud | voki | other
- **Created:** YYYY-MM-DD
- **Context:** Brief description and any relevant notes

Where PRIORITY is: P0 (critical), P1 (high), P2 (medium), P3 (low)
"""

import json
import re
import os
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional
import uuid

# Paths
TASKS_DIR = Path("/projects/Notes/Pickle/dev-tasks")
STATE_FILE = Path.home() / "clawd" / "memory" / "dev-task-state.json"
BACKLOG = TASKS_DIR / "BACKLOG.md"
IN_PROGRESS = TASKS_DIR / "IN-PROGRESS.md"
BLOCKED = TASKS_DIR / "BLOCKED.md"
DONE = TASKS_DIR / "DONE.md"


@dataclass
class Task:
    id: str
    title: str
    priority: str  # P0, P1, P2, P3
    project: str
    created: str
    context: str
    agent_session: Optional[str] = None
    started_at: Optional[str] = None
    blocked_reason: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[str] = None


@dataclass
class AgentRun:
    task_id: str
    session_key: str
    spawned_at: str
    last_checked: Optional[str] = None
    status: str = "running"  # running, completed, failed, stuck


@dataclass
class State:
    active_agents: dict  # task_id -> AgentRun
    last_nightly_run: Optional[str] = None
    last_monitor_run: Optional[str] = None


def load_state() -> State:
    """Load machine state from JSON."""
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        active = {}
        for tid, agent_data in data.get("active_agents", {}).items():
            active[tid] = AgentRun(**agent_data)
        return State(
            active_agents=active,
            last_nightly_run=data.get("last_nightly_run"),
            last_monitor_run=data.get("last_monitor_run"),
        )
    return State(active_agents={})


def save_state(state: State):
    """Save machine state to JSON."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "active_agents": {tid: asdict(agent) for tid, agent in state.active_agents.items()},
        "last_nightly_run": state.last_nightly_run,
        "last_monitor_run": state.last_monitor_run,
    }
    STATE_FILE.write_text(json.dumps(data, indent=2))


def parse_tasks(filepath: Path) -> list[Task]:
    """Parse tasks from a markdown file."""
    if not filepath.exists():
        return []
    
    content = filepath.read_text()
    tasks = []
    
    # Split by task headers (## [P0] Title format)
    pattern = r'^## \[(P[0-3])\] (.+?)$'
    blocks = re.split(pattern, content, flags=re.MULTILINE)
    
    # blocks[0] is content before first task (header/intro)
    # then [priority, title, body, priority, title, body, ...]
    i = 1
    while i < len(blocks) - 2:
        priority = blocks[i]
        title = blocks[i + 1].strip()
        body = blocks[i + 2] if i + 2 < len(blocks) else ""
        
        # Parse body for metadata
        task_id = _extract_field(body, "ID") or str(uuid.uuid4())[:8]
        project = _extract_field(body, "Project") or "unknown"
        created = _extract_field(body, "Created") or datetime.now().strftime("%Y-%m-%d")
        context = _extract_field(body, "Context") or body.strip()
        agent_session = _extract_field(body, "Agent")
        started_at = _extract_field(body, "Started")
        blocked_reason = _extract_field(body, "Blocked")
        completed_at = _extract_field(body, "Completed")
        result = _extract_field(body, "Result")
        
        tasks.append(Task(
            id=task_id,
            title=title,
            priority=priority,
            project=project,
            created=created,
            context=context,
            agent_session=agent_session,
            started_at=started_at,
            blocked_reason=blocked_reason,
            completed_at=completed_at,
            result=result,
        ))
        i += 3
    
    return tasks


def _extract_field(body: str, field: str) -> Optional[str]:
    """Extract a field value from task body."""
    pattern = rf'^\s*-?\s*\*?\*?{field}\*?\*?:\s*(.+?)$'
    match = re.search(pattern, body, re.MULTILINE | re.IGNORECASE)
    if match:
        value = match.group(1).strip()
        # Remove any leading ** from markdown bold
        value = re.sub(r'^\*\*\s*', '', value)
        return value
    return None


def format_task(task: Task) -> str:
    """Format a task as markdown (no bold markers for machine parsing)."""
    lines = [
        f"## [{task.priority}] {task.title}",
        f"- ID: {task.id}",
        f"- Project: {task.project}",
        f"- Created: {task.created}",
    ]
    if task.started_at:
        lines.append(f"- Started: {task.started_at}")
    if task.agent_session:
        lines.append(f"- Agent: {task.agent_session}")
    if task.blocked_reason:
        lines.append(f"- Blocked: {task.blocked_reason}")
    if task.completed_at:
        lines.append(f"- Completed: {task.completed_at}")
    if task.result:
        lines.append(f"- Result: {task.result}")
    lines.append(f"- Context: {task.context}")
    lines.append("")
    return "\n".join(lines)


def write_tasks(filepath: Path, tasks: list[Task], header: str = ""):
    """Write tasks to a markdown file."""
    content = header + "\n\n" if header else ""
    content += "\n".join(format_task(t) for t in tasks)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content)


def get_top_backlog_task() -> Optional[Task]:
    """Get highest priority task from backlog."""
    tasks = parse_tasks(BACKLOG)
    if not tasks:
        return None
    # Sort by priority (P0 first)
    tasks.sort(key=lambda t: t.priority)
    return tasks[0]


def move_task(task_id: str, from_file: Path, to_file: Path, updates: dict = None):
    """Move a task from one file to another, optionally updating fields."""
    from_tasks = parse_tasks(from_file)
    to_tasks = parse_tasks(to_file)
    
    task = None
    remaining = []
    for t in from_tasks:
        if t.id == task_id:
            task = t
        else:
            remaining.append(t)
    
    if not task:
        raise ValueError(f"Task {task_id} not found in {from_file}")
    
    # Apply updates
    if updates:
        for key, value in updates.items():
            if hasattr(task, key):
                setattr(task, key, value)
    
    to_tasks.insert(0, task)  # Add to top
    
    write_tasks(from_file, remaining, _get_header(from_file))
    write_tasks(to_file, to_tasks, _get_header(to_file))
    
    return task


def _get_header(filepath: Path) -> str:
    """Get the appropriate header for a task file."""
    headers = {
        BACKLOG: "# Dev Task Backlog\n\nPrioritized queue of development tasks. P0 = critical, P3 = low priority.",
        IN_PROGRESS: "# Tasks In Progress\n\nCurrently being worked on by agents or manually.",
        BLOCKED: "# Blocked Tasks\n\nTasks waiting on external input or dependencies.",
        DONE: "# Completed Tasks\n\nRolling log of finished work.",
    }
    return headers.get(filepath, "# Tasks")


def add_task(title: str, project: str, priority: str, context: str) -> Task:
    """Add a new task to the backlog."""
    task = Task(
        id=str(uuid.uuid4())[:8],
        title=title,
        priority=priority,
        project=project,
        created=datetime.now().strftime("%Y-%m-%d"),
        context=context,
    )
    
    tasks = parse_tasks(BACKLOG)
    tasks.append(task)
    # Sort by priority
    tasks.sort(key=lambda t: t.priority)
    write_tasks(BACKLOG, tasks, _get_header(BACKLOG))
    
    return task


def init_files():
    """Initialize task files if they don't exist."""
    for filepath in [BACKLOG, IN_PROGRESS, BLOCKED, DONE]:
        if not filepath.exists():
            write_tasks(filepath, [], _get_header(filepath))


if __name__ == "__main__":
    # Initialize files on first run
    init_files()
    print(f"Task files initialized in {TASKS_DIR}")
    print(f"State file: {STATE_FILE}")
