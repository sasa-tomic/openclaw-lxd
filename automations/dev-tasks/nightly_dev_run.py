#!/usr/bin/env python3
"""
Nightly Dev Run - Pick top backlog task and spawn an agent.

Called by cron job. Outputs JSON for OpenClaw to process.
The actual agent spawning is done by OpenClaw after reading this output.
"""

import sys
import json
from datetime import datetime
from task_manager import (
    load_state, save_state, get_top_backlog_task, move_task,
    BACKLOG, IN_PROGRESS, AgentRun
)


def main():
    state = load_state()
    
    # Check if we already have too many active agents
    max_concurrent = 2
    active_count = len([a for a in state.active_agents.values() if a.status == "running"])
    if active_count >= max_concurrent:
        print(json.dumps({
            "action": "skip",
            "reason": f"Already {active_count} agents running (max {max_concurrent})",
        }))
        return
    
    # Get top task
    task = get_top_backlog_task()
    if not task:
        print(json.dumps({
            "action": "skip",
            "reason": "No tasks in backlog",
        }))
        return
    
    # Check if this task is already being worked on
    if task.id in state.active_agents:
        print(json.dumps({
            "action": "skip",
            "reason": f"Task {task.id} already has an active agent",
        }))
        return
    
    now = datetime.now().isoformat()
    
    # Move task to in-progress
    task = move_task(
        task.id,
        BACKLOG,
        IN_PROGRESS,
        updates={"started_at": now}
    )
    
    # Prepare agent spawn request
    # OpenClaw will read this and actually spawn the agent
    spawn_request = {
        "action": "spawn",
        "task_id": task.id,
        "task_title": task.title,
        "project": task.project,
        "priority": task.priority,
        "context": task.context,
        "prompt": build_agent_prompt(task),
    }
    
    # Update state (session_key will be added by OpenClaw after spawn)
    state.active_agents[task.id] = AgentRun(
        task_id=task.id,
        session_key="pending",  # Will be updated after spawn
        spawned_at=now,
        status="running",
    )
    state.last_nightly_run = now
    save_state(state)
    
    print(json.dumps(spawn_request, indent=2))


def build_agent_prompt(task) -> str:
    """Build the prompt for the dev agent."""
    return f"""You are working on a development task.

**Task:** {task.title}
**Project:** {task.project}
**Priority:** {task.priority}

**Context:**
{task.context}

**Instructions:**
1. Read any relevant project docs (AGENTS.md, existing code, etc.)
2. Implement the task following project conventions
3. Write tests if applicable
4. Commit your changes with a clear message
5. Report back with:
   - What you did
   - Any issues encountered
   - Whether the task is complete, needs review, or is blocked

If you get stuck or need clarification, mark the task as BLOCKED and explain why.
"""


if __name__ == "__main__":
    main()
