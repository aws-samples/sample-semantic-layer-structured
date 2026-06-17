"""Unit tests for the MCP OAuth proxy Lambda handler.

Covers the contract Claude Code / VSCode depend on: OAuth metadata shape, scope
injection at /authorize, DCR echo, and the WWW-Authenticate resource_metadata
rewrite on a gateway 401. Pure stdlib handler — no AWS calls except SSM (mocked).
"""
import json
import os
import sys
import urllib.error
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'mcp-proxy'))

SCOPE = 'semantic-layer-mcp/invoke'


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Set the proxy env the handler reads at import/runtime."""
    monkeypatch.setenv('COGNITO_DOMAIN', 'https://ex.auth.us-east-1.amazoncognito.com')
    monkeypatch.setenv('CLIENT_ID', 'pkce-client')
    monkeypatch.setenv('CLIENT_SECRET', '')
    monkeypatch.setenv('GATEWAY_SCOPE', SCOPE)
    monkeypatch.setenv('GATEWAY_URL_SSM_PARAM', '/semantic-layer-dev/mcp/gateway-url')


def _import():
    """Import the handler fresh so env + module globals are picked up.

    :returns: the lambda_function module.
    """
    import importlib

    import lambda_function

    return importlib.reload(lambda_function)


def _evt(path, method='GET', query=None, headers=None, body=None):
    """Build a minimal HTTP API v2 proxy event."""
    return {
        'rawPath': path,
        'requestContext': {'http': {'method': method}, 'domainName': 'proxy.example.com', 'stage': '$default'},
        'queryStringParameters': query or {},
        'headers': headers or {},
        'body': body,
    }


def test_oauth_metadata_includes_gateway_scope():
    """/.well-known/oauth-authorization-server advertises the gateway scope."""
    mod = _import()
    resp = mod.lambda_handler(_evt('/.well-known/oauth-authorization-server'), None)
    body = json.loads(resp['body'])
    assert SCOPE in body['scopes_supported']
    assert body['authorization_endpoint'].endswith('/authorize')
    assert 'S256' in body['code_challenge_methods_supported']


def test_authorize_injects_gateway_scope_into_redirect():
    """/authorize injects the gateway scope and redirects to Cognito."""
    mod = _import()
    resp = mod.lambda_handler(
        _evt('/authorize', query={'scope': 'openid', 'redirect_uri': 'http://localhost:33418', 'state': 'xyz'}),
        None,
    )
    assert resp['statusCode'] == 302
    location = resp['headers']['Location']
    assert 'oauth2/authorize' in location
    # The gateway scope must be present in the forwarded scope param.
    assert SCOPE.replace('/', '%2F') in location or SCOPE in urllib_unquote(location)


def urllib_unquote(s: str) -> str:
    """Local helper: percent-decode for scope assertions."""
    import urllib.parse

    return urllib.parse.unquote(s)


def test_dcr_echoes_client_id_and_array_redirect_uris():
    """/register returns the pre-registered client id with redirect_uris as a list."""
    mod = _import()
    body = json.dumps({'client_name': 'Claude Code', 'redirect_uris': ['http://localhost:33418']})
    resp = mod.lambda_handler(_evt('/register', method='POST', body=body), None)
    data = json.loads(resp['body'])
    assert data['client_id'] == 'pkce-client'
    assert isinstance(data['redirect_uris'], list)
    assert data['token_endpoint_auth_method'] == 'none'


def test_token_exchange_sends_browser_user_agent_to_cognito():
    """POST /token must send a browser-like User-Agent to Cognito's token endpoint.

    The Cognito user pool sits behind WAF Bot Control, which 403-blocks requests
    whose UA looks like an HTTP library (urllib's default "Python-urllib/3.x").
    Without a browser UA the back-channel token exchange is silently blocked and
    the whole OAuth login loops forever. Assert the outgoing request carries a
    non-library UA so that regression can't return unnoticed.
    """
    mod = _import()

    captured = {}

    class _Resp:
        """Minimal context-manager stand-in for urlopen's response."""

        status = 200

        def read(self):
            return b'{"access_token": "tok", "token_type": "Bearer"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=None):
        # Capture the User-Agent header the handler set on the outgoing request.
        captured['user_agent'] = req.get_header('User-agent')
        return _Resp()

    body = 'grant_type=authorization_code&code=abc&code_verifier=v&redirect_uri=http://localhost:33418'
    with patch('lambda_function.urllib.request.urlopen', side_effect=_fake_urlopen):
        resp = mod.lambda_handler(_evt('/token', method='POST', body=body), None)

    assert resp['statusCode'] == 200
    ua = captured['user_agent']
    assert ua, 'token exchange must set a User-Agent header'
    # Must not look like an HTTP library (the thing WAF Bot Control blocks).
    assert 'python-urllib' not in ua.lower()
    assert 'Mozilla' in ua


def test_proxy_rewrites_www_authenticate_on_401():
    """A 401 from the gateway has its WWW-Authenticate resource_metadata rewritten
    to point at THIS proxy (else Claude Code aborts on a mismatch)."""
    mod = _import()

    err = urllib.error.HTTPError(
        url='https://gw',
        code=401,
        msg='Unauthorized',
        hdrs={'WWW-Authenticate': 'Bearer resource_metadata="https://upstream/.well-known/oauth-protected-resource"'},
        fp=None,
    )
    err.read = lambda: b'{"error":"unauthorized"}'  # type: ignore[assignment]

    with patch.object(mod, '_get_gateway_url', return_value='https://gw.example.com'), patch(
        'lambda_function.urllib.request.urlopen', side_effect=err
    ):
        resp = mod.lambda_handler(_evt('/mcp', method='POST', body='{}'), None)

    assert resp['statusCode'] == 401
    wa = resp['headers']['WWW-Authenticate']
    # Rewritten to the proxy's own protected-resource metadata URL.
    assert 'proxy.example.com' in wa
    assert wa.endswith('/.well-known/oauth-protected-resource"')
