"""MCP OAuth Proxy Lambda (semantic-layer).

Sits between Claude Code / VS Code / Cursor and the semantic-layer MCP query
gateway so those clients can connect with a browser-based Cognito OAuth login.
Transport-agnostic (pure stdlib + boto3).

Auth architecture:
  Inbound  (client → proxy → gateway):
    The proxy runs a 3-legged OAuth flow with Cognito (Authorization Code +
    PKCE). ``/authorize`` injects ``semantic-layer-mcp/invoke`` into the scope so
    the resulting access_token carries it. The proxy forwards the token directly
    to the AgentCore MCP gateway, whose CUSTOM_JWT authorizer validates it. No
    token swap in the proxy.

  Outbound (gateway → Lambda target):
    The gateway invokes the mcp-tools Lambda via GATEWAY_IAM_ROLE (SigV4). No
    additional credentials needed there.

Environment variables:
  GATEWAY_URL_SSM_PARAM – SSM param name the mcp-server stack writes the gateway URL to
  COGNITO_DOMAIN        – Cognito hosted-UI base URL (https://xxx.auth.<region>.amazoncognito.com)
  CLIENT_ID             – Cognito PKCE 3LO app client id (no secret)
  CLIENT_SECRET         – empty for PKCE clients
  GATEWAY_SCOPE         – the resource-server scope (semantic-layer-mcp/invoke)
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache


def _require_https(url: str) -> str:
    """Raise ValueError if *url* does not use the https scheme.

    Prevents mis-configured env vars (http:// or file://) from reaching urllib.

    :param url: the URL to validate.
    :returns: the original url, unchanged.
    :raises ValueError: if the scheme is not https.
    """
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError(f"Refusing non-HTTPS URL: {url!r}")
    return url

import boto3

COGNITO_DOMAIN = os.environ.get("COGNITO_DOMAIN", "")
CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
GATEWAY_URL_SSM_PARAM = os.environ.get("GATEWAY_URL_SSM_PARAM", "")

# Scope required by the AgentCore MCP gateway's CUSTOM_JWT authorizer. Injected
# into every /authorize redirect so the returned access_token always carries it
# and can be forwarded directly to the gateway with no token swap.
GATEWAY_SCOPE = os.environ.get("GATEWAY_SCOPE", "semantic-layer-mcp/invoke")

# Browser-like User-Agent for the server-to-server token exchange with Cognito.
# The Cognito user pool sits behind a WAFv2 Web ACL with the AWS Bot Control
# managed rule group, which BLOCKS (403 + empty body) any request whose
# User-Agent looks like an HTTP library (e.g. urllib's default "Python-urllib/3.x").
# The browser /authorize leg passes because the user's real browser calls Cognito
# directly, but this back-channel POST /oauth2/token is made by the Lambda and was
# being silently blocked — leaving the token exchange to fail on every retry.
# Sending a browser-style UA gets the request past Bot Control to the real OAuth
# handler. See docs/MCP_SERVER.md for the full diagnosis.
COGNITO_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


@lru_cache(maxsize=1)
def _get_gateway_url() -> str:
    """Resolve the MCP gateway URL from SSM Parameter Store.

    Cached across warm Lambda invocations. The parameter is written by the
    mcp-server stack after the gateway is created, avoiding a circular stack
    dependency between the proxy stack and mcp-server.

    :returns: the MCP gateway base URL.
    :raises RuntimeError: if neither GATEWAY_URL nor GATEWAY_URL_SSM_PARAM is set.
    """
    env = os.environ.get("GATEWAY_URL")
    if env:
        return env
    if not GATEWAY_URL_SSM_PARAM:
        raise RuntimeError("Neither GATEWAY_URL nor GATEWAY_URL_SSM_PARAM is set")
    ssm = boto3.client("ssm")
    return ssm.get_parameter(Name=GATEWAY_URL_SSM_PARAM)["Parameter"]["Value"]


def lambda_handler(event: dict, context) -> dict:
    """Route MCP OAuth + proxy requests.

    :param event: the API Gateway (HTTP API) proxy event.
    :param context: the Lambda context (unused).
    :returns: an API Gateway proxy response dict.
    """
    path = event.get("rawPath", event.get("path", "/"))
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")

    if path == "/.well-known/oauth-authorization-server":
        return handle_oauth_metadata(event)
    elif path == "/.well-known/oauth-protected-resource":
        return handle_protected_resource_metadata(event)
    elif path == "/authorize":
        return handle_authorize(event)
    elif path == "/callback":
        return handle_callback(event)
    elif path == "/token" and method == "POST":
        return handle_token(event)
    elif path == "/register" and method == "POST":
        return handle_dcr(event)
    else:
        return proxy_to_gateway(event)


# ---------------------------------------------------------------------------
# OAuth metadata endpoints (RFC 8414 / RFC 9728)
# ---------------------------------------------------------------------------


def handle_oauth_metadata(event: dict) -> dict:
    """Serve RFC 8414 authorization-server metadata.

    :param event: the proxy event (for the public API URL).
    :returns: a JSON proxy response with the discovery document.
    """
    api_url = get_api_url(event)
    return json_response(
        200,
        {
            "issuer": api_url,
            "authorization_endpoint": f"{api_url}/authorize",
            "token_endpoint": f"{api_url}/token",
            "registration_endpoint": f"{api_url}/register",
            "scopes_supported": ["openid", "profile", "email", GATEWAY_SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
            "code_challenge_methods_supported": ["S256"],
        },
    )


def handle_protected_resource_metadata(event: dict) -> dict:
    """Serve RFC 9728 protected-resource metadata.

    :param event: the proxy event (for the public API URL).
    :returns: a JSON proxy response with the protected-resource document.
    """
    api_url = get_api_url(event)
    return json_response(
        200,
        {
            "resource": api_url,
            "authorization_servers": [api_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["openid", "profile", "email", GATEWAY_SCOPE],
        },
    )


# ---------------------------------------------------------------------------
# Authorization Code flow helpers
# ---------------------------------------------------------------------------


def handle_authorize(event: dict) -> dict:
    """Redirect /authorize to Cognito, injecting the gateway scope.

    :param event: the proxy event (carries the client's query params).
    :returns: a 302 redirect to the Cognito hosted-UI authorize endpoint.
    """
    params = dict(event.get("queryStringParameters") or {})
    params["client_id"] = CLIENT_ID

    # Inject the gateway scope so the returned access_token is accepted by the
    # gateway's CUSTOM_JWT authorizer without any token swap.
    requested = set(params.get("scope", "openid").split())
    requested.add(GATEWAY_SCOPE)
    params["scope"] = " ".join(sorted(requested))

    original_redirect_uri = params.get("redirect_uri", "")
    original_state = params.get("state", "")

    if original_redirect_uri:
        compound_state = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "state": original_state,
                    "redirect_uri": urllib.parse.unquote(original_redirect_uri),
                }
            ).encode()
        ).decode()
        params["state"] = compound_state
        api_url = get_api_url(event)
        params["redirect_uri"] = f"{api_url}/callback"

    redirect_url = (
        f"{COGNITO_DOMAIN.rstrip('/')}/oauth2/authorize"
        f"?{urllib.parse.urlencode(params)}"
    )
    return {"statusCode": 302, "headers": {"Location": redirect_url}, "body": ""}


def handle_callback(event: dict) -> dict:
    """Receive Cognito callback, decode compound state, forward to the client.

    :param event: the proxy event (carries code + compound state).
    :returns: a 302 redirect back to the client's original redirect_uri.
    """
    params = event.get("queryStringParameters") or {}
    code = params.get("code", "")
    encoded_state = params.get("state", "")
    error = params.get("error", "")

    if error:
        return json_response(400, {"error": error})

    try:
        clean = encoded_state.replace(" ", "+")
        padding = 4 - len(clean) % 4
        if padding != 4:
            clean += "=" * padding
        compound_state = json.loads(base64.urlsafe_b64decode(clean).decode())
        original_state = compound_state.get("state", "")
        original_redirect_uri = compound_state.get("redirect_uri", "")
    except Exception as exc:  # noqa: BLE001 — malformed state → 400, not a 500
        print(f"State decode error: {exc}")
        return json_response(400, {"error": "Invalid state parameter"})

    if not original_redirect_uri:
        return json_response(400, {"error": "Missing redirect_uri in state"})

    forward_url = (
        f"{original_redirect_uri}"
        f"?{urllib.parse.urlencode({'code': code, 'state': original_state})}"
    )
    return {"statusCode": 302, "headers": {"Location": forward_url}, "body": ""}


def handle_token(event: dict) -> dict:
    """Proxy the token request to Cognito, rewriting redirect_uri.

    :param event: the proxy event (carries the form-encoded token request).
    :returns: a JSON proxy response with the Cognito token payload.
    """
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()

    params = dict(urllib.parse.parse_qsl(body))
    params["client_id"] = CLIENT_ID
    if CLIENT_SECRET:
        params["client_secret"] = CLIENT_SECRET
    if "redirect_uri" in params:
        params["redirect_uri"] = f"{get_api_url(event)}/callback"

    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        _require_https(f"{COGNITO_DOMAIN.rstrip('/')}/oauth2/token"), data=data, method="POST"
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    # Browser-like UA so WAF Bot Control on the Cognito pool lets this
    # back-channel token exchange through (see COGNITO_USER_AGENT above).
    req.add_header("User-Agent", COGNITO_USER_AGENT)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 — scheme enforced by _require_https above  # nosemgrep: dynamic-urllib-use-detected — fixed AWS service endpoint from config, not user-controlled host
            token_data = json.loads(resp.read().decode())
            if "created_at" not in token_data:
                token_data["created_at"] = int(time.time() * 1000)
            return json_response(200, token_data)
    except urllib.error.HTTPError as exc:
        # Log the upstream status + body so a future failure (e.g. a new WAF
        # rule) is diagnosable from CloudWatch instead of silently 403-ing.
        err_body = exc.read().decode()
        print(f"Cognito token exchange failed: HTTP {exc.code} body={err_body!r}")
        return json_response(exc.code, {"error": err_body})


def handle_dcr(event: dict) -> dict:
    """Dynamic Client Registration (RFC 7591) — return the pre-registered CLIENT_ID.

    MCP clients (Claude Code, VS Code) validate the response with Zod and require
    ``redirect_uris`` to be present as an array. Echo back whatever the client
    sent, or fall back to an empty list so the field is always an array.

    :param event: the proxy event (carries the registration request body).
    :returns: a JSON proxy response echoing the pre-registered client.
    """
    body = event.get("body", "") or ""
    if event.get("isBase64Encoded") and body:
        body = base64.b64decode(body).decode()
    try:
        req_data = json.loads(body) if body else {}
    except Exception:  # noqa: BLE001 — malformed body → empty registration echo
        req_data = {}

    redirect_uris = req_data.get("redirect_uris", [])
    if not isinstance(redirect_uris, list):
        redirect_uris = [redirect_uris] if redirect_uris else []

    return json_response(
        200,
        {
            "client_id": CLIENT_ID,
            "client_name": req_data.get("client_name", "MCP Client"),
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )


# ---------------------------------------------------------------------------
# MCP proxy
# ---------------------------------------------------------------------------


def proxy_to_gateway(event: dict) -> dict:
    """Forward MCP requests to the gateway with the caller's Bearer token.

    The token was issued by Cognito with the ``semantic-layer-mcp/invoke`` scope
    (injected at /authorize time), so the gateway's CUSTOM_JWT authorizer accepts
    it directly — no token swap required.

    :param event: the proxy event (carries the MCP request).
    :returns: an API Gateway proxy response mirroring the gateway response.
    """
    path = event.get("rawPath", event.get("path", "/"))
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    headers = event.get("headers") or {}
    body = event.get("body", "")

    if event.get("isBase64Encoded") and body:
        body = base64.b64decode(body)

    gateway_url = _get_gateway_url()
    target_url = _require_https(
        f"{gateway_url.rstrip('/')}{path}" if path != "/" else gateway_url
    )

    req_headers = {
        "Content-Type": headers.get("content-type", "application/json"),
        "Accept": headers.get("accept", "application/json"),
    }
    if auth := headers.get("authorization"):
        req_headers["Authorization"] = auth
    for h in ("mcp-protocol-version", "mcp-session-id"):
        if headers.get(h):
            req_headers[h.title()] = headers[h]

    try:
        if method == "POST" and body:
            data = body.encode() if isinstance(body, str) else body
            req = urllib.request.Request(target_url, data=data, method="POST")
        else:
            req = urllib.request.Request(target_url, method=method)
        for k, v in req_headers.items():
            req.add_header(k, v)

        with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310 — scheme enforced by _require_https on target_url  # nosemgrep: dynamic-urllib-use-detected — fixed AWS service endpoint from config, not user-controlled host
            resp_body = resp.read().decode()
            resp_headers = {
                "Content-Type": resp.headers.get("Content-Type", "application/json")
            }
            if sid := resp.headers.get("Mcp-Session-Id"):
                resp_headers["Mcp-Session-Id"] = sid
            return {"statusCode": resp.status, "headers": resp_headers, "body": resp_body}

    except urllib.error.HTTPError as exc:
        resp_headers = {"Content-Type": "application/json"}
        if exc.headers.get("WWW-Authenticate"):
            # Rewrite resource_metadata to point at this proxy, not the upstream
            # AgentCore URL. Without this, Claude Code follows the upstream URL,
            # gets the AgentCore resource identifier, and aborts with a "protected
            # resource does not match expected" error.
            api_url = get_api_url(event)
            resp_headers["WWW-Authenticate"] = (
                f'Bearer resource_metadata="{api_url}/.well-known/oauth-protected-resource"'
            )
        return {"statusCode": exc.code, "headers": resp_headers, "body": exc.read().decode()}
    except Exception as exc:  # noqa: BLE001 — surface upstream errors as JSON-RPC
        return json_response(502, {"error": {"code": -32603, "message": str(exc)}})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_api_url(event: dict) -> str:
    """Build this proxy's public base URL from the request context.

    :param event: the proxy event.
    :returns: the https base URL (with stage when not $default), else localhost.
    """
    ctx = event.get("requestContext") or {}
    domain = ctx.get("domainName", "")
    stage = ctx.get("stage", "")
    if domain and stage and stage != "$default":
        return f"https://{domain}/{stage}"
    return f"https://{domain}" if domain else "http://localhost"


def json_response(status_code: int, body: dict) -> dict:
    """Build a JSON API Gateway proxy response.

    :param status_code: HTTP status code.
    :param body: JSON-serializable response body.
    :returns: an API Gateway proxy response dict.
    """
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
