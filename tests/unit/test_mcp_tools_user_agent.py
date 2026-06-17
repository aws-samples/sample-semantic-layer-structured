"""Unit tests for the mcp-tools Lambda OAuth User-Agent header.

The Cognito hosted-UI /oauth2/token endpoint is fronted by WAF Bot Control,
which 403s the default Python-urllib User-Agent. These tests assert the token
mint (and, for consistency, the runtime invoke) carry a browser User-Agent.
"""
import importlib.util
import os

from unittest.mock import patch

# Load lambda/mcp-tools/index.py by file path under a UNIQUE module name.
# Several Lambda handlers under test are all named `index.py`; a plain
# `import index` caches the first one in sys.modules['index'] and hands later
# test files the WRONG module under the full-suite run. A distinct name avoids
# that cross-test collision.
_MCP_TOOLS_INDEX_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), '..', '..', 'lambda', 'mcp-tools', 'index.py'
    )
)
_spec = importlib.util.spec_from_file_location(
    'mcp_tools_index', _MCP_TOOLS_INDEX_PATH
)
mcp_index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp_index)


class _Resp:
    """Minimal urlopen context-manager stand-in returning a fixed body."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_fetch_m2m_token_sends_browser_user_agent():
    """_fetch_m2m_token sends a browser User-Agent so Cognito WAF Bot Control
    doesn't 403 the token mint."""
    mcp_index._m2m_token_cache.clear()
    captured = {}

    def _fake_urlopen(req, timeout=0):
        # urllib stores header keys capitalized-first-letter only.
        captured['ua'] = req.get_header('User-agent')
        return _Resp(b'{"access_token":"t","expires_in":3600}')

    env = {
        'OAUTH_TOKEN_ENDPOINT': 'https://x.auth.us-east-1.amazoncognito.com/oauth2/token',
        'M2M_CLIENT_ID': 'c',
        'OAUTH_SCOPE': 's',
    }
    with patch.dict(os.environ, env), patch.object(
        mcp_index, '_m2m_client_secret', return_value='sec'
    ), patch.object(mcp_index.urllib.request, 'urlopen', _fake_urlopen):
        mcp_index._fetch_m2m_token(force=True)
    assert captured['ua'] and 'Mozilla' in captured['ua']


def test_invoke_runtime_sync_sends_browser_user_agent():
    """_invoke_runtime_sync sends a browser User-Agent on the /invocations POST
    (consistency with the token-mint request)."""
    captured = {}

    def _fake_urlopen(req, timeout=0):
        captured['ua'] = req.get_header('User-agent')
        return _Resp(b'{"result": "ok"}')

    with patch.dict(os.environ, {'AWS_REGION': 'us-east-1'}), patch.object(
        mcp_index, '_fetch_m2m_token', return_value='tok'
    ), patch.object(mcp_index.urllib.request, 'urlopen', _fake_urlopen):
        mcp_index._invoke_runtime_sync(
            runtime_arn='arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/fake',
            payload={'id': 'x'},
        )
    assert captured['ua'] and 'Mozilla' in captured['ua']
