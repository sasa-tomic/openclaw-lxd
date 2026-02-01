# Dev Task Manager

Automated task tracking and agent spawning for development work.

## Overview

- **Task files:** `/projects/Notes/Pickle/dev-tasks/` (Obsidian, synced)
- **Pipeline state:** `~/clawd/memory/dev-pipeline-state.json`
- **Cron job:** `nightly-dev-run` (02:00 CET)

## Pipeline Flow

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  BACKLOG    │ ──▶ │  IMPLEMENT   │ ──▶ │   VERIFY    │
│  (P0/P1)    │     │   (agent)    │     │   (agent)   │
└─────────────┘     └──────────────┘     └─────────────┘
                           │                    │
                           │              ┌─────┴─────┐
                           │              │           │
                           ▼              ▼           ▼
                    BLOCKED          CHANGES?     CLEAN
                    (stop)           (retry ×3)   (commit)
                                          │           │
                                          └─────┬─────┘
                                                ▼
                                        ┌─────────────┐
                                        │    DONE     │
                                        └─────────────┘
```

- **Implement:** Fresh agent implements task, runs relevant tests
- **Verify:** Different agent with fresh context, runs full test suite
- **Retry:** If verifier makes changes, re-verify (max 3 attempts)
- **Commit:** Only if verification passes with clean git diff

## Task Files

| File | Purpose |
|------|---------|
| `BACKLOG.md` | Prioritized queue (P0-P3) |
| `IN-PROGRESS.md` | Currently being worked on |
| `BLOCKED.md` | Waiting on dependencies |
| `DONE.md` | Completed (rolling log) |

## Task Format

```markdown
## [P1] Task title here
- ID: unique-id
- Project: decent-cloud
- Created: 2026-01-31
- Context: Description and any relevant notes
```

Priority levels: P0 (critical), P1 (high), P2 (medium), P3 (low)

## Scripts

### add_task.py
Add a task to the backlog:
```bash
python3 add_task.py "Task title" -p decent-cloud -P P1 -c "Context here"
```

### nightly_dev_run.py
Called by cron at 02:00. Picks highest priority task, outputs spawn request.
```bash
python3 nightly_dev_run.py
# Output: JSON with action=spawn or action=skip
```

### agent_monitor.py
Called by cron every 4h. Checks agent status, detects stuck agents.
```bash
python3 agent_monitor.py
# Output: JSON status report

# Mark task complete/blocked/failed:
python3 agent_monitor.py complete <task_id> "Result description"
python3 agent_monitor.py blocked <task_id> "Reason"
python3 agent_monitor.py failed <task_id> "Reason"
```

### task_manager.py
Core library. Also initializes files when run directly:
```bash
python3 task_manager.py
```

## Workflow

1. Tasks added to BACKLOG.md (manually or via add_task.py)
2. Nightly job (02:00) picks top task → spawns agent → moves to IN-PROGRESS
3. Monitor job (every 4h) checks progress, alerts if stuck
4. On completion: moves to DONE with result

## Concurrency

Max 2 concurrent agents (configurable in nightly_dev_run.py).
