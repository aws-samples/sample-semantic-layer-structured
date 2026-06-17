"""Unit tests for AgentCoreService (OAuth M2M invocation path)."""
import sys, os
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))


def _make_service():
    """Construct an AgentCoreService with boto3 (secretsmanager) mocked.

    :returns: an AgentCoreService instance whose _invoke_runtime is patchable.
    """
    with patch('services.agentcore_service.boto3') as mock_boto:
        mock_boto.client.return_value = MagicMock()
        from services.agentcore_service import AgentCoreService
        return AgentCoreService()


def test_invoke_metadata_agent_payload_is_id_only():
    """invoke_metadata_agent sends only {"id": job_id} over the OAuth HTTPS path."""
    svc = _make_service()
    svc.metadata_runtime_arn = 'arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/fake'

    job_id = 'a' * 36
    with patch.object(svc, '_invoke_runtime', return_value={'status': 'processing'}) as inv:
        svc.invoke_metadata_agent(id=job_id)

    # _invoke_runtime is called with the runtime ARN, a session id, and the
    # id-only payload — no tables/annotations leak into the request.
    kwargs = inv.call_args.kwargs
    assert kwargs['runtime_arn'] == svc.metadata_runtime_arn
    assert kwargs['payload'] == {'id': job_id}
    assert 'tables' not in kwargs['payload']
    assert 'annotations' not in kwargs['payload']


def test_invoke_metadata_agent_raises_without_arn():
    """ValueError raised when METADATA_RUNTIME_ARN is not set."""
    svc = _make_service()
    svc.metadata_runtime_arn = None  # not configured

    with pytest.raises(ValueError, match='METADATA_RUNTIME_ARN'):
        svc.invoke_metadata_agent(id='a' * 36)


def test_fetch_token_uses_client_credentials_and_caches():
    """_fetch_token mints a client_credentials token and caches it until expiry."""
    svc = _make_service()
    svc.oauth_token_endpoint = 'https://example.auth.us-east-1.amazoncognito.com/oauth2/token'
    svc.m2m_client_id = 'm2m-client'
    svc.oauth_scope = 'semantic-layer-mcp/invoke'

    import json as _json
    import io

    fake_resp = io.BytesIO(_json.dumps({'access_token': 'tok-1', 'expires_in': 3600}).encode())
    fake_resp.__enter__ = lambda s: s  # type: ignore[attr-defined]
    fake_resp.__exit__ = lambda *a: False  # type: ignore[attr-defined]

    with patch.object(svc, '_m2m_client_secret', return_value='secret'), patch(
        'services.agentcore_service.urllib.request.urlopen', return_value=fake_resp
    ) as urlopen:
        token = svc._fetch_token()
        assert token == 'tok-1'
        # Second call within expiry is served from cache (no new urlopen).
        token2 = svc._fetch_token()
        assert token2 == 'tok-1'
        assert urlopen.call_count == 1


def test_fetch_token_sends_browser_user_agent():
    """_fetch_token sends a browser User-Agent so Cognito WAF Bot Control
    doesn't 403 the /oauth2/token request (default Python-urllib UA is a known
    bot signature)."""
    svc = _make_service()
    svc.oauth_token_endpoint = 'https://x.auth.us-east-1.amazoncognito.com/oauth2/token'
    svc.m2m_client_id = 'c'
    svc.oauth_scope = 's'
    captured = {}

    class _Resp:
        def __enter__(s):
            return s

        def __exit__(s, *a):
            return False

        def read(s):
            return b'{"access_token":"t","expires_in":3600}'

    def _fake_urlopen(req, timeout=0):
        # urllib normalizes header name capitalization to first-letter-only.
        captured['ua'] = req.get_header('User-agent')
        return _Resp()

    with patch.object(svc, '_m2m_client_secret', return_value='sec'), patch(
        'services.agentcore_service.urllib.request.urlopen', _fake_urlopen
    ):
        svc._fetch_token()
    assert captured['ua'] and 'Mozilla' in captured['ua']


def test_invoke_runtime_sends_browser_user_agent():
    """_invoke_runtime sends a browser User-Agent on the /invocations POST
    (consistency with the token-mint request)."""
    svc = _make_service()
    captured = {}

    class _Resp:
        def __enter__(s):
            return s

        def __exit__(s, *a):
            return False

        def read(s):
            return b'{"result": "ok"}'

    def _fake_urlopen(req, timeout=0):
        captured['ua'] = req.get_header('User-agent')
        return _Resp()

    with patch.object(svc, '_fetch_token', return_value='tok'), patch(
        'services.agentcore_service.urllib.request.urlopen', _fake_urlopen
    ):
        svc._invoke_runtime(
            runtime_arn='arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/fake',
            session_id='s' * 33,
            payload={'id': 'x'},
        )
    assert captured['ua'] and 'Mozilla' in captured['ua']
