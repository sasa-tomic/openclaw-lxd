#!/usr/bin/env python3
"""
Agent Monitor - Check on running agents and update task status.

Called periodically by cron. Outputs status report for OpenClaw.
"""

import sys
import json
from datetime import datetime, timedelta
from task_manager import (
    load_state, save_state, parse_tasks, move_task, write_tasks,
    IN_PROGRESS, BLOCKED, DONE, AgentRun, _get_header
)


# Consider an agent stuck if no progress in this many hours
STUCK_THRESHOLD_HOURS = 4


def main():
    state = load_state()
    now = datetime.now()
    
    if not state.active_agents:
        print(json.dumps({
            "action": "nothing",
            "message": "No active agents to monitor",
        }))
        return
    
    report = {
        "action": "report",
        "timestamp": now.isoformat(),
        "agents": [],
        "alerts": [],
    }
    
    for task_id, agent in list(state.active_agents.items()):
        agent_report = {
            "task_id": task_id,
            "session_key": agent.session_key,
            "status": agent.status,
            "spawned_at": agent.spawned_at,
            "runtime_hours": None,
        }
        
        # Calculate runtime
        spawned = datetime.fromisoformat(agent.spawned_at)
        runtime = now - spawned
        agent_report["runtime_hours"] = round(runtime.total_seconds() / 3600, 1)
        
        # Check if stuck
        if agent.status == "running":
            last_check = datetime.fromisoformat(agent.last_checked) if agent.last_checked else spawned
            since_check = now - last_check
            
            if since_check > timedelta(hours=STUCK_THRESHOLD_HOURS):
                agent_report["possibly_stuck"] = True
                report["alerts"].append({
                    "type": "stuck",
                    "task_id": task_id,
                    "hours_since_activity": round(since_check.total_seconds() / 3600, 1),
                })
        
        # Update last checked
        agent.last_checked = now.isoformat()
        report["agents"].append(agent_report)
    
    state.last_monitor_run = now.isoformat()
    save_state(state)
    
    print(json.dumps(report, indent=2))


def mark_task_complete(task_id: str, result: str):
    """Mark a task as complete and move to done."""
    state = load_state()
    now = datetime.now().isoformat()
    
    # Move from in-progress to done
    try:
        task = move_task(
            task_id,
            IN_PROGRESS,
            DONE,
            updates={
                "completed_at": now,
                "result": result,
            }
        )
    except ValueError:
        # Task might already be moved
        pass
    
    # Remove from active agents
    if task_id in state.active_agents:
        state.active_agents[task_id].status = "completed"
        del state.active_agents[task_id]
    
    save_state(state)
    return task


def mark_task_blocked(task_id: str, reason: str):
    """Mark a task as blocked."""
    state = load_state()
    
    try:
        task = move_task(
            task_id,
            IN_PROGRESS,
            BLOCKED,
            updates={"blocked_reason": reason}
        )
    except ValueError:
        pass
    
    if task_id in state.active_agents:
        state.active_agents[task_id].status = "blocked"
        del state.active_agents[task_id]
    
    save_state(state)
    return task


def mark_task_failed(task_id: str, reason: str):
    """Mark a task as failed and move back to backlog."""
    state = load_state()
    from task_manager import BACKLOG
    
    try:
        task = move_task(
            task_id,
            IN_PROGRESS,
            BACKLOG,
            updates={
                "context": f"[FAILED: {reason}]\n\nOriginal context:\n" + 
                          (parse_tasks(IN_PROGRESS)[0].context if parse_tasks(IN_PROGRESS) else ""),
                "started_at": None,
                "agent_session": None,
            }
        )
    except (ValueError, IndexError):
        pass
    
    if task_id in state.active_agents:
        del state.active_agents[task_id]
    
    save_state(state)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "complete" and len(sys.argv) >= 4:
            mark_task_complete(sys.argv[2], sys.argv[3])
        elif cmd == "blocked" and len(sys.argv) >= 4:
            mark_task_blocked(sys.argv[2], sys.argv[3])
        elif cmd == "failed" and len(sys.argv) >= 4:
            mark_task_failed(sys.argv[2], sys.argv[3])
        else:
            print(f"Usage: {sys.argv[0]} [complete|blocked|failed] <task_id> <reason>")
    else:
        main()
