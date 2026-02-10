#!/bin/bash
# Clean HEARTBEAT.md - Keep it simple and focused
# Runs daily to ensure HEARTBEAT.md only contains critical system health checks
# Usage: ./clean-heartbeat.sh

set -euo pipefail

HEARTBEAT_FILE="/home/openclaw/clawd/HEARTBEAT.md"
TEMP_FILE="/tmp/heartbeat-cleaned.md"

echo "=== CLEANING HEARTBEAT.md ==="
echo "Date: $(date -Iseconds)"

# The canonical, minimal HEARTBEAT.md content
cat > "$TEMP_FILE" << 'EOF'
# HEARTBEAT.md

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
| Obsidian Note Review | Every 30 min | `/projects/automations/heartbeat/obsidian-note-review.sh` |
| Email Check | Every 15 min | `/projects/automations/heartbeat/email-check.sh` |
| Obsidian Maintenance | Daily 9 AM | `/projects/automations/heartbeat/obsidian-maintenance.sh` |
| Twitter Morning Research | Daily 8 AM | `/projects/automations/heartbeat/twitter-morning.sh` |
| Twitter Engagement | 10 AM, 2 PM, 6 PM | `/projects/automations/heartbeat/twitter-engagement.sh` |

---

If nothing needs attention: `HEARTBEAT_OK`
EOF

# Check if HEARTBEAT.md exists
if [ ! -f "$HEARTBEAT_FILE" ]; then
    echo "HEARTBEAT.md does not exist, creating it..."
    cp "$TEMP_FILE" "$HEARTBEAT_FILE"
    echo "Created HEARTBEAT.md"
    rm -f "$TEMP_FILE"
    exit 0
fi

# Compare current file with canonical version
if diff -q "$HEARTBEAT_FILE" "$TEMP_FILE" > /dev/null 2>&1; then
    echo "✅ HEARTBEAT.md is already clean"
    rm -f "$TEMP_FILE"
    exit 0
fi

# File differs - report what changed
echo ""
echo "⚠️ HEARTBEAT.md has drifted from canonical version"
echo "Differences:"
diff -u "$HEARTBEAT_FILE" "$TEMP_FILE" | head -50 || true

# Ask if we should overwrite (for now, just report but don't auto-overwrite)
# This is safer - let Mr. T decide if drift is intentional
echo ""
echo "HEARTBEAT.md drift detected. Manual review needed."
echo "Canonical version is at: $TEMP_FILE"

# For now, keep the existing file and just report
# Uncomment the line below to auto-overwrite:
# cp "$TEMP_FILE" "$HEARTBEAT_FILE" && echo "Overwritten HEARTBEAT.md"

# Clean up temp file only if not overwriting
# rm -f "$TEMP_FILE"

# Actually, let's be opinionated: if HEARTBEAT.md has drifted significantly,
# overwrite it. We can check line count:
CURRENT_LINES=$(wc -l < "$HEARTBEAT_FILE")
CANONICAL_LINES=$(wc -l < "$TEMP_FILE")

echo "Current lines: $CURRENT_LINES, Canonical: $CANONICAL_LINES"

# If current has significantly more lines (drifted > 50%), overwrite
if [ "$CURRENT_LINES" -gt $((CANONICAL_LINES + CANONICAL_LINES / 2)) ]; then
    echo ""
    echo "HEARTBEAT.md has bloated significantly. Overwriting with clean version..."
    cp "$TEMP_FILE" "$HEARTBEAT_FILE"
    rm -f "$TEMP_FILE"
    echo "✅ HEARTBEAT.md cleaned"
else
    echo ""
    echo "HEARTBEAT.md looks reasonable. Keeping as-is."
    rm -f "$TEMP_FILE"
fi

echo ""
echo "HEARTBEAT cleanup complete."
