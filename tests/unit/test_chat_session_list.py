"""Unit tests for ChatSessionService list / archive / title backfill.

Companion to ``test_chat_session_service.py``. The list endpoint depends on
the ``userId-updatedAt-index`` GSI, so this test file builds the table with
that GSI explicitly. moto's DDB mock supports BatchGetItem and GSI Query so
we can exercise the full code path without hitting AWS.
"""

from __future__ import annotations

import os
import sys
import time

import boto3
import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

from moto import mock_aws  # type: ignore  # noqa: E402

from services.chat_session_service import (  # noqa: E402
    ChatSessionNotFoundError,
    ChatSessionService,
)


_TABLE_NAME = 'semantic-layer-chat-sessions'
_GSI_NAME = 'userId-updatedAt-index'


def _build_table_with_gsi() -> None:
    """Provision the DDB table + GSI used by ``list_for_user``."""
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


@pytest.fixture
def service():
    """Yield a ChatSessionService bound to a fresh moto-mocked table+GSI."""
    with mock_aws():
        _build_table_with_gsi()
        yield ChatSessionService(table_name=_TABLE_NAME, region='us-east-1')


# ---------------------------------------------------------------------
# list_for_user
# ---------------------------------------------------------------------


def test_list_for_user_returns_newest_first(service: ChatSessionService) -> None:
    """Three sessions for one user — newest updatedAt comes first."""
    for i, sid in enumerate(['s1', 's2', 's3']):
        service.create_session(
            session_id=sid, ontology_id='ont', mode='vkg', user_id='alice'
        )
        # Append a turn so updatedAt advances; sleep so moto returns
        # distinct ISO timestamps.
        service.append_turn(
            session_id=sid, role='user', text=f'q{i}', turn_id=f't{i}'
        )
        time.sleep(0.01)  # nosemgrep: arbitrary-sleep — moto DynamoDB needs distinct epoch-second timestamps for sort-order test

    result = service.list_for_user(user_id='alice')
    ids = [s['sessionId'] for s in result['sessions']]
    assert ids == ['s3', 's2', 's1']


def test_list_for_user_excludes_archived(service: ChatSessionService) -> None:
    """Archived sessions don't appear in the list."""
    service.create_session(
        session_id='live', ontology_id='ont', mode='vkg', user_id='alice'
    )
    service.append_turn(
        session_id='live', role='user', text='q', turn_id='t0'
    )
    service.create_session(
        session_id='gone', ontology_id='ont', mode='vkg', user_id='alice'
    )
    service.append_turn(
        session_id='gone', role='user', text='q', turn_id='t1'
    )
    service.archive(session_id='gone')

    result = service.list_for_user(user_id='alice')
    ids = [s['sessionId'] for s in result['sessions']]
    assert ids == ['live']


def test_list_for_user_isolates_users(service: ChatSessionService) -> None:
    """Alice and Bob see separate lists."""
    service.create_session(
        session_id='a1', ontology_id='ont', mode='vkg', user_id='alice'
    )
    service.append_turn(session_id='a1', role='user', text='q', turn_id='t')
    service.create_session(
        session_id='b1', ontology_id='ont', mode='vkg', user_id='bob'
    )
    service.append_turn(session_id='b1', role='user', text='q', turn_id='t')

    alice_ids = [s['sessionId'] for s in service.list_for_user(user_id='alice')['sessions']]
    bob_ids = [s['sessionId'] for s in service.list_for_user(user_id='bob')['sessions']]
    assert alice_ids == ['a1']
    assert bob_ids == ['b1']


def test_list_for_user_empty_when_no_sessions(service: ChatSessionService) -> None:
    """Brand new user → empty list, no cursor."""
    result = service.list_for_user(user_id='nobody')
    assert result == {'sessions': [], 'nextCursor': None}


# ---------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------


def test_archive_sets_archived_flag(service: ChatSessionService) -> None:
    service.create_session(
        session_id='s1', ontology_id='ont', mode='vkg', user_id='u'
    )
    service.archive(session_id='s1')
    item = service.get_session(session_id='s1')
    assert item.get('archived') is True


def test_archive_missing_session_raises(service: ChatSessionService) -> None:
    with pytest.raises(ChatSessionNotFoundError):
        service.archive(session_id='nope')


# ---------------------------------------------------------------------
# set_title + lazy backfill
# ---------------------------------------------------------------------


def test_append_turn_backfills_title_from_first_user_message(
    service: ChatSessionService,
) -> None:
    service.create_session(
        session_id='s1', ontology_id='ont', mode='vkg', user_id='u'
    )
    service.append_turn(
        session_id='s1', role='user', text='What is my premium?', turn_id='t0'
    )
    item = service.get_session(session_id='s1')
    assert item['title'] == 'What is my premium?'


def test_append_turn_does_not_overwrite_existing_title(
    service: ChatSessionService,
) -> None:
    service.create_session(
        session_id='s1', ontology_id='ont', mode='vkg', user_id='u'
    )
    service.append_turn(
        session_id='s1', role='user', text='first question', turn_id='t0'
    )
    service.append_turn(
        session_id='s1', role='user', text='second question', turn_id='t1'
    )
    item = service.get_session(session_id='s1')
    assert item['title'] == 'first question'


def test_append_turn_truncates_long_titles(service: ChatSessionService) -> None:
    service.create_session(
        session_id='s1', ontology_id='ont', mode='vkg', user_id='u'
    )
    long_text = 'q' * 200
    service.append_turn(
        session_id='s1', role='user', text=long_text, turn_id='t0'
    )
    item = service.get_session(session_id='s1')
    assert len(item['title']) == 80


def test_set_title_overrides(service: ChatSessionService) -> None:
    service.create_session(
        session_id='s1', ontology_id='ont', mode='vkg', user_id='u'
    )
    service.append_turn(
        session_id='s1', role='user', text='auto', turn_id='t0'
    )
    service.set_title(session_id='s1', title='Manual title')
    item = service.get_session(session_id='s1')
    assert item['title'] == 'Manual title'


def test_set_title_missing_session_raises(service: ChatSessionService) -> None:
    with pytest.raises(ChatSessionNotFoundError):
        service.set_title(session_id='nope', title='x')
