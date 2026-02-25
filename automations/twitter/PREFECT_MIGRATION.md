# Twitter Automation: Prefect Migration

**Status:** Complete (migrated 2026-02-22)
**Created:** 2026-02-21

## Why Prefect over Systemd?

| Aspect | Systemd | Prefect |
|--------|---------|---------|
| Logs | `journalctl -u twitter-*` | UI at :4200 + structured JSON |
| Coordination | None (tab conflicts) | Work pool serializes CDP access |
| Retries | Manual | Built-in with backoff |
| Visibility | Run `systemctl status` | Real-time UI, run history |
| Scheduling | `.timer` files | `prefect.yaml` |

## Migration Steps

### 1. Install Dependencies (using uv)
```bash
cd /projects/automations/twitter
uv sync  # Install from pyproject.toml
```

### 2. Start Prefect Server
```bash
# One-time setup
systemctl --user enable twitter-prefect-server.service
systemctl --user start twitter-prefect-server.service

# Or ad-hoc:
uv run prefect server start --host 127.0.0.1
```

### 3. Create Work Pool
```bash
uv run prefect work-pool create twitter-automation --type process
```

### 4. Start Worker
```bash
# One-time setup
systemctl --user enable twitter-prefect-worker.service
systemctl --user start twitter-prefect-worker.service

# Or ad-hoc:
uv run prefect worker start --pool twitter-automation
```

### 5. Deploy Flows
```bash
uv run prefect deploy --all
```

### 6. Disable Old Systemd Timers
```bash
systemctl --user stop twitter-engagement-autonomous.timer
systemctl --user disable twitter-engagement-autonomous.timer
# Repeat for all twitter-*.timer
```

### 7. Verify
- Open http://localhost:4200
- Check Deployments tab shows all 6 flows
- Check Schedules are correct
- Trigger a flow manually to test

## File Structure

```
twitter/
├── twitter_scheduler.py   # Prefect flow definitions
├── prefect.yaml           # Deployment config with schedules
├── post_original_content.py
├── post_thread.py
├── daily_strategy_eval.py
├── cdp_health_check.py
└── ...

heartbeat/
├── twitter-engagement.py
├── twitter_morning.py
└── ...

systemd-jobs/
├── twitter-prefect-server.service  # NEW: Prefect server
├── twitter-prefect-worker.service  # NEW: Prefect worker
├── twitter-*.service               # OLD: Can be disabled
└── twitter-*.timer                 # OLD: Can be disabled
```

## Schedules (from prefect.yaml)

| Flow | Schedule | Description |
|------|----------|-------------|
| engagement | 5x daily (9,12,15,18,21 UTC) | Reply to relevant tweets |
| content | 2x daily (10:30, 16:30 UTC) | Post original takes |
| thread | Wed 15:00 UTC | Weekly technical thread |
| eval | Daily 7:00 UTC | Strategy evaluation |
| research | Daily 8:00 UTC | Morning content research |
| health | Every 15 min | CDP connectivity check |

## Removed Jobs

| Job | Why Removed |
|-----|-------------|
| query-optimizer | LLM output ignored; auto-modified source code (brittle); engagement script has fallback |

## Ad-hoc Usage

Run a single flow (no server needed):
```bash
uv run python twitter_scheduler.py health
uv run python twitter_scheduler.py engagement
uv run python twitter_scheduler.py content
```

Run all flows once:
```bash
uv run python twitter_scheduler.py --all
```

Trigger a deployed flow via CLI:
```bash
uv run prefect deployment run 'twitter-cdp-health/cdp-health'
uv run prefect deployment run 'twitter-engagement/engagement'
```

View flow run logs:
```bash
uv run prefect flow-run logs <flow-run-id>
```

List recent runs:
```bash
uv run prefect flow-run ls --limit 10
```

## Rollback

If something goes wrong:
```bash
# Stop Prefect
systemctl --user stop twitter-prefect-worker.service
systemctl --user stop twitter-prefect-server.service

# Re-enable systemd timers
systemctl --user enable --now twitter-engagement-autonomous.timer
# ... repeat for others
```

## UI Access

- **Local:** http://localhost:4200
- **Remote:** Port-forward or use `prefect server start --host 0.0.0.0` (not recommended for public networks)

## Benefits After Migration

1. **No more CDP tab conflicts** - Work pool serializes execution
2. **Structured logs** - Filter by flow, time, status in UI
3. **Retry logic** - Failed flows auto-retry with backoff
4. **Run history** - See what ran when, what failed, what succeeded
5. **Manual triggers** - Run any flow from UI or CLI
6. **One place to look** - All Twitter automation in one dashboard
