"""Unit tests for the chat-first ``GET /query/sessions`` list endpoint and the
soft-delete behaviour of ``DELETE /query/sessions/{id}``.

These tests boot the FastAPI ``query_api.app`` with moto-backed DDB so the
ChatSessionService inside it operates against a real (mocked) table+GSI.
``OntologyService.get_metadata_config`` is patched to return a stub name so
we don't need the metadata table provisioned for these scenarios.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, Optional
from unittest.mock import patch

import boto3
import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402


_TABLE_NAME = 'semantic-layer-chat-sessions'
_GSI_NAME = 'userId-updatedAt-index'


def _build_table_with_gsi() -> None:
    """Mirror the production CDK schema (PK + userId-updatedAt-index)."""
    client = boto3.client('dynamodb', region_name='us-east-1')
    client.create_table(
        TableName=_TABLE_NAME,
        AttributeDefinitions=[
            {'AttributeName': 'sessionId', 'AttributeType': 'S'},
            {'AttributeName': 'userId', 'AttributeType': 'S'},
            {'AttributeName': 'updatedAt', 'AttributeType': 'S'},
        ],
        KeySchema=[{'AttributeName': 'sessionId', 'KeyType': 'HASH'}],
        BillingMode='PAY_PER_REQUEST',
        GlobalSecondaryIndexes=[
            {
                'IndexName': _GSI_NAME,
                'KeySchema': [
                    {'AttributeName': 'userId', 'KeyType': 'HASH'},
                    {'AttributeName': 'updatedAt', 'KeyType': 'RANGE'},
                ],
                'Projection': {'ProjectionType': 'KEYS_ONLY'},
            }
        ],
    )
    client.update_time_to_live(
        TableName=_TABLE_NAME,
        TimeToLiveSpecification={'AttributeName': 'ttl', 'Enabled': True},
    )


def _stub_principal(_request: Any) -> Dict[str, str]:
    """Always identify the caller as ``alice`` for tests."""
    return {'userId': 'alice', 'email': 'alice@example.com', 'jwt': ''}


def _stub_ontology_config(*, id: str) -> Optional[Dict[str, Any]]:
    """Return a deterministic display name without touching the metadata table."""
    return {'id': id, 'name': f'Ontology {id}'}


@pytest.fixture
def client() -> TestClient:
    """Yield a TestClient bound to query_api.app + a fresh moto DDB table.

    The fixture rebinds the module-level ``chat_sessions`` and patches
    ``get_principal`` / ``OntologyService.get_metadata_config`` so the
    endpoints exercise the real code path without external dependencies.
    """
    os.environ['CHAT_SESSIONS_TABLE'] = _TABLE_NAME
    os.environ['AWS_REGION'] = 'us-east-1'

    with mock_aws():
        _build_table_with_gsi()

        # Import after env vars are set so the module-level singletons bind
        # to the moto-mocked table.
        if 'query_api' in sys.modules:
            del sys.modules['query_api']
        import query_api  # noqa: WPS433 — late import on purpose

        from services.chat_session_service import ChatSessionService

        # Rebind to a service that knows the test table name explicitly —
        # the module-level instance was constructed before the env var swap
        # could be guaranteed in some import orderings.
        query_api.chat_sessions = ChatSessionService(
            table_name=_TABLE_NAME, region='us-east-1'
        )

        with patch.object(query_api, 'get_principal', _stub_principal), patch.object(
            query_api.ontology_service,
            'get_metadata_config',
            side_effect=lambda id: _stub_ontology_config(id=id),
        ):
            yield TestClient(query_api.app)


def _seed_session(
    client: TestClient,
    *,
    session_id: str,
    ontology_id: str,
    user_message: str = 'hello',
) -> None:
    """Use the service directly via the TestClient's app to seed a session."""
    import query_api  # noqa: WPS433

    query_api.chat_sessions.create_session(
        session_id=session_id,
        ontology_id=ontology_id,
        mode='vkg',
        user_id='alice',
    )
    query_api.chat_sessions.append_turn(
        session_id=session_id, role='user', text=user_message, turn_id='t0'
    )


# ---------------------------------------------------------------------------
# GET /sessions
# ---------------------------------------------------------------------------


def test_list_sessions_returns_newest_first(client: TestClient) -> None:
    _seed_session(client, session_id='s1', ontology_id='ont-a')
    time.sleep(0.01)  # nosemgrep: arbitrary-sleep — moto DynamoDB needs distinct epoch-second timestamps for sort-order test
    _seed_session(client, session_id='s2', ontology_id='ont-b')
    time.sleep(0.01)  # nosemgrep: arbitrary-sleep — moto DynamoDB needs distinct epoch-second timestamps for sort-order test
    _seed_session(client, session_id='s3', ontology_id='ont-a')

    resp = client.get('/sessions')
    assert resp.status_code == 200
    body = resp.json()
    ids = [s['sessionId'] for s in body['sessions']]
    assert ids == ['s3', 's2', 's1']


def test_list_sessions_hydrates_ontology_name(client: TestClient) -> None:
    _seed_session(client, session_id='s1', ontology_id='ont-a')

    resp = client.get('/sessions')
    body = resp.json()
    row = body['sessions'][0]
    assert row['ontologyId'] == 'ont-a'
    assert row['ontologyName'] == 'Ontology ont-a'
    assert row['mode'] == 'vkg'
    assert row['title'] == 'hello'
    assert 'updatedAt' in row
    assert 'createdAt' in row


def test_list_sessions_excludes_archived(client: TestClient) -> None:
    _seed_session(client, session_id='live', ontology_id='ont-a')
    _seed_session(client, session_id='gone', ontology_id='ont-a')

    import query_api  # noqa: WPS433

    query_api.chat_sessions.archive(session_id='gone')

    resp = client.get('/sessions')
    ids = [s['sessionId'] for s in resp.json()['sessions']]
    assert ids == ['live']


def test_list_sessions_empty_for_new_user(client: TestClient) -> None:
    resp = client.get('/sessions')
    assert resp.status_code == 200
    assert resp.json() == {'sessions': [], 'nextCursor': None}


def test_list_sessions_invalid_cursor_returns_400(client: TestClient) -> None:
    resp = client.get('/sessions?cursor=not-json')
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /sessions/{id} — soft delete
# ---------------------------------------------------------------------------


def test_delete_session_archives_and_removes_from_list(client: TestClient) -> None:
    _seed_session(client, session_id='s1', ontology_id='ont-a')

    resp = client.delete('/sessions/s1')
    assert resp.status_code == 200
    assert resp.json() == {'status': 'archived', 'sessionId': 's1'}

    list_resp = client.get('/sessions')
    assert list_resp.json()['sessions'] == []


def test_delete_missing_session_returns_404(client: TestClient) -> None:
    resp = client.delete('/sessions/does-not-exist')
    assert resp.status_code == 404
