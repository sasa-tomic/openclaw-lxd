#!/bin/bash
# Pickle Heartbeat Pre-Check Script
# Runs all health checks and only wakes Pickle if there are issues

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$HOME/.pickle/logs"
STATE_FILE="$HOME/.pickle/heartbeat-state.json"
ALERT_FILE="$HOME/.pickle/heartbeat-alerts.txt"

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$STATE_FILE")"

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
ISSUES=()
ALERTS=()

# Initialize state file if missing
if [[ ! -f "$STATE_FILE" ]]; then
    echo '{"lastChecks":{},"lastIssues":[],"lastAlertTime":null}' > "$STATE_FILE"
fi

# Helper: add issue
add_issue() {
    ISSUES+=("$1")
}

# Helper: add alert
add_alert() {
    ALERTS+=("$1")
}

# =============================================================================
# 1. System Health Check
# =============================================================================

echo "=== SYSTEM HEALTH CHECK @ $TIMESTAMP ==="

# WhatsApp check
echo -n "WhatsApp: "
if wacli doctor 2>/dev/null | grep -q "AUTHENTICATED.*true"; then
    if ps aux | grep -q "[w]acli sync"; then
        echo "✅ Authenticated & sync running"
    else
        echo "⚠️ Authenticated but sync not running"
        add_issue "WhatsApp: sync not running"
        # Try to fix
        systemctl --user restart wacli-sync 2>/dev/null || add_alert "Failed to restart wacli-sync"
    fi
else
    echo "❌ NOT AUTHENTICATED - needs QR re-link!"
    add_issue "WhatsApp: NOT AUTHENTICATED"
    add_alert "WhatsApp needs QR re-auth (run: wacli auth)"
fi

# Signal sync check
echo -n "Signal sync: "
if systemctl --user is-active signal-obsidian-sync >/dev/null 2>&1; then
    echo "✅ active"
else
    echo "❌ DOWN"
    add_issue "Signal sync: DOWN"
    # Try to fix
    systemctl --user restart signal-obsidian-sync 2>/dev/null || add_alert "Failed to restart signal-obsidian-sync"
fi

# Message watcher check
echo -n "Message watcher: "
if systemctl --user is-active message-watcher >/dev/null 2>&1; then
    echo "✅ active"
else
    echo "❌ DOWN"
    add_issue "Message watcher: DOWN"
    # Try to fix
    systemctl --user restart message-watcher 2>/dev/null || add_alert "Failed to restart message-watcher"
fi

# wacli sync check
echo -n "wacli sync: "
if systemctl --user is-active wacli-sync >/dev/null 2>&1; then
    echo "✅ active"
else
    echo "❌ DOWN"
    add_issue "wacli sync: DOWN"
    # Try to fix
    systemctl --user restart wacli-sync 2>/dev/null || add_alert "Failed to restart wacli-sync"
fi

# =============================================================================
# 2. Log Check (last 15 minutes)
# =============================================================================

echo ""
echo "=== LOG CHECK ==="

ERROR_LOG=$(mktemp)
journalctl --user --since "15 minutes ago" --priority err --no-pager 2>/dev/null | head -200 > "$ERROR_LOG" || true

if [[ -s "$ERROR_LOG" ]]; then
    ERROR_COUNT=$(wc -l < "$ERROR_LOG")
    echo "⚠️ Found $ERROR_COUNT error log entries"
    
    # Sample first few errors
    SAMPLE=$(head -5 "$ERROR_LOG")
    add_issue "Errors in logs (last 15min): $ERROR_COUNT entries"
    add_alert "Recent error logs detected:\n$SAMPLE"
else
    echo "✅ No errors in recent logs"
fi

rm -f "$ERROR_LOG"

# =============================================================================
# 3. Calendar Check
# =============================================================================

echo ""
echo "=== CALENDAR CHECK ==="

CALENDAR_OUTPUT=$(mktemp)
cd /projects/automations/google-calendar && .venv/bin/python gcal.py list --days 1 > "$CALENDAR_OUTPUT" 2>&1 || {
    echo "❌ Calendar check failed"
    add_issue "Calendar check: failed to fetch"
}

if [[ -f "$CALENDAR_OUTPUT" ]] && grep -q "start" "$CALENDAR_OUTPUT"; then
    # Parse events and check for upcoming (<2h)
    NOW_EPOCH=$(date +%s)
    TWO_HOURS_LATER=$((NOW_EPOCH + 7200))
    
    # Extract event times (simplified - assumes format)
    while IFS= read -r line; do
        if [[ "$line" =~ start:\ ([0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}) ]]; then
            EVENT_TIME="${BASH_REMATCH[1]}"
            EVENT_EPOCH=$(date -d "$EVENT_TIME" +%s 2>/dev/null || echo "0")
            
            if [[ "$EVENT_EPOCH" -gt "$NOW_EPOCH" ]] && [[ "$EVENT_EPOCH" -lt "$TWO_HOURS_LATER" ]]; then
                # Event is within 2 hours
                EVENT_NAME=$(echo "$line" | grep -oP 'summary: \K.*' || echo "Unknown event")
                add_alert "📅 Event in <2h: $EVENT_NAME at $EVENT_TIME"
            fi
        fi
    done < "$CALENDAR_OUTPUT"
    
    echo "✅ Calendar checked"
else
    echo "ℹ️ No upcoming events"
fi

rm -f "$CALENDAR_OUTPUT"

# =============================================================================
# 4. Update State & Decide Action
# =============================================================================

# Update state file
jq --arg timestamp "$TIMESTAMP" \
   --argjson issues "$(printf '%s\n' "${ISSUES[@]}" | jq -R . | jq -s .)" \
   '.lastChecks.heartbeat = $timestamp | .lastIssues = $issues' \
   "$STATE_FILE" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"

# =============================================================================
# 5. Wake Pickle if Needed
# =============================================================================

if [[ ${#ALERTS[@]} -gt 0 ]]; then
    echo ""
    echo "=== WAKING PICKLE ==="
    
    # Build alert message
    ALERT_MESSAGE="🚨 Heartbeat Alert @ $TIMESTAMP"
    for alert in "${ALERTS[@]}"; do
        ALERT_MESSAGE="$ALERT_MESSAGE\n\n$alert"
    done
    
    # Write to file for debugging/logs
    echo -e "$ALERT_MESSAGE" > "$ALERT_FILE"
    
    # Wake Pickle with full context in the message (no file reading needed)
    # Use openclaw CLI to send system event with immediate wake
    openclaw system event --text "$(echo -e "$ALERT_MESSAGE")" --mode now 2>&1 | tee -a "$LOG_DIR/wake.log" || {
        echo "❌ Failed to wake Pickle via openclaw"
    }
    
    echo "✅ Pickle woken with alerts"
elif [[ ${#ISSUES[@]} -gt 0 ]]; then
    echo ""
    echo "ℹ️ Issues detected but auto-fixed (no alerts needed)"
else
    echo ""
    echo "✅ HEARTBEAT_OK - all systems nominal"
fi

exit 0
