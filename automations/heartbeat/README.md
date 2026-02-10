# Heartbeat Automations

This directory contains standalone automation scripts that were previously embedded in `HEARTBEAT.md`. They now run as dedicated cron jobs, making the heartbeat much leaner and more focused on critical system health checks.

## Scripts

| Script | Purpose | Schedule |
|--------|---------|----------|
| `check-missed-crons.sh` | Detect and reschedule cron jobs that missed their time | Every 30 min |
| `clean-heartbeat.sh` | Keep HEARTBEAT.md clean and focused | Daily 5 AM |
| `obsidian-note-review.sh` | Review recently modified notes, extract action items | Every 30 min |
| `email-check.sh` | Check for important emails, skip marketing | Every 15 min |
| `obsidian-maintenance.sh` | Daily vault organization and health checks | Daily 9 AM |
| `twitter-morning.sh` | Morning Twitter research for @DecentCloud_org | Daily 8 AM |
| `twitter-engagement.sh` | Find and track Twitter engagement opportunities | 10 AM, 2 PM, 6 PM |
| `cron-wrapper.sh` | Wrapper for executing scripts and sending results | Called by cron |

## Cron Jobs

The scripts are triggered by OpenClaw cron jobs:

```bash
# List all cron jobs
cron list

# View job details
cron runs <jobId>

# Manually trigger a job
cron run <jobId>

# Disable/enable a job
cron update <jobId> --patch '{"enabled": false}'
```

## How It Works

1. **Cron fires** → Sends `RUN_<TASK_NAME>` system event to main session
2. **Main session receives** → I detect the system event and run the appropriate script
3. **Script executes** → Writes results to `/tmp/<task>-results.txt`
4. **Results processed** → I send the formatted results to Telegram via message tool

### Special Scripts

**`check-missed-crons.sh`** - Self-healing for cron system:
- Scans all enabled cron jobs (cron list passed via `/tmp/cron-list-for-missed-check.json`)
- Finds jobs where `nextRunAtMs` is in the past
- Writes missed job IDs to `/tmp/missed-crons-results.txt` for main session to process
- Tracks notified jobs to avoid spamming about the same missed job
- Only notifies about a job once per hour (to give it a chance to recover)

**Note:** This script requires the main session to provide the cron list. When the `RUN_CHECK_MISSED_CRONS` event fires, the main session should:
1. Get the cron list via `cron list`
2. Write it to `/tmp/cron-list-for-missed-check.json`
3. Run the script
4. Read `/tmp/missed-crons-results.txt`
5. Call `cron run <jobId>` for each missed job

**`clean-heartbeat.sh`** - Keep HEARTBEAT.md minimal:
- Compares current HEARTBEAT.md to canonical version
- If bloated (>50% larger), overwrites with clean version
- Runs daily at 5 AM (after Clawdbot update, before morning jobs)
- Canonical version has only: System Health, Log Check, Calendar, Sync Verification

## State Tracking

Each script maintains its own state:

| Script | State File | Tracks |
|--------|------------|--------|
| `check-missed-crons.sh` | `memory/missed-crons-state.json` | Last check, notified jobs |
| `obsidian-note-review.sh` | `memory/obsidian-note-review-state.json` | Last check timestamp |
| `email-check.sh` | `memory/email-check-state.json` | Last email ID, last check |
| `obsidian-maintenance.sh` | `memory/obsidian-maintenance-state.json` | Last maintenance date |
| `twitter-morning.sh` | `memory/twitter-state.json` | Research runs, posts, pending |
| `twitter-engagement.sh` | `memory/twitter-state.json` | Engaged tweets, pending |

## Adding New Automations

1. Create the script in this directory (or link from elsewhere)
2. Make it executable: `chmod +x script.sh`
3. Add a cron job using `cron add`
4. Document it in the table above
5. Handle the system event in main session (detect `RUN_<TASK_NAME>`)

## Troubleshooting

**Script not running?**
- Check cron list: `cron list`
- Verify job is enabled
- Check next run time

**Results not appearing?**
- Check if script writes to `/tmp/<task>-results.txt`
- Verify script has proper permissions
- Check script output: `/projects/automations/heartbeat/script.sh`

**State file issues?**
- State files auto-create if missing
- Manually delete to reset state

## Disabled Old Jobs

The following cron jobs were disabled in favor of the new system:

- `hourly-note-review` → Replaced by `obsidian-note-review` (every 30 min)
- `Decent Cloud Twitter research` → Replaced by `twitter-morning` (8 AM)

These remain in the system but are disabled. Delete if no longer needed:
```bash
cron remove <jobId>
```
