#!/bin/bash
# Verify end-to-end message delivery capability
# Run this as a healthcheck - if it fails, alert immediately

set -euo pipefail

OPENCLAW="/home/openclaw/.npm-global/bin/openclaw"
TARGET="5996479639"
TIMESTAMP=$(date +%s)
TEST_MSG="healthcheck-$TIMESTAMP"

echo "=== MESSAGE DELIVERY HEALTHCHECK ==="
echo "Time: $(date -Iseconds)"

# 1. Send test message
echo "Sending test message..."
RESULT=$($OPENCLAW message send --channel telegram --target "$TARGET" --message "$TEST_MSG" 2>&1)

# 2. Verify it returned successfully
if echo "$RESULT" | grep -q "Sent via Telegram"; then
    MSG_ID=$(echo "$RESULT" | grep -oP 'Message ID: \K\d+' || echo "unknown")
    echo "✅ Message sent (ID: $MSG_ID)"
else
    echo "❌ CRITICAL: Message send failed"
    echo "Output: $RESULT"
    exit 1
fi

# 3. Check if openclaw is in PATH
if ! which openclaw &>/dev/null; then
    echo "⚠️ WARNING: openclaw not in PATH (systemd services will fail)"
    exit 1
fi

echo "✅ Delivery healthcheck passed"
