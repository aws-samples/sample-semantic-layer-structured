#!/bin/bash
# One-shot setup for Cowork ↔ Semantic-Layer MCP Gateway (Cognito).
#
# Reuses the deployed Cognito MCP PKCE client (McpPkceClientId) — a PUBLIC
# client (no client_secret) that already has loopback redirect
# http://127.0.0.1:33418 registered and the scope `semantic-layer-mcp/invoke`
# that the Gateway CUSTOM_JWT authorizer validates. No new infrastructure.
#
# Mirrors the Claude Code OAuth path, but injects the bearer via a headersHelper
# because Cowork's native MCP OAuth fallback is non-functional (see README).
#
# Auto-derives endpoints from CloudFormation when AWS creds are present;
# otherwise reads/writes ~/.cowork-semantic-layer/config.env.
#
# Usage:
#   ./setup-cognito.sh                # connector (mode 1)
#   ./setup-cognito.sh --force-login  # re-authenticate even if tokens are valid
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STORE_DIR="$HOME/.cowork-semantic-layer"
TOKEN_STORE="$STORE_DIR/tokens.json"
CONFIG_ENV="$STORE_DIR/config.env"
HEADERS_HELPER="/usr/local/bin/agentcore-token-semantic-layer.sh"
CONFIG_LIBRARY="$HOME/Library/Application Support/Claude-3p/configLibrary"

AUTH_STACK="${AUTH_STACK:-semantic-layer-dev-auth}"
MCP_STACK="${MCP_STACK:-semantic-layer-dev-mcp-server}"

echo "=== Cowork Semantic-Layer Setup (Cognito) ==="
echo ""

mkdir -p "$STORE_DIR"

# --- Step 1: Gather values (CloudFormation first, then config.env) ---

cfn_out() {
  # cfn_out <stack> <OutputKey>
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" \
    --output text 2>/dev/null || echo ""
}

if command -v aws >/dev/null 2>&1 && aws sts get-caller-identity >/dev/null 2>&1; then
  echo "Reading CloudFormation outputs ($AUTH_STACK, $MCP_STACK) ..."
  HOSTED_UI=$(cfn_out "$AUTH_STACK" "McpHostedUiDomainUrl")
  CLIENT_ID=$(cfn_out "$AUTH_STACK" "McpPkceClientId")
  GATEWAY_URL=$(cfn_out "$MCP_STACK" "McpGatewayUrl")
  if [ -n "$HOSTED_UI" ]; then
    AUTHORIZE_URL="${HOSTED_UI}/oauth2/authorize"
    TOKEN_URL="${HOSTED_UI}/oauth2/token"
  fi
  SCOPES="openid profile email semantic-layer-mcp/invoke"
  REDIRECT_URI="http://127.0.0.1:33418"
fi

# Fall back to (or seed missing values from) config.env.
if [ -f "$CONFIG_ENV" ]; then
  # shellcheck disable=SC1090
  source "$CONFIG_ENV"
fi

# Final guard: anything still empty is fatal — fail loudly, no silent defaults.
: "${AUTHORIZE_URL:?Set AUTHORIZE_URL (deploy not found and no config.env)}"
: "${TOKEN_URL:?Set TOKEN_URL}"
: "${CLIENT_ID:?Set CLIENT_ID}"
: "${GATEWAY_URL:?Set GATEWAY_URL}"
: "${SCOPES:?Set SCOPES}"
REDIRECT_URI="${REDIRECT_URI:-http://127.0.0.1:33418}"

CALLBACK_PORT=$(python3 -c "import urllib.parse; print(urllib.parse.urlparse('$REDIRECT_URI').port)")

echo "  Authorize URL: $AUTHORIZE_URL"
echo "  Token URL:     $TOKEN_URL"
echo "  Client ID:     $CLIENT_ID"
echo "  Gateway URL:   $GATEWAY_URL"
echo "  Scopes:        $SCOPES"
echo "  Redirect URI:  $REDIRECT_URI"
echo ""

# --- Step 2: Persist config.env ---

cat > "$CONFIG_ENV" << EOF
AUTHORIZE_URL="$AUTHORIZE_URL"
TOKEN_URL="$TOKEN_URL"
CLIENT_ID="$CLIENT_ID"
GATEWAY_URL="$GATEWAY_URL"
SCOPES="$SCOPES"
REDIRECT_URI="$REDIRECT_URI"
EOF
chmod 600 "$CONFIG_ENV"
echo "Wrote $CONFIG_ENV"

# --- Step 3: Install headersHelper ---
# The installed copy reads ~/.cowork-semantic-layer; the in-repo agentcore-token.sh
# already points there, so install it verbatim.

echo "Installing headersHelper to $HEADERS_HELPER (requires sudo)..."
sudo cp "$SCRIPT_DIR/agentcore-token.sh" "$HEADERS_HELPER"
sudo chmod 755 "$HEADERS_HELPER"
echo "Installed $HEADERS_HELPER"

# --- Step 4: Decide whether to log in ---

SKIP_LOGIN=false
if [ -f "$TOKEN_STORE" ]; then
  STILL_VALID=$(python3 -c "
import json
try:
    t = json.load(open('$TOKEN_STORE'))
    print('yes' if t.get('refresh_token') else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")
  if [ "$STILL_VALID" = "yes" ]; then
    echo "Existing tokens with a refresh_token found. Skipping login."
    echo "(Run with --force-login to re-authenticate)"
    SKIP_LOGIN=true
  fi
fi

# --- Step 5: Cognito login (loopback PKCE, no client_secret) ---

if [ "$SKIP_LOGIN" != "true" ] || [ "${1:-}" = "--force-login" ]; then
  echo ""
  echo "Opening browser for Cognito login..."

  STATE=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
  VERIFIER=$(python3 -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode())")
  CHALLENGE=$(python3 -c "import hashlib, base64; print(base64.urlsafe_b64encode(hashlib.sha256('$VERIFIER'.encode()).digest()).rstrip(b'=').decode())")

  AUTH_URL="${AUTHORIZE_URL}?client_id=${CLIENT_ID}&response_type=code&scope=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$SCOPES'))")&redirect_uri=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$REDIRECT_URI'))")&state=${STATE}&code_challenge=${CHALLENGE}&code_challenge_method=S256"

  # Loopback server captures ?code= on any path (Cognito redirects to "/").
  python3 << PYEOF &
import http.server, urllib.parse, sys, threading

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        if state != "$STATE" or not code:
            self.send_response(400); self.end_headers()
            self.wfile.write(b"Login failed"); return
        with open("/tmp/cowork_semlayer_auth_code", "w") as f:
            f.write(code)
        self.send_response(200); self.end_headers()
        self.wfile.write(b"<h1>Login complete</h1><p>You can close this tab.</p>")
        threading.Timer(0.5, lambda: sys.exit(0)).start()
    def log_message(self, *args):
        pass

http.server.HTTPServer(("127.0.0.1", $CALLBACK_PORT), Handler).serve_forever()
PYEOF
  CALLBACK_PID=$!
  sleep 0.5

  open "$AUTH_URL" 2>/dev/null || echo "Open this URL: $AUTH_URL"

  echo "Waiting for login callback (timeout 5 min)..."
  for i in $(seq 1 300); do
    if [ -f /tmp/cowork_semlayer_auth_code ]; then break; fi
    sleep 1
  done

  kill $CALLBACK_PID 2>/dev/null || true
  wait $CALLBACK_PID 2>/dev/null || true

  if [ ! -f /tmp/cowork_semlayer_auth_code ]; then
    echo "ERROR: Login timed out. Run setup-cognito.sh again." >&2
    exit 1
  fi

  CODE=$(cat /tmp/cowork_semlayer_auth_code)
  rm -f /tmp/cowork_semlayer_auth_code

  # Public-client code exchange: client_id + PKCE verifier, NO client_secret.
  RESP=$(curl -s -X POST "${TOKEN_URL}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode "grant_type=authorization_code" \
    --data-urlencode "client_id=${CLIENT_ID}" \
    --data-urlencode "code=${CODE}" \
    --data-urlencode "redirect_uri=${REDIRECT_URI}" \
    --data-urlencode "code_verifier=${VERIFIER}")

  python3 << PYEOF
import json, time, sys
resp = json.loads('''$RESP''')
if "access_token" not in resp:
    print("Token exchange failed: " + json.dumps(resp), file=sys.stderr)
    sys.exit(1)
tokens = {
    "access_token": resp["access_token"],
    "refresh_token": resp.get("refresh_token", ""),
    "expires_at": time.time() + resp["expires_in"],
}
if not tokens["refresh_token"]:
    print("WARNING: no refresh_token returned — re-run with --force-login on expiry.", file=sys.stderr)
with open("$TOKEN_STORE", "w") as f:
    json.dump(tokens, f)
import os; os.chmod("$TOKEN_STORE", 0o600)
print("Tokens saved to $TOKEN_STORE")
PYEOF
fi

# --- Step 6: Write managedMcpServers to configLibrary (mode 1) ---

python3 << PYEOF
import json, os, uuid

config_lib = "$CONFIG_LIBRARY"
os.makedirs(config_lib, exist_ok=True)
meta_path = os.path.join(config_lib, "_meta.json")

try:
    with open(meta_path) as f:
        meta = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    meta = {"entries": []}

profile_id = meta.get("appliedId")
if not profile_id:
    profile_id = str(uuid.uuid4())
    meta["appliedId"] = profile_id
    meta["entries"] = [{"id": profile_id, "name": "Default"}]

profile_path = os.path.join(config_lib, f"{profile_id}.json")
try:
    with open(profile_path) as f:
        profile = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    profile = {}

servers = [s for s in profile.get("managedMcpServers", [])
           if s.get("name") != "Semantic Layer MCP"]
servers.append({
    "url": "$GATEWAY_URL",
    "transport": "http",
    "name": "Semantic Layer MCP",
    "headersHelper": "$HEADERS_HELPER",
    "headersHelperTtlSec": 900,
})
profile["managedMcpServers"] = servers

with open(profile_path, "w") as f:
    json.dump(profile, f, indent=2)
with open(meta_path, "w") as f:
    json.dump(meta, f, indent=2)

if not profile.get("inferenceProvider"):
    print("WARNING: this configLibrary profile has no inferenceProvider set.")
    print("         managedMcpServers is SILENTLY IGNORED without it — complete the")
    print("         3P Bedrock inference setup (Developer > Configure third-party inference).")
print(f"Wrote managedMcpServers to {profile_path}")
PYEOF

# --- Step 7: Clear caches ---

rm -f "$HOME/Library/Application Support/Claude-3p/plugin-settings.json"
security delete-generic-password -s "Claude-credentials" 2>/dev/null || true
find "$HOME/Library/Application Support/Claude-3p/" -name ".credentials.json" -delete 2>/dev/null || true
find "$HOME/Library/Application Support/Claude-3p/" -name "*mcp*auth*" -delete 2>/dev/null || true
echo "Cleared caches"

echo ""
echo "=== Setup complete (Cognito connector) ==="
echo "Restart Cowork (Cmd+Q, reopen) to connect."
echo "Tools (ListOntologies, OntologyQuery, MetadataQuery, QuerySuggestions)"
echo "appear under Customize > Connectors > Semantic Layer MCP."
