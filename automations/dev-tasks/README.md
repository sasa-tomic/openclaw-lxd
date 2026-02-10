# Dev Pipeline - Usage

**Updated:** 2026-02-01 - Now runs every 4 hours with per-project locking

---

## Quick Start

### Manual Run (Specific Task)
```bash
cd /projects/automations/dev-tasks && python3 unified_pipeline.py manual --task-id <id>
```

### Manual Run (Next P0/P1 Task)
```bash
cd /projects/automations/dev-tasks && python3 unified_pipeline.py manual --next
```

### Check Pipeline Status
```bash
cd /projects/automations/dev-tasks && python3 unified_pipeline.py status
```

### Force Unlock (Emergency Only)
```bash
cd /projects/automations/dev-tasks && python3 unified_pipeline.py unlock <project>
# Example: python3 unified_pipeline.py unlock decent-cloud
```

---

## Automation

**Cron schedule:** Every 4 hours (`0 */4 * * *`)

**Next runs:**
- 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 (CET)

**Jobs:**
- `unified-dev-run-every-4h` - Runs `unified_pipeline.py automated`
- `hourly-note-review` - Runs note watcher
- `agent-monitor` - Monitors active dev agents

---

## Per-Project Locking

Each project has independent lock file:

| Project | Lock File | State File |
|---------|-----------|-------------|
| decent-cloud | `memory/dev-pipeline-decent-cloud.lock` | `memory/dev-pipeline-state-decent-cloud.json` |
| voki | `memory/dev-pipeline-voki.lock` | `memory/dev-pipeline-state-voki.json` |

**Projects run independently** - no cross-blocking.

---

## Pipeline Flow (All Steps)

```
Step 0: Resume Check
  ↓
Step 1: Start (pick next P0/P1 task)
  ↓
Step 2: Preflight (clean slate)
  ↓
Step 3: Implement (45 min timeout)
  ↓
Step 4: Verify (fresh agent, max 3 retries)
  ↓
Step 5: Commit (if verified clean)
  ↓
Step 6: Next/Done (check for more tasks)
```

**Timeouts:**
- Preflight: 45 min
- Implementation: 45 min
- Verification: 45 min (max 3 attempts)
- Total batch: 2 hours

---

## Lock Management

**Auto-cleanup:**
- Locks release on completion (success/failure)
- Signal traps (SIGINT, SIGTERM, etc.) trigger cleanup
- Stale locks (>2h old) are auto-broken

**Manual override:**
```bash
python3 unified_pipeline.py unlock <project>
```

Use only if pipeline is truly stuck (process crashed without cleanup).

---

## State Tracking

**Per-project state files** track:
- Current status (idle, preflight, implementing, verifying, committing, done, failed)
- Current task ID and title
- Verify attempts (max 3)
- Session keys of spawned agents
- Completed tasks list
- Running by (manual/automated)

**Example state:**
```json
{
  "status": "implementing",
  "current_task_id": "dc-regions",
  "current_task_title": "Move geographic region mapping to shared crate",
  "project": "decent-cloud",
  "verify_attempts": 0,
  "running_by": "manual"
}
```

---

## Error Recovery

### Stale Lock Detected
```
⚠️  Found stale lock (PID 12345 not running)
   Removing stale lock for 'decent-cloud'...
```
→ Lock auto-removed, pipeline proceeds normally.

### Process Killed (Ctrl+C)
```
🛑 Signal received, cleaning up...
   Lock released for 'decent-cloud'
```
→ Cleanup runs, lock removed.

### Both Manual + Cron Race
```
❌ Project 'decent-cloud' already locked
   Locked by: manual
   Locked at: 2026-02-01T12:55:00Z
   PID: 54321
   Task: dc-regions
```
→ Cron skips, manual continues uninterrupted.

---

## Troubleshooting

### Pipeline Won't Start
```bash
# Check if locked
python3 unified_pipeline.py status

# Force unlock (if stale)
python3 unified_pipeline.py unlock decent-cloud
```

### Agent Won't Spawn
- Check gateway logs for spawn errors
- Verify `runTimeoutSeconds: 2700` is being used
- Check available agent IDs with `agents_list`

### Lock File Corruption
- Delete lock file manually:
  ```bash
  rm -f /home/openclaw/clawd/memory/dev-pipeline-*.lock
  ```
- Reset state:
  ```bash
  rm -f /home/openclaw/clawd/memory/dev-pipeline-state-*.json
  ```

---

*For detailed process rules, see `/home/openclaw/clawd/docs/DEV_PROCESS.md`*
