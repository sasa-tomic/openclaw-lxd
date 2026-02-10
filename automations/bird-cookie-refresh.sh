#!/usr/bin/env bash
# Refresh bird CLI cookies from satbox Chrome via CDP
# Extracts auth_token and ct0 from the Chrome instance with remote debugging
set -euo pipefail

CDP_HOST="192.168.0.13"
CDP_PORT="9222"
ENV_FILE="$HOME/.openclaw/.env"
SECRETS_FILE="$HOME/.config/bird/secrets.env"
NODE_PATH="$(npm root -g)"

# Get the first page target's id from the CDP target list
PAGE_WS=$(curl -sf "http://${CDP_HOST}:${CDP_PORT}/json" 2>/dev/null \
  | python3 -c "import sys,json; pages=json.load(sys.stdin); print(pages[0]['id'] if pages else '')" 2>/dev/null || true)

if [[ -z "$PAGE_WS" ]]; then
  echo "WARN: No CDP targets found at ${CDP_HOST}:${CDP_PORT} (Chrome not running / not reachable). Skipping refresh." >&2
  exit 0
fi

# Extract cookies via CDP WebSocket
COOKIES=$(NODE_PATH="$NODE_PATH" node -e "
const WebSocket = require('ws');
const ws = new WebSocket('ws://${CDP_HOST}:${CDP_PORT}/devtools/page/${PAGE_WS}');
const timeout = setTimeout(() => { console.error('Timeout'); process.exit(1); }, 10000);
ws.on('open', () => {
  ws.send(JSON.stringify({id:1, method:'Network.getCookies', params:{urls:['https://x.com']}}));
});
ws.on('message', (data) => {
  clearTimeout(timeout);
  const resp = JSON.parse(data);
  const cookies = resp?.result?.cookies || [];
  const result = {};
  cookies.forEach(c => {
    if (c.name === 'auth_token' || c.name === 'ct0') result[c.name] = c.value;
  });
  if (result.auth_token && result.ct0) {
    console.log(JSON.stringify(result));
  } else {
    console.error('Missing cookies - not logged into X?');
    process.exit(1);
  }
  ws.close();
});
ws.on('error', (e) => { console.error('WS error:', e.message); process.exit(1); });
" 2>/dev/null)

if [[ -z "$COOKIES" ]]; then
  echo "ERROR: Failed to extract cookies from CDP" >&2
  exit 1
fi

AUTH_TOKEN=$(echo "$COOKIES" | python3 -c "import sys,json; print(json.load(sys.stdin)['auth_token'])")
CT0=$(echo "$COOKIES" | python3 -c "import sys,json; print(json.load(sys.stdin)['ct0'])")

# Check if values actually changed
CURRENT_AUTH=$(grep '^AUTH_TOKEN=' "$ENV_FILE" 2>/dev/null | sed 's/AUTH_TOKEN=//' || true)
if [[ "$AUTH_TOKEN" == "$CURRENT_AUTH" ]]; then
  echo "Cookies unchanged, skipping update"
  exit 0
fi

# Persist for systemd units that use EnvironmentFile (no 'export', no quotes - systemd format)
mkdir -p "$(dirname "$ENV_FILE")"
cat > "$ENV_FILE" << EOF
# Bird CLI (X/Twitter) - @DecentCloud_org cookies (auto-refreshed $(date -u +%Y-%m-%dT%H:%M:%SZ))
AUTH_TOKEN=${AUTH_TOKEN}
CT0=${CT0}
EOF

# Persist for shell scripts that source exports explicitly
mkdir -p "$(dirname "$SECRETS_FILE")"
cat > "$SECRETS_FILE" << EOF
# bird auth cookies for X (auto-refreshed $(date -u +%Y-%m-%dT%H:%M:%SZ))
export AUTH_TOKEN='${AUTH_TOKEN}'
export CT0='${CT0}'
EOF
chmod 600 "$SECRETS_FILE"

echo "Cookies refreshed: auth_token=${AUTH_TOKEN:0:10}... ct0=${CT0:0:10}..."
