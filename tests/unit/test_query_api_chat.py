"""Tests for the AG-UI chat endpoints in query_api.py."""

from __future__ import annotations

import os
import sys

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

# Make the rest-api package importable.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)


_TABLE_NAME = 'semantic-layer-chat-sessions'


def _build_table() -> None:
    client = boto3.client('dynamodb', region_name='us-east-1')
    client.create_table(
        TableName=_TABLE_NAME,
        AttributeDefinitions=[
            {'AttributeName': 'sessionId', 'AttributeType': 'S'},
        ],
        KeySchema=[{'AttributeName': 'sessionId', 'KeyType': 'HASH'}],
        BillingMode='PAY_PER_REQUEST',
    )


@pytest.fixture
def app_with_mocks(monkeypatch):
    """Patch services and yield a fresh TestClient for query_api."""
    # Required env vars (set before import).
    monkeypatch.setenv('CHAT_SESSIONS_TABLE', _TABLE_NAME)
    monkeypatch.setenv('AWS_REGION', 'us-east-1')
    monkeypatch.setenv('QUERY_RUNTIME_ARN', 'arn:aws:bedrock-agentcore:us-east-1:0:runtime/q')
    monkeypatch.setenv(
        'METADATA_QUERY_RUNTIME_ARN',
        'arn:aws:bedrock-agentcore:us-east-1:0:runtime/m',
    )

    with mock_aws():
        _build_table()

        # Reload query_api with patches applied.
        if 'query_api' in sys.modules:
            del sys.modules['query_api']
        import query_api  # noqa: WPS433  — reimport with patched env

        # Replace the service with one bound to the moto-mocked table.
        from services.chat_session_service import ChatSessionService
        query_api.chat_sessions = ChatSessionService(
            table_name=_TABLE_NAME, region='us-east-1'
        )

        yield query_api


def test_get_session_returns_404_when_missing(app_with_mocks):
    client = TestClient(app_with_mocks.app)
    resp = client.get('/sessions/nope')
    assert resp.status_code == 404


def test_get_session_returns_owned_session(app_with_mocks):
    """GET /sessions/{id} returns the transcript to its owner.

    The TestClient sends no JWT, so get_principal resolves the principal to
    'anonymous'; the session is created under that same owner.
    """
    qa = app_with_mocks
    qa.chat_sessions.create_session(
        session_id='sess-own', ontology_id='ont-test',
        mode='ontology_query', user_id='anonymous',
    )
    client = TestClient(qa.app)
    resp = client.get('/sessions/sess-own')
    assert resp.status_code == 200
    assert resp.json()['sessionId'] == 'sess-own'


def test_get_session_foreign_owner_returns_403(app_with_mocks):
    """A caller must not read a session owned by a different user."""
    qa = app_with_mocks
    qa.chat_sessions.create_session(
        session_id='sess-victim', ontology_id='ont-test',
        mode='ontology_query', user_id='victim',
    )
    client = TestClient(qa.app)  # principal resolves to 'anonymous' != 'victim'
    resp = client.get('/sessions/sess-victim')
    assert resp.status_code == 403


def test_delete_session_archives_existing(app_with_mocks):
    """DELETE /sessions/{id} archives a session owned by the caller."""
    qa = app_with_mocks
    qa.chat_sessions.create_session(
        session_id='sess-del',
        ontology_id='ont-test',
        mode='ontology_query',
        user_id='anonymous',  # matches the no-JWT test principal
    )
    client = TestClient(qa.app)
    resp = client.delete('/sessions/sess-del')
    assert resp.status_code == 200
    body = resp.json()
    assert body['status'] == 'archived'
    assert body['sessionId'] == 'sess-del'


def test_delete_session_foreign_owner_returns_403(app_with_mocks):
    """A caller must not archive a session owned by a different user."""
    qa = app_with_mocks
    qa.chat_sessions.create_session(
        session_id='sess-victim-del', ontology_id='ont-test',
        mode='ontology_query', user_id='victim',
    )
    client = TestClient(qa.app)
    resp = client.delete('/sessions/sess-victim-del')
    assert resp.status_code == 403
    # The session must remain un-archived.
    assert qa.chat_sessions.get_session(
        session_id='sess-victim-del').get('archived') is not True


def test_delete_session_returns_404_when_missing(app_with_mocks):
    """Missing sessions surface as 404 — symmetric with GET /sessions/{id}."""
    client = TestClient(app_with_mocks.app)
    resp = client.delete('/sessions/nope')
    assert resp.status_code == 404
