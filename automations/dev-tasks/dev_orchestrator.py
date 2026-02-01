#!/usr/bin/env python3
"""
Dev Orchestrator - Manages the implement → verify → commit cycle.

Called by nightly cron. Outputs JSON instructions for the orchestrator agent.

Workflow:
1. Pick approved task from queue
2. Generate implementation prompt
3. After implementation: git add, generate verification prompt
4. If verification clean (no changes): commit
5. If verification made changes: retry (up to 3x)
6. On success: next task. On failure: stop batch.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional
from task_manager import (
    parse_tasks, move_task, write_tasks, load_state, save_state,
    BACKLOG, IN_PROGRESS, BLOCKED, DONE, _get_header, Task
)

STATE_FILE = Path.home() / "clawd" / "memory" / "dev-pipeline-state.json"

# Project-specific configurations
PROJECT_CONFIGS = {
    "decent-cloud": {
        "repo_path": "/projects/decent-cloud",
        "test_command": "cargo test",
        "agents_md": "/projects/decent-cloud/AGENTS.md",
        "pre_impl_read": [
            "/projects/decent-cloud/AGENTS.md",
            "/projects/decent-cloud/memory/decent-cloud-dev.md"
        ],
    },
    "voki": {
        "repo_path": "/projects/voice-ai-agent",
        "test_command": "pytest",
        "agents_md": "/projects/voice-ai-agent/AGENTS.md",
        "pre_impl_read": [],
    },
    "default": {
        "repo_path": None,
        "test_command": "echo 'No test command configured'",
        "agents_md": None,
        "pre_impl_read": [],
    }
}


@dataclass
class PipelineState:
    """Tracks current position in the dev pipeline."""
    status: str = "idle"  # idle, implementing, verifying, committing, done, failed
    current_task_id: Optional[str] = None
    current_task_title: Optional[str] = None
    project: Optional[str] = None
    verify_attempts: int = 0
    max_verify_attempts: int = 3
    impl_session_key: Optional[str] = None
    verify_session_key: Optional[str] = None
    batch_started_at: Optional[str] = None
    completed_tasks: list = None
    failed_task: Optional[str] = None
    error_message: Optional[str] = None
    
    def __post_init__(self):
        if self.completed_tasks is None:
            self.completed_tasks = []


def load_pipeline_state() -> PipelineState:
    """Load pipeline state from JSON."""
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return PipelineState(**data)
    return PipelineState()


def save_pipeline_state(state: PipelineState):
    """Save pipeline state to JSON."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(asdict(state), indent=2, default=list))


def get_project_config(project: str) -> dict:
    """Get configuration for a project."""
    return PROJECT_CONFIGS.get(project, PROJECT_CONFIGS["default"])


def get_approved_tasks() -> list[Task]:
    """Get tasks marked as approved (P0 or P1) from backlog."""
    tasks = parse_tasks(BACKLOG)
    # For now, P0 and P1 are considered approved
    # Later we can add explicit approval markers
    return [t for t in tasks if t.priority in ("P0", "P1")]


def build_implementation_prompt(task: Task) -> str:
    """Build the prompt for the implementation agent."""
    config = get_project_config(task.project)
    
    pre_read = ""
    if config["pre_impl_read"]:
        files = ", ".join(config["pre_impl_read"])
        pre_read = f"\n\n**First, read these files:** {files}"
    
    repo_note = ""
    if config["repo_path"]:
        repo_note = f"\n**Repository:** `{config['repo_path']}`"
    
    test_note = ""
    if config["test_command"]:
        test_note = f"\n**Test command:** `{config['test_command']}`"
    
    return f"""You are implementing a development task. Focus on clean, production-ready code.

**Task:** {task.title}
**Task ID:** {task.id}
**Project:** {task.project}
**Priority:** {task.priority}
{repo_note}{test_note}{pre_read}

**Context:**
{task.context}

**Instructions:**
1. Read the project's AGENTS.md if it exists for coding conventions
2. Implement the task following project patterns
3. Run relevant tests for the code you changed
4. Do NOT commit - just implement and verify tests pass
5. When done, output a summary:
   - Files changed
   - Tests run and results
   - Any concerns or blockers

**Important:** 
- Keep changes focused on this task only
- Follow existing code patterns
- If you hit a blocker, say "BLOCKED:" and explain why
- When complete, say "IMPLEMENTATION COMPLETE" with your summary
"""


def build_verification_prompt(task: Task, attempt: int) -> str:
    """Build the prompt for the verification agent."""
    config = get_project_config(task.project)
    
    return f"""You are verifying a code implementation. You have fresh context - no knowledge of how it was implemented.

**Task that was implemented:** {task.title}
**Task ID:** {task.id}
**Project:** {task.project}
**Verification attempt:** {attempt} of 3
**Repository:** `{config['repo_path'] or 'unknown'}`
**Test command:** `{config['test_command']}`

**Your job:**
1. Check `git status` to see what files were changed
2. Review the changes with `git diff`
3. Run the FULL test suite: `{config['test_command']}`
4. Verify the implementation matches the task requirements

**Task requirements were:**
{task.context}

**Decision:**
- If tests pass AND implementation looks correct → say "VERIFIED CLEAN"
- If you find issues that need fixing → fix them, then say "CHANGES MADE" with what you fixed
- If there are fundamental problems you can't fix → say "BLOCKED:" and explain

**Critical:** 
- Do NOT commit anything
- If you make any changes, say "CHANGES MADE"
- Only say "VERIFIED CLEAN" if `git diff` shows no uncommitted changes after your review
"""


def build_commit_message(task: Task) -> str:
    """Build a commit message for the task."""
    # Map priority to conventional commit type
    type_map = {"P0": "fix", "P1": "feat", "P2": "feat", "P3": "chore"}
    commit_type = type_map.get(task.priority, "chore")
    
    # Extract scope from task title if it has parentheses
    title = task.title
    scope = ""
    if "(" in title and ")" in title:
        # e.g., "Uptime calculation per provider (Phase 6.2)"
        # Keep the main title
        pass
    
    return f"{commit_type}: {title}\n\nTask ID: {task.id}\n\nAutomated implementation via dev-orchestrator."


def build_preflight_prompt(project: str) -> str:
    """Build prompt for preflight check (clean slate)."""
    config = get_project_config(project)
    
    return f"""You are preparing the repository for a dev cycle. Ensure a clean slate.

**Repository:** `{config['repo_path']}`
**Test command:** `{config['test_command']}`

**Steps:**
1. `cd {config['repo_path']}`
2. Check `git status` - if there are uncommitted changes:
   - Review them briefly
   - If they look intentional, commit with message "chore: uncommitted changes from previous session"
   - If they look broken/partial, stash them: `git stash -m "partial changes"`
3. Run the full test suite: `{config['test_command']}`
4. If tests fail:
   - Analyze the failures
   - Fix them
   - Commit fixes with message "fix: failing tests in preflight"
   - Run tests again to confirm
5. Ensure `git status` is clean and tests pass

**Output:**
- If all good: "PREFLIGHT COMPLETE" + brief summary
- If you fixed something: "PREFLIGHT COMPLETE - FIXES APPLIED" + what you fixed
- If you can't fix failures: "PREFLIGHT BLOCKED:" + explanation
"""


def start_batch():
    """Start a new batch run."""
    state = load_pipeline_state()
    
    # Check if already running
    if state.status not in ("idle", "done", "failed"):
        return {
            "action": "skip",
            "reason": f"Pipeline already in progress: {state.status}",
            "current_task": state.current_task_id,
        }
    
    # Get approved tasks
    tasks = get_approved_tasks()
    if not tasks:
        return {
            "action": "skip", 
            "reason": "No approved tasks (P0/P1) in backlog",
        }
    
    # Pick first task
    task = tasks[0]
    config = get_project_config(task.project)
    
    now = datetime.now().isoformat()
    
    # Update state - start with preflight
    state.status = "preflight"
    state.current_task_id = task.id
    state.current_task_title = task.title
    state.project = task.project
    state.verify_attempts = 0
    state.impl_session_key = None
    state.verify_session_key = None
    state.batch_started_at = now
    state.completed_tasks = []
    state.failed_task = None
    state.error_message = None
    save_pipeline_state(state)
    
    return {
        "action": "preflight",
        "task_id": task.id,
        "task_title": task.title,
        "project": task.project,
        "prompt": build_preflight_prompt(task.project),
        "repo_path": config["repo_path"],
        "next_step": "After preflight completes, run: python3 dev_orchestrator.py after_preflight <success|blocked> [error]",
    }


def after_preflight(success: bool, error: str = None):
    """Called after preflight completes."""
    state = load_pipeline_state()
    
    if state.status != "preflight":
        return {"error": f"Unexpected state: {state.status}"}
    
    if not success:
        state.status = "failed"
        state.failed_task = state.current_task_id
        state.error_message = error or "Preflight failed"
        save_pipeline_state(state)
        
        return {
            "action": "stop",
            "reason": state.error_message,
            "task_id": state.current_task_id,
        }
    
    # Move task to in-progress
    now = datetime.now().isoformat()
    move_task(state.current_task_id, BACKLOG, IN_PROGRESS, {"started_at": now})
    
    state.status = "implementing"
    save_pipeline_state(state)
    
    # Get task for prompt
    tasks = parse_tasks(IN_PROGRESS)
    task = next((t for t in tasks if t.id == state.current_task_id), None)
    
    if not task:
        return {"error": f"Task {state.current_task_id} not found in IN_PROGRESS"}
    
    return {
        "action": "implement",
        "task_id": task.id,
        "task_title": task.title,
        "project": task.project,
        "prompt": build_implementation_prompt(task),
        "repo_path": get_project_config(task.project)["repo_path"],
    }


def after_implementation(success: bool, session_key: str = None, error: str = None):
    """Called after implementation completes."""
    state = load_pipeline_state()
    
    if state.status != "implementing":
        return {"error": f"Unexpected state: {state.status}"}
    
    if not success:
        state.status = "failed"
        state.failed_task = state.current_task_id
        state.error_message = error or "Implementation failed"
        save_pipeline_state(state)
        
        # Move task to blocked
        move_task(state.current_task_id, IN_PROGRESS, BLOCKED, 
                  {"blocked_reason": state.error_message})
        
        return {
            "action": "stop",
            "reason": state.error_message,
            "task_id": state.current_task_id,
        }
    
    state.impl_session_key = session_key
    state.status = "verifying"
    state.verify_attempts = 1
    save_pipeline_state(state)
    
    # Get task for prompt
    tasks = parse_tasks(IN_PROGRESS)
    task = next((t for t in tasks if t.id == state.current_task_id), None)
    
    if not task:
        return {"error": f"Task {state.current_task_id} not found in IN_PROGRESS"}
    
    config = get_project_config(task.project)
    
    return {
        "action": "verify",
        "task_id": task.id,
        "attempt": state.verify_attempts,
        "prompt": build_verification_prompt(task, state.verify_attempts),
        "repo_path": config["repo_path"],
        "pre_verify_commands": [
            f"cd {config['repo_path']}",
            "git add -A",  # Stage all changes
        ] if config["repo_path"] else [],
    }


def after_verification(result: str, session_key: str = None):
    """
    Called after verification completes.
    result: "clean", "changes_made", "blocked"
    """
    state = load_pipeline_state()
    
    if state.status != "verifying":
        return {"error": f"Unexpected state: {state.status}"}
    
    state.verify_session_key = session_key
    
    # Get task
    tasks = parse_tasks(IN_PROGRESS)
    task = next((t for t in tasks if t.id == state.current_task_id), None)
    
    if not task:
        return {"error": f"Task {state.current_task_id} not found"}
    
    config = get_project_config(task.project)
    
    if result == "clean":
        # Ready to commit
        state.status = "committing"
        save_pipeline_state(state)
        
        return {
            "action": "commit",
            "task_id": task.id,
            "repo_path": config["repo_path"],
            "commit_message": build_commit_message(task),
            "commands": [
                f"cd {config['repo_path']}",
                "git add -A",
                f"git commit -m '{build_commit_message(task).replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'",
            ] if config["repo_path"] else [],
        }
    
    elif result == "changes_made":
        # Need to verify again
        if state.verify_attempts >= state.max_verify_attempts:
            state.status = "failed"
            state.failed_task = state.current_task_id
            state.error_message = f"Verification failed after {state.max_verify_attempts} attempts"
            save_pipeline_state(state)
            
            move_task(state.current_task_id, IN_PROGRESS, BLOCKED,
                      {"blocked_reason": state.error_message})
            
            return {
                "action": "stop",
                "reason": state.error_message,
                "task_id": task.id,
            }
        
        state.verify_attempts += 1
        save_pipeline_state(state)
        
        return {
            "action": "verify",
            "task_id": task.id,
            "attempt": state.verify_attempts,
            "prompt": build_verification_prompt(task, state.verify_attempts),
            "repo_path": config["repo_path"],
            "pre_verify_commands": [
                f"cd {config['repo_path']}",
                "git add -A",
            ] if config["repo_path"] else [],
        }
    
    else:  # blocked
        state.status = "failed"
        state.failed_task = state.current_task_id
        state.error_message = "Verification found blocking issues"
        save_pipeline_state(state)
        
        move_task(state.current_task_id, IN_PROGRESS, BLOCKED,
                  {"blocked_reason": state.error_message})
        
        return {
            "action": "stop",
            "reason": state.error_message,
            "task_id": task.id,
        }


def after_commit(success: bool, error: str = None):
    """Called after commit completes."""
    state = load_pipeline_state()
    
    if state.status != "committing":
        return {"error": f"Unexpected state: {state.status}"}
    
    if not success:
        state.status = "failed"
        state.failed_task = state.current_task_id
        state.error_message = error or "Commit failed"
        save_pipeline_state(state)
        return {
            "action": "stop",
            "reason": state.error_message,
        }
    
    # Success! Move to done
    now = datetime.now().isoformat()
    move_task(state.current_task_id, IN_PROGRESS, DONE, {
        "completed_at": now,
        "result": "Implemented and verified automatically",
    })
    
    state.completed_tasks.append({
        "id": state.current_task_id,
        "title": state.current_task_title,
    })
    
    # Check for more tasks
    remaining_tasks = get_approved_tasks()
    
    if remaining_tasks:
        # Start next task
        task = remaining_tasks[0]
        move_task(task.id, BACKLOG, IN_PROGRESS, {"started_at": now})
        
        state.status = "implementing"
        state.current_task_id = task.id
        state.current_task_title = task.title
        state.project = task.project
        state.verify_attempts = 0
        state.impl_session_key = None
        state.verify_session_key = None
        save_pipeline_state(state)
        
        return {
            "action": "implement",
            "task_id": task.id,
            "task_title": task.title,
            "project": task.project,
            "prompt": build_implementation_prompt(task),
            "repo_path": get_project_config(task.project)["repo_path"],
            "completed_so_far": state.completed_tasks,
        }
    else:
        # Batch complete!
        state.status = "done"
        save_pipeline_state(state)
        
        return {
            "action": "batch_complete",
            "completed_tasks": state.completed_tasks,
            "started_at": state.batch_started_at,
            "completed_at": now,
        }


def get_status():
    """Get current pipeline status."""
    state = load_pipeline_state()
    return asdict(state)


def reset():
    """Reset pipeline to idle state."""
    state = PipelineState()
    save_pipeline_state(state)
    return {"action": "reset", "status": "idle"}


def check_uncommitted_work(repo_path: str) -> dict:
    """Check if there's uncommitted work in a repo."""
    import subprocess
    
    try:
        # Check git status
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True
        )
        has_changes = bool(result.stdout.strip())
        
        # Get list of changed files
        changed_files = []
        if has_changes:
            for line in result.stdout.strip().split('\n'):
                if line:
                    # Format: "XY filename" where XY is status
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        changed_files.append(parts[1])
        
        return {
            "has_changes": has_changes,
            "changed_files": changed_files,
        }
    except Exception as e:
        return {"has_changes": False, "error": str(e)}


def build_resume_prompt(task: Task, state: PipelineState, uncommitted: dict) -> str:
    """Build prompt to resume partial work."""
    config = get_project_config(task.project)
    
    files_list = "\n".join(f"  - {f}" for f in uncommitted.get("changed_files", []))
    
    return f"""You are resuming a partially completed task. Previous agent timed out mid-work.

**Task:** {task.title}
**Task ID:** {task.id}
**Project:** {task.project}
**Repository:** `{config['repo_path']}`
**Test command:** `{config['test_command']}`

**Previous state:** {state.status}
**Verify attempts so far:** {state.verify_attempts}

**Uncommitted changes found:**
{files_list}

**Your job:**
1. Review the uncommitted changes: `git diff`
2. Understand what was done and what's missing
3. Run tests: `{config['test_command']}`
4. If tests pass and work looks complete:
   - Say "IMPLEMENTATION COMPLETE" with summary
5. If tests fail or work is incomplete:
   - Fix the issues
   - Run tests again
   - When passing, say "IMPLEMENTATION COMPLETE"
6. If fundamentally blocked, say "BLOCKED:" with explanation

**Task requirements were:**
{task.context}

**Important:** Don't start over - build on the existing work.
"""


def resume():
    """Check for partial work and generate continuation prompt if found."""
    state = load_pipeline_state()
    
    # If idle/done/failed with no current task, nothing to resume
    if state.status in ("idle", "done") or not state.current_task_id:
        return {
            "action": "no_resume",
            "reason": f"No partial work (status: {state.status})",
            "proceed_with": "start",
        }
    
    # Get task info
    tasks = parse_tasks(IN_PROGRESS)
    task = next((t for t in tasks if t.id == state.current_task_id), None)
    
    if not task:
        # Task not in progress, check backlog
        tasks = parse_tasks(BACKLOG)
        task = next((t for t in tasks if t.id == state.current_task_id), None)
    
    if not task:
        # Can't find the task - reset and start fresh
        return {
            "action": "no_resume",
            "reason": f"Task {state.current_task_id} not found, resetting",
            "proceed_with": "start",
            "auto_reset": True,
        }
    
    config = get_project_config(task.project)
    
    if not config["repo_path"]:
        return {
            "action": "no_resume",
            "reason": "No repo path configured",
            "proceed_with": "start",
        }
    
    # Check for uncommitted changes
    uncommitted = check_uncommitted_work(config["repo_path"])
    
    if not uncommitted.get("has_changes"):
        # No uncommitted changes but state says in-progress
        # Might have been committed already or work was lost
        return {
            "action": "no_resume",
            "reason": f"State is {state.status} but no uncommitted changes found",
            "proceed_with": "start",
            "suggestion": "Previous work may have been lost or already committed",
        }
    
    # We have partial work! Generate resume prompt
    return {
        "action": "resume",
        "task_id": task.id,
        "task_title": task.title,
        "project": task.project,
        "previous_state": state.status,
        "uncommitted_files": uncommitted["changed_files"],
        "prompt": build_resume_prompt(task, state, uncommitted),
        "repo_path": config["repo_path"],
        "next_step": "After completion, run: python3 dev_orchestrator.py after_impl true",
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: dev_orchestrator.py <command> [args...]")
        print("Commands: resume, start, after_impl, after_verify, after_commit, status, reset")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "resume":
        result = resume()
    elif cmd == "start":
        result = start_batch()
    elif cmd == "after_preflight":
        success = sys.argv[2].lower() in ("true", "success") if len(sys.argv) > 2 else False
        error = sys.argv[3] if len(sys.argv) > 3 else None
        result = after_preflight(success, error)
    elif cmd == "after_impl":
        success = sys.argv[2].lower() == "true" if len(sys.argv) > 2 else False
        session = sys.argv[3] if len(sys.argv) > 3 else None
        error = sys.argv[4] if len(sys.argv) > 4 else None
        result = after_implementation(success, session, error)
    elif cmd == "after_verify":
        verdict = sys.argv[2] if len(sys.argv) > 2 else "blocked"
        session = sys.argv[3] if len(sys.argv) > 3 else None
        result = after_verification(verdict, session)
    elif cmd == "after_commit":
        success = sys.argv[2].lower() == "true" if len(sys.argv) > 2 else False
        error = sys.argv[3] if len(sys.argv) > 3 else None
        result = after_commit(success, error)
    elif cmd == "status":
        result = get_status()
    elif cmd == "reset":
        result = reset()
    else:
        result = {"error": f"Unknown command: {cmd}"}
    
    print(json.dumps(result, indent=2))
