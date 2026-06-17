"""Unit tests for ChatSessionService — DynamoDB-backed chat transcript store.

Uses moto's DDB mock so the tests exercise the real boto3 calls without an
actual AWS account.
"""

from __future__ import annotations

import os
import sys
import time

import boto3
import pytest

# Make the rest-api package importable.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

from moto import mock_aws  # type: ignore  # noqa: E402

from services.chat_session_service import (  # noqa: E402
    ChatSessionNotFoundError,
    ChatSessionService,
    SessionOwnershipError,
)


_TABLE_NAME = 'semantic-layer-chat-sessions'


def _build_table() -> None:
    """Provision the DDB table inside the moto mock for each test."""
    client = boto3.client('dynamodb', region_name='us-east-1')
    client.create_table(
        TableName=_TABLE_NAME,
        AttributeDefinitions=[
            {'AttributeName': 'sessionId', 'AttributeType': 'S'},
        ],
        KeySchema=[{'AttributeName': 'sessionId', 'KeyType': 'HASH'}],
        BillingMode='PAY_PER_REQUEST',
    )
    client.update_time_to_live(
        TableName=_TABLE_NAME,
        TimeToLiveSpecification={'AttributeName': 'ttl', 'Enabled': True},
    )


@pytest.fixture
def service():
    """Yield a ChatSessionService bound to a fresh moto-mocked table."""
    with mock_aws():
        _build_table()
        yield ChatSessionService(table_name=_TABLE_NAME, region='us-east-1')


def test_create_session_persists_initial_item(service: ChatSessionService) -> None:
    item = service.create_session(
        session_id='s1',
        ontology_id='ont-123',
        mode='vkg',
        user_id='alice',
    )
    assert item['sessionId'] == 's1'
    assert item['messages'] == []
    assert item['ttl'] > int(time.time())
    # Round-trip via get_session.
    got = service.get_session(session_id='s1')
    assert got['ontologyId'] == 'ont-123'


def test_create_session_rejects_duplicate(service: ChatSessionService) -> None:
    service.create_session(
        session_id='s1', ontology_id='o', mode='vkg', user_id='u'
    )
    with pytest.raises(ValueError):
        service.create_session(
            session_id='s1', ontology_id='o', mode='vkg', user_id='u'
        )


def test_get_session_raises_when_missing(service: ChatSessionService) -> None:
    with pytest.raises(ChatSessionNotFoundError):
        service.get_session(session_id='missing')


def test_append_turn_grows_messages_in_order(service: ChatSessionService) -> None:
    service.create_session(
        session_id='s1', ontology_id='o', mode='vkg', user_id='u'
    )
    service.append_turn(
        session_id='s1', role='user', text='hi', turn_id='t1'
    )
    service.append_turn(
        session_id='s1',
        role='assistant',
        text='hello',
        turn_id='t1',
        reasoning_steps=[{'tool': 'sparql', 'durationMs': 12}],
    )
    msgs = service.get_session(session_id='s1')['messages']
    assert [m['role'] for m in msgs] == ['user', 'assistant']
    assert msgs[1]['reasoningSteps'] == [{'tool': 'sparql', 'durationMs': 12}]


def test_append_turn_refreshes_ttl(service: ChatSessionService) -> None:
    service.create_session(
        session_id='s1', ontology_id='o', mode='vkg', user_id='u'
    )
    initial_ttl = service.get_session(session_id='s1')['ttl']
    # Sleep long enough for epoch-second resolution to advance.
    time.sleep(1.1)  # nosemgrep: arbitrary-sleep — TTL integer must advance by ≥1s to be measurable
    service.append_turn(
        session_id='s1', role='user', text='m', turn_id='t1'
    )
    refreshed_ttl = service.get_session(session_id='s1')['ttl']
    assert refreshed_ttl > initial_ttl


def test_append_turn_rejects_unknown_role(service: ChatSessionService) -> None:
    service.create_session(
        session_id='s1', ontology_id='o', mode='vkg', user_id='u'
    )
    with pytest.raises(ValueError):
        service.append_turn(
            session_id='s1', role='system', text='x', turn_id='t1'
        )


def test_history_window_returns_last_n(service: ChatSessionService) -> None:
    service.create_session(
        session_id='s1', ontology_id='o', mode='vkg', user_id='u'
    )
    for i in range(5):
        service.append_turn(
            session_id='s1', role='user', text=f'msg-{i}', turn_id=f't{i}'
        )
    window = service.history_window(session_id='s1', n=3)
    assert [m['text'] for m in window] == ['msg-2', 'msg-3', 'msg-4']


def test_history_window_returns_empty_for_missing_session(
    service: ChatSessionService,
) -> None:
    assert service.history_window(session_id='nope') == []


def test_get_or_create_creates_when_missing(
    service: ChatSessionService,
) -> None:
    item = service.get_or_create(
        session_id='s1', ontology_id='o', mode='vkg', user_id='u'
    )
    assert item['messages'] == []


def test_get_or_create_returns_existing(service: ChatSessionService) -> None:
    service.create_session(
        session_id='s1', ontology_id='o', mode='vkg', user_id='u'
    )
    service.append_turn(
        session_id='s1', role='user', text='hi', turn_id='t1'
    )
    item = service.get_or_create(
        session_id='s1', ontology_id='other', mode='semantic-rag', user_id='u'
    )
    # Returned item is the existing one — ontologyId not overwritten.
    assert item['ontologyId'] == 'o'
    assert len(item['messages']) == 1


def test_delete_session_is_idempotent(service: ChatSessionService) -> None:
    service.create_session(
        session_id='s1', ontology_id='o', mode='vkg', user_id='u'
    )
    service.delete_session(session_id='s1')
    service.delete_session(session_id='s1')  # second delete is fine
    with pytest.raises(ChatSessionNotFoundError):
        service.get_session(session_id='s1')


# ---------------------------------------------------------------------------
# Session-to-user binding (security)
# ---------------------------------------------------------------------------

def test_get_session_owned_returns_for_owner(service: ChatSessionService) -> None:
    service.create_session(
        session_id='s1', ontology_id='o', mode='vkg', user_id='alice'
    )
    got = service.get_session_owned(session_id='s1', user_id='alice')
    assert got['sessionId'] == 's1'


def test_get_session_owned_rejects_foreign_user(service: ChatSessionService) -> None:
    service.create_session(
        session_id='s1', ontology_id='o', mode='vkg', user_id='victim'
    )
    with pytest.raises(SessionOwnershipError):
        service.get_session_owned(session_id='s1', user_id='attacker')


def test_get_session_owned_missing_raises_not_found(
    service: ChatSessionService,
) -> None:
    with pytest.raises(ChatSessionNotFoundError):
        service.get_session_owned(session_id='missing', user_id='alice')
