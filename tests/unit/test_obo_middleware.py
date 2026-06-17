"""Tests for the OBO middleware FastAPI dependencies."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)


@pytest.fixture(autouse=True)
def reset_module(monkeypatch):
    # Force a fresh module each test so the singleton accessor pulls our
    # patched IdentityService.
    if 'services.obo_middleware' in sys.modules:
        del sys.modules['services.obo_middleware']
    yield


def _request_with_event(event: dict) -> Request:
    scope = {
        'type': 'http',
        'method': 'GET',
        'path': '/',
        'headers': [],
        'query_string': b'',
        'aws.event': event,
    }
    return Request(scope)


def test_extract_principal_pulls_sub_and_jwt():
    from services.obo_middleware import _extract_principal
    event = {
        'requestContext': {
            'authorizer': {'jwt': {'claims': {'sub': 'u-1', 'email': 'a@b'}}}
        },
        'headers': {'Authorization': 'Bearer XYZ'},
    }
    principal = _extract_principal(event)
    assert principal == {'userId': 'u-1', 'email': 'a@b', 'jwt': 'XYZ'}


def test_extract_principal_handles_missing_authorizer():
    from services.obo_middleware import _extract_principal
    principal = _extract_principal({})
    assert principal == {'userId': '', 'email': '', 'jwt': ''}


def test_get_principal_returns_empty_for_local_dev():
    from services.obo_middleware import get_principal
    req = _request_with_event({})  # no Lambda event
    assert get_principal(req) == {'userId': '', 'email': '', 'jwt': ''}


def test_require_obo_returns_none_when_flag_off(monkeypatch):
    from services.obo_middleware import require_obo
    import services.obo_middleware as mw
    fake = MagicMock(enabled=False)
    monkeypatch.setattr(mw, 'get_identity_service', lambda: fake)
    req = _request_with_event({})
    out = require_obo(req)
    assert out is None
    fake.exchange.assert_not_called()


def test_require_obo_raises_401_when_flag_on_but_no_jwt(monkeypatch):
    from services.obo_middleware import require_obo
    import services.obo_middleware as mw
    fake = MagicMock(enabled=True)
    monkeypatch.setattr(mw, 'get_identity_service', lambda: fake)
    req = _request_with_event({})  # no JWT
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        require_obo(req)
    assert ei.value.status_code == 401


def test_require_obo_raises_401_on_exchange_failure(monkeypatch):
    from services.identity_service import ObOExchangeError
    import services.obo_middleware as mw
    fake = MagicMock(enabled=True)
    fake.exchange.side_effect = ObOExchangeError('boom')
    monkeypatch.setattr(mw, 'get_identity_service', lambda: fake)
    req = _request_with_event({
        'requestContext': {
            'authorizer': {'jwt': {'claims': {'sub': 'u'}}},
        },
        'headers': {'Authorization': 'Bearer T'},
    })
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        mw.require_obo(req)
    assert ei.value.status_code == 401


def test_require_obo_returns_token_when_exchange_succeeds(monkeypatch):
    from services.identity_service import ObOToken
    import services.obo_middleware as mw
    fake = MagicMock(enabled=True)
    token = ObOToken(
        user_id='u', token_id='t', access_key_id='a',
        secret_access_key='s', session_token='st',
        expires_at_epoch=10**12,
    )
    fake.exchange.return_value = token
    monkeypatch.setattr(mw, 'get_identity_service', lambda: fake)
    req = _request_with_event({
        'requestContext': {
            'authorizer': {'jwt': {'claims': {'sub': 'u'}}},
        },
        'headers': {'Authorization': 'Bearer T'},
    })
    out = mw.require_obo(req, ontology_id='ont-1')
    assert out is token
    fake.exchange.assert_called_once()


def test_dependency_in_fastapi_route(monkeypatch):
    """End-to-end: hook ``require_obo`` into a real FastAPI app and assert
    the dependency fires."""
    import services.obo_middleware as mw

    fake = MagicMock(enabled=False)
    monkeypatch.setattr(mw, 'get_identity_service', lambda: fake)

    app = FastAPI()

    @app.get('/echo')
    def echo(request: Request):  # nosemgrep: useless-inner-function — registered as FastAPI route handler by decorator
        return {'principal': mw.get_principal(request)}

    client = TestClient(app)
    response = client.get('/echo')
    assert response.status_code == 200
    assert response.json()['principal']['userId'] == ''
