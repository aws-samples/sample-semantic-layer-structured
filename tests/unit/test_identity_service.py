"""Unit tests for the OBO IdentityService."""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

from services.identity_service import (  # noqa: E402
    IdentityService,
    ObOExchangeError,
)


def _ok_response(*, expires_in_seconds=900):
    return {
        'tokenId': 'tok-1',
        'expiresAtEpoch': int(time.time()) + expires_in_seconds,
        'credentials': {
            'accessKeyId': 'AKIA',
            'secretAccessKey': 'sec',
            'sessionToken': 'tok',
        },
    }


def test_disabled_short_circuits():
    svc = IdentityService(enabled=False)
    with pytest.raises(ObOExchangeError):
        svc.exchange(
            jwt='j', user_id='u', ontology_id='o', target_audience='a'
        )


def test_exchange_success_returns_token():
    client = MagicMock()
    client.exchange_token.return_value = _ok_response()
    svc = IdentityService(
        enabled=True, agentcore_identity_client=client
    )
    token = svc.exchange(
        jwt='j', user_id='u', ontology_id='o', target_audience='a'
    )
    assert token.access_key_id == 'AKIA'
    assert token.user_id == 'u'
    assert token.token_id == 'tok-1'
    payload = token.as_runtime_payload()
    assert payload['awsSessionToken'] == 'tok'


def test_exchange_caches_per_user_ontology():
    client = MagicMock()
    client.exchange_token.return_value = _ok_response()
    svc = IdentityService(
        enabled=True, agentcore_identity_client=client
    )
    svc.exchange(
        jwt='j', user_id='u', ontology_id='o', target_audience='a'
    )
    svc.exchange(
        jwt='j', user_id='u', ontology_id='o', target_audience='a'
    )
    # Same key — only one upstream call.
    assert client.exchange_token.call_count == 1


def test_exchange_refreshes_when_token_expired():
    client = MagicMock()
    # First call returns a near-expired token.
    client.exchange_token.side_effect = [
        _ok_response(expires_in_seconds=10),
        _ok_response(expires_in_seconds=900),
    ]
    svc = IdentityService(
        enabled=True, agentcore_identity_client=client
    )
    svc.exchange(
        jwt='j', user_id='u', ontology_id='o', target_audience='a'
    )
    # Second call should re-exchange because the cached token is within the
    # leeway window of expiry.
    svc.exchange(
        jwt='j', user_id='u', ontology_id='o', target_audience='a'
    )
    assert client.exchange_token.call_count == 2


def test_exchange_fails_closed_on_client_error():
    client = MagicMock()
    client.exchange_token.side_effect = ClientError(
        {'Error': {'Code': 'AccessDenied', 'Message': 'no'}},
        'ExchangeToken',
    )
    svc = IdentityService(
        enabled=True, agentcore_identity_client=client
    )
    with pytest.raises(ObOExchangeError):
        svc.exchange(
            jwt='j', user_id='u', ontology_id='o', target_audience='a'
        )


def test_exchange_rejects_response_missing_credentials():
    client = MagicMock()
    client.exchange_token.return_value = {
        'tokenId': 't',
        'credentials': {'accessKeyId': '', 'secretAccessKey': '', 'sessionToken': ''},
    }
    svc = IdentityService(
        enabled=True, agentcore_identity_client=client
    )
    with pytest.raises(ObOExchangeError):
        svc.exchange(
            jwt='j', user_id='u', ontology_id='o', target_audience='a'
        )


def test_invalidate_drops_cache_entry():
    client = MagicMock()
    client.exchange_token.return_value = _ok_response()
    svc = IdentityService(
        enabled=True, agentcore_identity_client=client
    )
    svc.exchange(
        jwt='j', user_id='u', ontology_id='o', target_audience='a'
    )
    svc.invalidate(user_id='u', ontology_id='o')
    svc.exchange(
        jwt='j', user_id='u', ontology_id='o', target_audience='a'
    )
    assert client.exchange_token.call_count == 2
