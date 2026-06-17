#!/bin/bash
# headersHelper for Cowork managedMcpServers.
# Reads the stored access_token, refreshes it (public-client refresh, no
# client_secret) when near expiry, and prints a JSON object with the
# Authorization header that Cowork injects into each MCP request.
#
# Provider-agnostic: every endpoint/identifier comes from config.env, so the
# same script works against Cognito (this repo) or Entra. Also usable
# standalone for debugging: ./agentcore-token.sh
set -euo pipefail

CONFIG="${HOME}/.cowork-semantic-layer/config.env"
TOKEN_STORE="${HOME}/.cowork-semantic-layer/tokens.json"

if [ ! -f "$CONFIG" ]; then
  echo "Missing config: $CONFIG  Run: cd cowork && ./setup-cognito.sh" >&2
  exit 1
fi
if [ ! -f "$TOKEN_STORE" ]; then
  echo "No tokens. Run: cd cowork && ./setup-cognito.sh" >&2
  exit 1
fi

source "$CONFIG"

# Refresh 60s before expiry so Cowork never gets a token that dies mid-request.
NEED_REFRESH=$(python3 -c "
import json, time
t = json.load(open('$TOKEN_STORE'))
print('yes' if time.time() >= t['expires_at'] - 60 else 'no')
")

if [ "$NEED_REFRESH" = "yes" ]; then
  REFRESH_TOKEN=$(python3 -c "import json; print(json.load(open('$TOKEN_STORE')).get('refresh_token',''))")
  if [ -z "$REFRESH_TOKEN" ]; then
    echo "Token expired and no refresh_token stored. Run: ./setup-cognito.sh --force-login" >&2
    exit 1
  fi

  # Public-client refresh: client_id + refresh_token, NO client_secret.
  RESP=$(curl -s -X POST "${TOKEN_URL}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode "grant_type=refresh_token" \
    --data-urlencode "client_id=${CLIENT_ID}" \
    --data-urlencode "refresh_token=${REFRESH_TOKEN}")

  python3 << PYEOF
import json, time, sys
resp = json.loads('''$RESP''')
if 'access_token' not in resp:
    print('Token refresh failed: ' + json.dumps(resp), file=sys.stderr)
    sys.exit(1)
store = json.load(open('$TOKEN_STORE'))
store['access_token'] = resp['access_token']
store['expires_at'] = time.time() + resp['expires_in']
# Cognito does not rotate refresh tokens (Entra does); persist if present.
if 'refresh_token' in resp:
    store['refresh_token'] = resp['refresh_token']
json.dump(store, open('$TOKEN_STORE', 'w'))
PYEOF
fi

ACCESS_TOKEN=$(python3 -c "import json; print(json.load(open('$TOKEN_STORE'))['access_token'])")
# Emit ONLY Authorization — Cowork adds Mcp-Protocol-Version itself; duplicating
# it causes "Unsupported MCP protocol version".
printf '{"Authorization":"Bearer %s"}\n' "$ACCESS_TOKEN"
