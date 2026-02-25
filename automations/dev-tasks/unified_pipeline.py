#!/usr/bin/env python3
"""
Unified Pipeline - Orchestrates dev work for any project.

Handles both manual and automated runs with per-project locking.
Follows 6-step flow: Resume → Start → Preflight → Implement → Verify → Commit → Next/Done

ALL prompts use Python subprocess module - NEVER bash scripts directly.

Usage:
  unified_pipeline.py manual --task-id <id>          # Manual run with specific task
  unified_pipeline.py manual --next               # Manual run with next P0/P1 task
  unified_pipeline.py automated                    # Automated run (for cron)
  unified_pipeline.py status                       # Show all locks and states
  unified_pipeline.py unlock <project>               # Force unlock (emergency)
"""

import json
import os
import sys
import signal
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from models import PipelineState

# Import task management
sys.path.insert(0, str(Path(__file__).parent))
from task_manager import (
    parse_tasks,
    move_task,
    write_tasks,
    BACKLOG,
    IN_PROGRESS,
    BLOCKED,
    DONE,
    Task,
)
from shared_config import PROJECT_CONFIGS, get_project_config as _get_project_config

# Paths
WORKSPACE = Path.home() / "clawd"
MEMORY_DIR = WORKSPACE / "memory"
AUTOMATIONS_DIR = Path("/projects/automations/dev-tasks")

# State files (per project)
STATE_FILES = {
    "decent-cloud": MEMORY_DIR / "dev-pipeline-state-decent-cloud.json",
    "voki": MEMORY_DIR / "dev-pipeline-state-voki.json",
}

# Lock files (per project)
LOCK_FILES = {
    "decent-cloud": MEMORY_DIR / "dev-pipeline-decent-cloud.lock",
    "voki": MEMORY_DIR / "dev-pipeline-voki.lock",
}

# Global state for cleanup
current_lock_file = None
session = None


def get_project_lock_file(project: str) -> Path:
    """Get lock file for a project."""
    return LOCK_FILES.get(project, MEMORY_DIR / f"dev-pipeline-{project}.lock")


def get_project_state_file(project: str) -> Path:
    """Get state file for a project."""
    return STATE_FILES.get(project, MEMORY_DIR / f"dev-pipeline-state-{project}.json")


def get_project_config(project: str) -> dict:
    """Get configuration for a project."""
    return _get_project_config(project)


def acquire_lock(project: str, mode: str = "unknown") -> bool:
    """
    Acquire lock for a project.
    Returns True if lock acquired, False if already locked.
    """
    global current_lock_file

    lock_file = get_project_lock_file(project)
    current_lock_file = lock_file

    if lock_file.exists():
        # Read existing lock
        try:
            lock_data = json.loads(lock_file.read_text())
            lock_pid = lock_data.get("pid")
            lock_time = lock_data.get("lockedAt", "")

            # Check if process still exists
            if lock_pid:
                try:
                    os.kill(int(lock_pid), 0)  # Check if alive, no signal
                    # Process is running
                    lock_by = lock_data.get("lockedBy", "unknown")
                    locked_at = lock_time
                    print(f"❌ Project '{project}' already locked")
                    print(f"   Locked by: {lock_by}")
                    print(f"   Locked at: {locked_at}")
                    print(f"   PID: {lock_pid}")
                    print(f"   Task: {lock_data.get('task', 'unknown')}")
                    return False
                except ProcessLookupError:
                    # Process is dead - stale lock
                    print(f"⚠️  Found stale lock (PID {lock_pid} not running)")
                    print(f"   Removing stale lock for '{project}'...")
                    lock_file.unlink()
                except (ValueError, OSError):
                    # Invalid PID or permission error
                    print(f"⚠️  Found stale lock (invalid PID {lock_pid})")
                    print(f"   Removing stale lock for '{project}'...")
                    lock_file.unlink()
        except (json.JSONDecodeError, OSError) as e:
            # Corrupt lock file
            print(f"⚠️  Found corrupt lock file: {e}")
            print(f"   Removing corrupt lock for '{project}'...")
            lock_file.unlink()

    # Create new lock
    lock_data = {
        "locked": True,
        "lockedAt": datetime.now(timezone.utc).isoformat(),
        "lockedBy": mode,
        "pid": os.getpid(),
        "project": project,
    }

    lock_file.write_text(json.dumps(lock_data, indent=2))
    print(f"✅ Lock acquired for '{project}' by {mode} (PID {os.getpid()})")
    return True


def release_lock(project: Optional[str] = None):
    """Release lock for a project. If project is None, use global current_lock_file."""
    global current_lock_file

    if project:
        lock_file = get_project_lock_file(project)
    elif current_lock_file:
        lock_file = current_lock_file
    else:
        return

    if lock_file and lock_file.exists():
        lock_file.unlink()
        print(f"🔓 Lock released for '{lock_file.stem.replace('dev-pipeline-', '')}'")


def load_pipeline_state(project: str) -> PipelineState:
    """Load pipeline state from JSON."""
    state_file = get_project_state_file(project)
    if state_file.exists():
        data = json.loads(state_file.read_text())
        return PipelineState(**data)
    return PipelineState(project=project, running_by="idle")


def save_pipeline_state(state: PipelineState):
    """Save pipeline state to JSON."""
    state_file = get_project_state_file(state.project)
    if not state_file.parent.exists():
        state_file.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "status": state.status,
        "current_task_id": state.current_task_id,
        "current_task_title": state.current_task_title,
        "project": state.project,
        "verify_attempts": state.verify_attempts,
        "max_verify_attempts": state.max_verify_attempts,
        "impl_session_key": state.impl_session_key,
        "verify_session_key": state.verify_session_key,
        "batch_started_at": state.batch_started_at,
        "completed_tasks": state.completed_tasks,
        "failed_task": state.failed_task,
        "error_message": state.error_message,
        "running_by": state.running_by,
    }
    state_file.write_text(json.dumps(data, indent=2, default=list))


def get_approved_tasks(project: str) -> list[Task]:
    """Get tasks marked as approved (P0 or P1) from backlog."""
    tasks = parse_tasks(BACKLOG)
    # Filter by project and priority
    return [t for t in tasks if t.priority in ("P0", "P1") and t.project == project]


def build_implementation_prompt(task: Task) -> str:
    """Build prompt for implementation agent."""
    config = get_project_config(task.project)
    repo_path = config["repo_path"]

    pre_read = ""
    if config["pre_impl_read"]:
        files = ", ".join(config["pre_impl_read"])
        pre_read = f"\n\n**First, read these files:** {files}"

    repo_note = ""
    if repo_path:
        repo_note = f"\n**Repository:** `{repo_path}`"

    test_cmd = (
        f'"{config["test_command"]}"'
        if " " in config["test_command"]
        else config["test_command"]
    )
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
1. Read project's AGENTS.md if it exists for coding conventions
2. Read /home/openclaw/clawd/docs/DEV_PROCESS.md if it exists
3. Implement task following project patterns
4. Run relevant tests for code you changed
5. Do NOT commit - just implement and verify tests pass
6. When done, output a summary:
   - Files changed
   - Tests run and results
   - Any concerns or blockers

**Python code example:**
```python
import subprocess
import os

# Change to repository
os.chdir("{repo_path}")

# Run tests
result = subprocess.run([{test_cmd}], capture_output=True, text=True)
print("Tests:", "PASSED" if result.returncode == 0 else "FAILED", result.stdout)
```

**Important:**
- Keep changes focused on this task only
- Follow existing code patterns
- If you hit a blocker, say "BLOCKED:" and explain why
- When complete, say "IMPLEMENTATION COMPLETE" with your summary
"""


def build_verification_prompt(task: Task, attempt: int) -> str:
    """Build prompt for verification agent."""
    config = get_project_config(task.project)
    repo_path = config["repo_path"]
    test_cmd = (
        f'"{config["test_command"]}"'
        if " " in config["test_command"]
        else config["test_command"]
    )

    return f"""You are verifying a code implementation. You have fresh context - no knowledge of how it was implemented.

**Task that was implemented:** {task.title}
**Task ID:** {task.id}
**Project:** {task.project}
**Verification attempt:** {attempt} of 3
**Repository:** `{repo_path or "unknown"}`
**Test command:** `{config["test_command"]}`
**Dev process:** `{config["dev_process"]}`

**Your job:**
1. Check `git status` to see what files were changed
2. Review changes with `git diff`
3. Run FULL test suite: `{config["test_command"]}`
4. Verify implementation matches task requirements

**Task requirements were:**
{task.context}

**Decision:**
- If tests pass AND implementation looks correct → say "VERIFIED CLEAN"
- If you find issues that need fixing → fix them, then say "CHANGES MADE" with what you fixed
- If there are fundamental problems you can't fix → say "BLOCKED:" and explain

**Python code example:**
```python
import subprocess
import os

# Change to repository
os.chdir("{repo_path}")

# Check git status
status = subprocess.run(["git", "status"], capture_output=True, text=True)

# Review git diff
diff = subprocess.run(["git", "diff"], capture_output=True, text=True)

# Run tests
tests = subprocess.run([{test_cmd}], capture_output=True, text=True)
```

**Critical:**
- Do NOT commit anything
- If you make any changes, say "CHANGES MADE"
- Only say "VERIFIED CLEAN" if `git diff` shows no uncommitted changes after your review
"""


def build_preflight_prompt(project: str) -> str:
    """Build prompt for preflight check (clean slate)."""
    config = get_project_config(project)
    repo_path = config["repo_path"]
    test_cmd = (
        f'"{config["test_command"]}"'
        if " " in config["test_command"]
        else config["test_command"]
    )

    return f"""You are preparing repository for a dev cycle. Ensure a clean slate.

**Repository:** `{repo_path}`
**Test command:** `{config["test_command"]}`
**Dev process:** `{config["dev_process"]}`

**Steps:**
1. Change directory to repository using os.chdir()
2. Check `git status` using subprocess.run(["git", "status"])
3. If there are uncommitted changes:
   - Review them briefly using subprocess.run(["git", "diff"])
   - If they look intentional, commit using subprocess.run(["git", "commit", "-m", "chore: uncommitted changes from previous session"])
   - If they look broken/partial, stash using subprocess.run(["git", "stash", "-m", "partial changes"])
4. Run full test suite using subprocess.run([{test_cmd}])
5. If tests fail:
   - Analyze failures
   - Fix them
   - Commit fixes using subprocess.run(["git", "commit", "-m", "fix: failing tests in preflight"])
   - Run tests again to confirm
6. Ensure `git status` is clean and tests pass

**Python code example:**
```python
import subprocess
import os

# Change to repository
os.chdir("{repo_path}")

# Check git status
status = subprocess.run(["git", "status"], capture_output=True, text=True)
print("Status:", status.stdout if status.returncode == 0 else status.stderr)

# Run tests
result = subprocess.run([{test_cmd}], capture_output=True, text=True)
if result.returncode != 0:
    print("Tests failed:", result.stderr)
    # Fix and re-run
else:
    print("Tests passed")
```

**Output:**
- If all good: "PREFLIGHT COMPLETE" + brief summary
- If you fixed something: "PREFLIGHT COMPLETE - FIXES APPLIED" + what you fixed
- If you can't fix failures: "PREFLIGHT BLOCKED:" + explanation

**Important:** Use Python subprocess to run all commands. Never execute bash directly.
"""


def build_commit_message(task: Task) -> str:
    """Build a commit message for task."""
    # Map priority to conventional commit type
    type_map = {"P0": "fix", "P1": "feat", "P2": "feat", "P3": "chore"}
    commit_type = type_map.get(task.priority, "chore")

    return f"{commit_type}: {task.title}\n\nTask ID: {task.id}\n\nImplemented via unified dev pipeline."


def start_batch(project: str, mode: str = "automated") -> dict:
    """Start a new batch run."""
    # Try to acquire lock
    if not acquire_lock(project, mode):
        return {
            "action": "skip",
            "reason": f"Project '{project}' is locked by another process",
        }

    state = load_pipeline_state(project)

    # Check if already running
    if state.status not in ("idle", "done", "failed"):
        release_lock(project)
        return {
            "action": "skip",
            "reason": f"Pipeline already in progress: {state.status}",
            "current_task": state.current_task_id,
        }

    # Get approved tasks
    tasks = get_approved_tasks(project)
    if not tasks:
        release_lock(project)
        return {
            "action": "skip",
            "reason": f"No approved tasks (P0/P1) in backlog for '{project}'",
        }

    # Pick first task
    task = tasks[0]
    config = get_project_config(task.project)

    now = datetime.now(timezone.utc).isoformat()

    # Update state - start with preflight
    state.status = "preflight"
    state.current_task_id = task.id
    state.current_task_title = task.title
    state.project = project
    state.verify_attempts = 0
    state.impl_session_key = None
    state.verify_session_key = None
    state.batch_started_at = now
    state.completed_tasks = []
    state.failed_task = None
    state.error_message = None
    state.running_by = mode
    save_pipeline_state(state)

    return {
        "action": "preflight",
        "task_id": task.id,
        "task_title": task.title,
        "project": project,
        "prompt": build_preflight_prompt(project),
        "repo_path": config["repo_path"],
        "mode": mode,
    }


def after_preflight(project: str, success: bool, error: str = None) -> dict:
    """Called after preflight completes."""
    state = load_pipeline_state(project)

    if state.status != "preflight":
        release_lock(project)
        return {"error": f"Unexpected state: {state.status}"}

    if not success:
        state.status = "failed"
        state.failed_task = state.current_task_id
        state.error_message = error or "Preflight failed"
        save_pipeline_state(state)

        release_lock(project)

        return {
            "action": "stop",
            "reason": state.error_message,
            "task_id": state.current_task_id,
        }

    # Move task to in-progress
    now = datetime.now(timezone.utc).isoformat()
    move_task(state.current_task_id, BACKLOG, IN_PROGRESS, {"started_at": now})

    state.status = "implementing"
    save_pipeline_state(state)

    # Get task for prompt
    tasks = parse_tasks(IN_PROGRESS)
    task = next((t for t in tasks if t.id == state.current_task_id), None)

    if not task:
        release_lock(project)
        return {"error": f"Task {state.current_task_id} not found in IN_PROGRESS"}

    return {
        "action": "implement",
        "task_id": task.id,
        "task_title": task.title,
        "project": project,
        "prompt": build_implementation_prompt(task),
        "repo_path": get_project_config(project)["repo_path"],
        "mode": state.running_by,
    }


def after_implementation(
    project: str, success: bool, session_key: str = None, error: str = None
) -> dict:
    """Called after implementation completes."""
    state = load_pipeline_state(project)

    if state.status != "implementing":
        release_lock(project)
        return {"error": f"Unexpected state: {state.status}"}

    if not success:
        state.status = "failed"
        state.failed_task = state.current_task_id
        state.error_message = error or "Implementation failed"
        save_pipeline_state(state)

        # Move task to blocked
        move_task(
            state.current_task_id,
            IN_PROGRESS,
            BLOCKED,
            {"blocked_reason": state.error_message},
        )

        release_lock(project)

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
        release_lock(project)
        return {"error": f"Task {state.current_task_id} not found in IN_PROGRESS"}

    config = get_project_config(task.project)

    return {
        "action": "verify",
        "task_id": task.id,
        "attempt": state.verify_attempts,
        "prompt": build_verification_prompt(task, state.verify_attempts),
        "repo_path": config["repo_path"],
        "mode": state.running_by,
        "pre_verify_commands": [
            f"cd {config['repo_path']}",
            "git add -A",  # Stage all changes
        ]
        if config["repo_path"]
        else [],
    }


def after_verification(project: str, result: str, session_key: str = None) -> dict:
    """
    Called after verification completes.
    result: "clean", "changes_made", "blocked"
    """
    state = load_pipeline_state(project)

    if state.status != "verifying":
        release_lock(project)
        return {"error": f"Unexpected state: {state.status}"}

    state.verify_session_key = session_key

    # Get task
    tasks = parse_tasks(IN_PROGRESS)
    task = next((t for t in tasks if t.id == state.current_task_id), None)

    if not task:
        release_lock(project)
        return {"error": f"Task {state.current_task_id} not found"}

    config = get_project_config(task.project)

    if result == "clean":
        # Ready to commit
        state.status = "committing"
        save_pipeline_state(state)

        return {
            "action": "commit",
            "task_id": task.id,
            "project": project,
            "repo_path": config["repo_path"],
            "commit_message": build_commit_message(task),
            "commands": [
                f"cd {config['repo_path']}",
                "git add -A",
                f"git commit -m '{build_commit_message(task).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'",
            ]
            if config["repo_path"]
            else [],
        }

    elif result == "changes_made":
        # Need to verify again
        if state.verify_attempts >= state.max_verify_attempts:
            state.status = "failed"
            state.failed_task = state.current_task_id
            state.error_message = (
                f"Verification failed after {state.max_verify_attempts} attempts"
            )
            save_pipeline_state(state)

            move_task(
                state.current_task_id,
                IN_PROGRESS,
                BLOCKED,
                {"blocked_reason": state.error_message},
            )

            release_lock(project)

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
            "mode": state.running_by,
            "pre_verify_commands": [
                f"cd {config['repo_path']}",
                "git add -A",
            ]
            if config["repo_path"]
            else [],
        }

    else:  # blocked
        state.status = "failed"
        state.failed_task = state.current_task_id
        state.error_message = "Verification found blocking issues"
        save_pipeline_state(state)

        move_task(
            state.current_task_id,
            IN_PROGRESS,
            BLOCKED,
            {"blocked_reason": state.error_message},
        )

        release_lock(project)

        return {
            "action": "stop",
            "reason": state.error_message,
            "task_id": task.id,
        }


def after_commit(project: str, success: bool, error: str = None) -> dict:
    """Called after commit completes."""
    state = load_pipeline_state(project)

    if state.status != "committing":
        release_lock(project)
        return {"error": f"Unexpected state: {state.status}"}

    if not success:
        state.status = "failed"
        state.failed_task = state.current_task_id
        state.error_message = error or "Commit failed"
        save_pipeline_state(state)
        release_lock(project)
        return {
            "action": "stop",
            "reason": state.error_message,
        }

    # Success! Move to done
    now = datetime.now(timezone.utc).isoformat()
    move_task(
        state.current_task_id,
        IN_PROGRESS,
        DONE,
        {
            "completed_at": now,
            "result": f"Implemented and verified via {state.running_by} pipeline",
        },
    )

    state.completed_tasks.append(
        {
            "id": state.current_task_id,
            "title": state.current_task_title,
        }
    )

    # Check for more tasks
    remaining_tasks = get_approved_tasks(project)

    if remaining_tasks:
        # Start next task
        task = remaining_tasks[0]
        move_task(task.id, BACKLOG, IN_PROGRESS, {"started_at": now})

        state.status = "implementing"
        state.current_task_id = task.id
        state.current_task_title = task.title
        state.project = project
        state.verify_attempts = 0
        state.impl_session_key = None
        state.verify_session_key = None
        save_pipeline_state(state)

        print(f"🔄 Moving to next task: {task.title}")

        return {
            "action": "implement",
            "task_id": task.id,
            "task_title": task.title,
            "project": project,
            "prompt": build_implementation_prompt(task),
            "repo_path": get_project_config(project)["repo_path"],
            "mode": state.running_by,
            "completed_so_far": state.completed_tasks,
        }
    else:
        # Batch complete!
        state.status = "done"
        state.running_by = "idle"
        save_pipeline_state(state)

        release_lock(project)

        return {
            "action": "batch_complete",
            "project": project,
            "completed_tasks": state.completed_tasks,
            "started_at": state.batch_started_at,
            "completed_at": now,
            "mode": state.running_by,
        }


def show_status():
    """Show all locks and pipeline states."""
    print("=== Dev Pipeline Status ===\n")

    for project in PROJECT_CONFIGS.keys():
        if project == "default":
            continue

        state_file = get_project_state_file(project)
        lock_file = get_project_lock_file(project)

        print(f"## {project}")

        # Lock status
        if lock_file.exists():
            try:
                lock_data = json.loads(lock_file.read_text())
                print(f"  Lock: 🔒 Locked by {lock_data.get('lockedBy', 'unknown')}")
                print(f"    At: {lock_data.get('lockedAt', 'unknown')}")
                print(f"    PID: {lock_data.get('pid', 'unknown')}")
                print(f"    Task: {lock_data.get('task', 'unknown')}")
            except (json.JSONDecodeError, OSError):
                print(f"  Lock: 🔒 Locked (corrupt file)")
        else:
            print(f"  Lock: 🔓 Not locked")

        if state_file.exists():
            try:
                state_data = json.loads(state_file.read_text())
                print(f"  Status: {state_data.get('status', 'unknown')}")
                if state_data.get("current_task_id"):
                    print(f"  Task: {state_data.get('current_task_title', 'unknown')}")
                print(f"  Running by: {state_data.get('running_by', 'unknown')}")
            except (json.JSONDecodeError, OSError):
                print(f"  Status: ❓ Unknown (corrupt state)")
        else:
            print(f"  Status: ❓ No state file")

        print()

    print("Use 'unified_pipeline.py unlock <project>' to force unlock a project.")


def force_unlock(project: str):
    """Force unlock a project."""
    if project not in LOCK_FILES:
        print(f"❌ Unknown project: {project}")
        print(f"   Valid projects: {list(LOCK_FILES.keys())}")
        return

    lock_file = get_project_lock_file(project)

    if not lock_file.exists():
        print(f"⚠️  No lock exists for '{project}'")
        return

    try:
        lock_data = json.loads(lock_file.read_text())
        print(f"🔓 Force unlocking '{project}'")
        print(f"   Was locked by: {lock_data.get('lockedBy', 'unknown')}")
        print(f"   Locked at: {lock_data.get('lockedAt', 'unknown')}")
        print(f"   PID: {lock_data.get('pid', 'unknown')}")
    except (json.JSONDecodeError, OSError):
        print(f"⚠️  Lock file appears corrupt")

    lock_file.unlink()
    print(f"✅ Lock removed")


def signal_handler(signum, frame):
    """Handle signals for cleanup."""
    print(f"\n🛑 Signal received ({signum}), cleaning up...")
    if current_lock_file and current_lock_file.exists():
        try:
            lock_data = json.loads(current_lock_file.read_text())
            project = lock_data.get("project")
            if project:
                release_lock(project)
        except (json.JSONDecodeError, OSError):
            pass
    sys.exit(1)


if __name__ == "__main__":
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if len(sys.argv) < 2:
        print("Usage: unified_pipeline.py <command> [args...]")
        print("\nCommands:")
        print("  manual --task-id <id>    Manual run with specific task ID")
        print("  manual --next               Manual run with next P0/P1 task")
        print("  automated                   Automated run (for cron)")
        print("  status                      Show all locks and states")
        print("  unlock <project>            Force unlock a project")
        print("\nProjects:")
        for p in PROJECT_CONFIGS.keys():
            if p != "default":
                print(f"  - {p}")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "manual":
        # Determine project from task ID or use default
        task_id = None
        project = "decent-cloud"  # Default

        if "--task-id" in sys.argv:
            idx = sys.argv.index("--task-id")
            if idx + 1 < len(sys.argv):
                task_id = sys.argv[idx + 1]

        # If no project specified, default to decent-cloud
        result = start_batch(project, mode="manual")
        if result.get("action") == "skip":
            print(f"⏭  {result['reason']}")
        elif result.get("action") == "preflight":
            print(f"📋 Task: {result['task_title']} ({result['task_id']})")
            print(f"📝 Preflight prompt ready for {result['project']}")
            print("\nNext step: Run preflight, then:")
            print(
                f"  unified_pipeline.py after_preflight {result['project']} <success|blocked> [error]"
            )

    elif cmd == "automated":
        # Run automated batch for all projects
        for project in PROJECT_CONFIGS.keys():
            if project == "default":
                continue

            result = start_batch(project, mode="automated")
            if result.get("action") == "skip":
                print(f"⏭  {project}: {result['reason']}")
            elif result.get("action") == "preflight":
                print(
                    f"📋 {project}: Task: {result['task_title']} ({result['task_id']})"
                )
                print(f"📝 Preflight prompt ready")

    elif cmd == "status":
        show_status()

    elif cmd == "unlock":
        if len(sys.argv) < 3:
            print("Usage: unified_pipeline.py unlock <project>")
            sys.exit(1)
        force_unlock(sys.argv[2])

    elif cmd == "after_preflight":
        if len(sys.argv) < 3:
            print(
                "Usage: unified_pipeline.py after_preflight <project> <success|blocked> [error]"
            )
            sys.exit(1)

        project = sys.argv[2]
        success = (
            sys.argv[3].lower() in ("true", "success") if len(sys.argv) > 3 else False
        )
        error = sys.argv[4] if len(sys.argv) > 4 else None
        result = after_preflight(project, success, error)
        print(json.dumps(result, indent=2))

    elif cmd == "after_impl":
        if len(sys.argv) < 3:
            print(
                "Usage: unified_pipeline.py after_impl <project> <true|false> [session] [error]"
            )
            sys.exit(1)

        project = sys.argv[2]
        success = sys.argv[3].lower() == "true" if len(sys.argv) > 3 else False
        session = sys.argv[4] if len(sys.argv) > 4 else None
        error = sys.argv[5] if len(sys.argv) > 5 else None
        result = after_implementation(project, success, session, error)
        print(json.dumps(result, indent=2))

    elif cmd == "after_verify":
        if len(sys.argv) < 3:
            print(
                "Usage: unified_pipeline.py after_verify <project> <clean|changes_made|blocked> [session]"
            )
            sys.exit(1)

        project = sys.argv[2]
        verdict = sys.argv[3] if len(sys.argv) > 3 else "blocked"
        session = sys.argv[4] if len(sys.argv) > 4 else None
        result = after_verification(project, verdict, session)
        print(json.dumps(result, indent=2))

    elif cmd == "after_commit":
        if len(sys.argv) < 3:
            print(
                "Usage: unified_pipeline.py after_commit <project> <true|false> [error]"
            )
            sys.exit(1)

        project = sys.argv[2]
        success = sys.argv[3].lower() == "true" if len(sys.argv) > 3 else False
        error = sys.argv[4] if len(sys.argv) > 4 else None
        result = after_commit(project, success, error)
        print(json.dumps(result, indent=2))

    else:
        print(f"❌ Unknown command: {cmd}")
        sys.exit(1)
