#!/usr/bin/env python3
"""Clean HEARTBEAT.md - Keep it simple and focused

Runs daily to ensure HEARTBEAT.md only contains critical system health checks.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.config import HEARTBEAT_FILE

HEARTBEAT_FILE = Path(HEARTBEAT_FILE)

CANONICAL_CONTENT = """# HEARTBEAT.md

## 🚨 System Health Check (every heartbeat)

**Run this FIRST on every heartbeat.** Alert Mr. T immediately if anything is broken.

```bash
# Quick health check script
echo "=== SYSTEM HEALTH ==="

# WhatsApp
echo -n "WhatsApp: "
if wacli doctor 2>/dev/null | grep -q "AUTHENTICATED.*true"; then
  if ps aux | grep -q "[w]acli sync"; then
    echo "✅ Authenticated & sync running"
  else
    echo "⚠️ Authenticated but sync not running"
  fi
else
  echo "❌ NOT AUTHENTICATED - needs QR re-link!"
fi

# Signal
echo -n "Signal sync: "
systemctl --user is-active signal-obsidian-sync 2>/dev/null || echo "❌ DOWN"

# Message watcher
echo -n "Message watcher: "
systemctl --user is-active message-watcher 2>/dev/null || echo "❌ DOWN"

# wacli sync
echo -n "wacli sync: "
systemctl --user is-active wacli-sync 2>/dev/null || echo "❌ DOWN"
```

**If anything is broken:**
1. Try to fix it (restart service, etc.)
2. If can't fix → **immediately alert Mr. T** (don't wait, don't say HEARTBEAT_OK)
3. Track issue in `memory/heartbeat-state.json`

---

## 📋 Log Check (every heartbeat)

**Check recent logs for errors and fix them proactively.**

```bash
# Check last 15 minutes of journal logs for errors
journalctl --user --since "15 minutes ago" --priority err --no-pager 2>/dev/null | head -200

# Check other logs
journalctl --user --since "15 minutes ago" --no-pager | head -200
```

**Common fixes:**
- "command not found" → check if binary path changed (clawdbot→openclaw, etc.)
- "permission denied" → check file permissions
- "connection refused" → restart the service
- Path errors → check for stale /home/moltbot references, update to /home/openclaw

**If you find and fix an issue:** mention it briefly when responding
**If all is well:** just reply HEARTBEAT_OK (don't bother Mr. T)
**If you can't fix something:** alert Mr. T immediately

---

## Calendar Check (every heartbeat)

**I own this calendar.** Check for upcoming events and alert Mr. T.

```bash
cd /projects/automations/google-calendar && .venv/bin/python gcal.py list --days 7
```

**Alert Mr. T when:**
- Event is <2h away (urgent reminder)

**Note:** Daily email-style calendar summaries are handled by the "Morning TODO review" cron job.

---

## WhatsApp/Signal Sync Verification

Quick health checks for messaging sync services:

**WhatsApp:**
- `wacli doctor` should show `AUTHENTICATED: true`
- wacli-sync service should be running
- Note: `CONNECTED: false` is cosmetic — messages sync if AUTHENTICATED is true

**Signal:**
- `systemctl --user is-active signal-obsidian-sync` should return "active"
- Check `~/.signal-cli/obsidian-sync.log` for errors

**If services are down:**
- Restart: `systemctl --user restart <service-name>`
- If AUTHENTICATED: false → alert Mr. T (needs QR re-auth for WhatsApp)

---

## Background Automation Status

These tasks are now handled by dedicated cron jobs (see `cron list`):

| Task | Schedule | Script |
|------|----------|--------|
| Obsidian Note Review | Every 30 min | `/projects/automations/heartbeat/obsidian_note_review.py` |
| Email Check | Every 15 min | `/projects/automations/heartbeat/email_check.py` |
| Obsidian Maintenance | Daily 9 AM | `/projects/automations/heartbeat/obsidian_maintenance.py` |
| Twitter Morning Research | Daily 8 AM | `/projects/automations/heartbeat/twitter_morning.py` |
| Twitter Engagement | 10 AM, 2 PM, 6 PM | `/projects/automations/heartbeat/twitter_engagement.py` |

---

If nothing needs attention: `HEARTBEAT_OK`
"""


def main():
    print("=== CLEANING HEARTBEAT.md ===")
    print(f"Date: {datetime.now().isoformat()}")

    if not HEARTBEAT_FILE.exists():
        print("HEARTBEAT.md does not exist, creating it...")
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(CANONICAL_CONTENT)
        print("Created HEARTBEAT.md")
        return 0

    current = HEARTBEAT_FILE.read_text()
    canonical = CANONICAL_CONTENT

    if current == canonical:
        print("✅ HEARTBEAT.md is already clean")
        return 0

    # Check line counts
    current_lines = len(current.split("\n"))
    canonical_lines = len(canonical.split("\n"))

    print(f"\n⚠️ HEARTBEAT.md has drifted from canonical version")
    print(f"Current lines: {current_lines}, Canonical: {canonical_lines}")

    # If current has significantly more lines (drifted > 50%), overwrite
    if current_lines > canonical_lines * 1.5:
        print(
            "\nHEARTBEAT.md has bloated significantly. Overwriting with clean version..."
        )
        HEARTBEAT_FILE.write_text(canonical)
        print("✅ HEARTBEAT.md cleaned")
    else:
        print("\nHEARTBEAT.md looks reasonable. Keeping as-is.")

    print("\nHEARTBEAT cleanup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
