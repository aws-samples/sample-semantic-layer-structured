"""Tests for agents/shared/chat_sessions.py write path."""
import os, sys
from pathlib import Path
from unittest.mock import MagicMock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agents'))
os.environ.setdefault('CHAT_SESSIONS_TABLE', 'test-chat-sessions')


def _svc_with_table():
    from shared.chat_sessions import ChatSessionService
    resource = MagicMock()
    table = MagicMock()
    resource.Table.return_value = table
    return ChatSessionService(ddb_resource=resource), table


def test_append_user_turn_writes_message_and_title():
    svc, table = _svc_with_table()
    svc.append_turn(session_id='s1', role='user', text='hi there', turn_id='t1')
    kwargs = table.update_item.call_args.kwargs
    assert kwargs['Key'] == {'sessionId': 's1'}
    assert ':m' in kwargs['ExpressionAttributeValues']
    msg = kwargs['ExpressionAttributeValues'][':m'][0]
    assert msg == {'role': 'user', 'text': 'hi there', 'turnId': 't1', 'reasoningSteps': []}
    assert ':title' in kwargs['ExpressionAttributeValues']


def test_append_rejects_bad_role():
    svc, _ = _svc_with_table()
    with pytest.raises(ValueError):
        svc.append_turn(session_id='s1', role='system', text='x', turn_id='t')


def test_create_session_defaults_empty_user_id_to_anonymous():
    """Empty userId must become 'anonymous' — DynamoDB rejects an empty string
    for the userId-updatedAt-index GSI key, which silently dropped sessions and
    left the chat sidebar empty."""
    svc, table = _svc_with_table()
    svc.create_session(session_id='s1', ontology_id='o1', mode='semantic-rag',
                       user_id='')
    item = table.put_item.call_args.kwargs['Item']
    assert item['userId'] == 'anonymous'


def test_create_session_preserves_real_user_id():
    svc, table = _svc_with_table()
    svc.create_session(session_id='s1', ontology_id='o1', mode='semantic-rag',
                       user_id='cognito-sub-123')
    item = table.put_item.call_args.kwargs['Item']
    assert item['userId'] == 'cognito-sub-123'


def test_create_session_persists_source():
    """source distinguishes chat vs MCP vs eval traffic on the Monitoring tab."""
    svc, table = _svc_with_table()
    svc.create_session(session_id='s1', ontology_id='o1', mode='vkg',
                       user_id='u', source='mcp')
    item = table.put_item.call_args.kwargs['Item']
    assert item['source'] == 'mcp'


def test_create_session_source_defaults_to_chat():
    svc, table = _svc_with_table()
    svc.create_session(session_id='s2', ontology_id='o1', mode='vkg', user_id='u')
    item = table.put_item.call_args.kwargs['Item']
    assert item['source'] == 'chat'


def test_get_or_create_forwards_source_on_create():
    """get_or_create must thread source into the create branch (write-once)."""
    svc, table = _svc_with_table()
    table.get_item.return_value = {}  # not found → create branch
    svc.get_or_create(session_id='s3', ontology_id='o1', mode='vkg',
                      user_id='u', source='eval')
    item = table.put_item.call_args.kwargs['Item']
    assert item['source'] == 'eval'


def test_append_assistant_includes_totals_and_thinking():
    svc, table = _svc_with_table()
    svc.append_turn(session_id='s1', role='assistant', text='ans', turn_id='t2',
                    totals={'rowCount': 1}, thinking_text='reasoned')
    msg = table.update_item.call_args.kwargs['ExpressionAttributeValues'][':m'][0]
    assert msg['totals'] == {'rowCount': 1} and msg['thinking'] == 'reasoned'


def test_append_assistant_converts_floats_to_decimal():
    """Regression: a totals payload with floats (scores, runtimeMs, usage) must
    be Decimal-converted before reaching update_item. Native float makes the
    boto3 DynamoDB resource raise, which previously dropped the assistant turn
    (user bubble shown, assistant response missing on reload)."""
    from decimal import Decimal
    svc, table = _svc_with_table()
    svc.append_turn(
        session_id='s1', role='assistant', text='ans', turn_id='t2',
        totals={
            'rowCount': 3,
            'runtimeMs': 1234.5,
            'usage': {'totalTokens': 42},
            'kbSources': [{'score': 0.91, 'content': 'x'}],
        },
    )
    msg = table.update_item.call_args.kwargs['ExpressionAttributeValues'][':m'][0]
    totals = msg['totals']
    # Every float is now a Decimal; ints and strings are untouched.
    assert totals['runtimeMs'] == Decimal('1234.5')
    assert isinstance(totals['runtimeMs'], Decimal)
    assert totals['rowCount'] == 3 and isinstance(totals['rowCount'], int)
    assert isinstance(totals['kbSources'][0]['score'], Decimal)
    assert totals['kbSources'][0]['content'] == 'x'
    # No native float survives anywhere in the persisted message.
    def _has_float(o):
        if isinstance(o, bool):
            return False
        if isinstance(o, float):
            return True
        if isinstance(o, dict):
            return any(_has_float(v) for v in o.values())
        if isinstance(o, (list, tuple)):
            return any(_has_float(v) for v in o)
        return False
    assert not _has_float(msg)


def test_append_drops_non_finite_floats():
    """NaN / Infinity cannot be stored in DynamoDB — coerced to None."""
    svc, table = _svc_with_table()
    svc.append_turn(session_id='s1', role='assistant', text='a', turn_id='t',
                    totals={'a': float('nan'), 'b': float('inf')})
    totals = table.update_item.call_args.kwargs['ExpressionAttributeValues'][':m'][0]['totals']
    assert totals['a'] is None and totals['b'] is None


def test_history_window_returns_last_n_messages_in_order():
    svc, table = _svc_with_table()
    messages = [{'role': 'user', 'text': str(i)} for i in range(15)]
    table.get_item.return_value = {'Item': {'sessionId': 's1', 'messages': messages}}
    result = svc.history_window(session_id='s1', n=10)
    assert result == messages[-10:]


def test_history_window_missing_session_returns_empty_list():
    svc, table = _svc_with_table()
    table.get_item.return_value = {}
    assert svc.history_window(session_id='missing') == []


def test_get_or_create_returns_existing_without_creating():
    svc, table = _svc_with_table()
    existing = {'sessionId': 's1', 'ontologyId': 'o1', 'mode': 'vkg',
                'userId': 'u1', 'messages': []}
    table.get_item.return_value = {'Item': existing}
    result = svc.get_or_create(session_id='s1', ontology_id='o1', mode='vkg', user_id='u1')
    assert result is existing
    table.put_item.assert_not_called()


def test_get_or_create_creates_when_missing():
    svc, table = _svc_with_table()
    table.get_item.return_value = {}
    result = svc.get_or_create(session_id='s2', ontology_id='o2', mode='semantic-rag', user_id='u2')
    table.put_item.assert_called_once()
    assert result['sessionId'] == 's2'
    assert result['mode'] == 'semantic-rag'
    assert result['ontologyId'] == 'o2'
    assert result['userId'] == 'u2'
    assert result['messages'] == []


def test_append_turn_refreshes_ttl_and_updated_at():
    svc, table = _svc_with_table()
    svc.append_turn(session_id='s1', role='user', text='hello', turn_id='t1')
    kwargs = table.update_item.call_args.kwargs
    values = kwargs['ExpressionAttributeValues']
    assert isinstance(values[':ttl'], int) and values[':ttl'] > 0
    assert isinstance(values[':now'], str)
    assert kwargs['ExpressionAttributeNames']['#ttl'] == 'ttl'


# ---------------------------------------------------------------------------
# Session-to-user binding (security): ownership must be enforced on every touch
# ---------------------------------------------------------------------------

def test_get_session_owned_returns_item_for_owner():
    svc, table = _svc_with_table()
    table.get_item.return_value = {'Item': {'sessionId': 's1', 'userId': 'u1'}}
    item = svc.get_session_owned(session_id='s1', user_id='u1')
    assert item['sessionId'] == 's1'


def test_get_session_owned_raises_on_foreign_user():
    from shared.chat_sessions import SessionOwnershipError
    svc, table = _svc_with_table()
    table.get_item.return_value = {'Item': {'sessionId': 's1', 'userId': 'victim'}}
    with pytest.raises(SessionOwnershipError):
        svc.get_session_owned(session_id='s1', user_id='attacker')


def test_get_session_owned_missing_raises_not_found():
    from shared.chat_sessions import ChatSessionNotFoundError
    svc, table = _svc_with_table()
    table.get_item.return_value = {}
    with pytest.raises(ChatSessionNotFoundError):
        svc.get_session_owned(session_id='missing', user_id='u1')


def test_get_or_create_raises_when_existing_session_owned_by_other():
    """A valid JWT must not hijack another user's existing session."""
    from shared.chat_sessions import SessionOwnershipError
    svc, table = _svc_with_table()
    table.get_item.return_value = {'Item': {'sessionId': 's1', 'userId': 'victim'}}
    with pytest.raises(SessionOwnershipError):
        svc.get_or_create(session_id='s1', ontology_id='o1', mode='vkg',
                          user_id='attacker')
    table.put_item.assert_not_called()


def test_append_turn_adds_owner_condition_when_user_id_given():
    """With user_id, the write is guarded by an atomic userId = :uid condition."""
    svc, table = _svc_with_table()
    svc.append_turn(session_id='s1', role='user', text='hi', turn_id='t1',
                    user_id='u1')
    kwargs = table.update_item.call_args.kwargs
    assert 'userId = :uid' in kwargs['ConditionExpression']
    assert kwargs['ExpressionAttributeValues'][':uid'] == 'u1'


def test_append_turn_no_owner_condition_when_user_id_omitted():
    """Backward compat: without user_id the condition stays existence-only."""
    svc, table = _svc_with_table()
    svc.append_turn(session_id='s1', role='user', text='hi', turn_id='t1')
    kwargs = table.update_item.call_args.kwargs
    assert kwargs['ConditionExpression'] == 'attribute_exists(sessionId)'
    assert ':uid' not in kwargs['ExpressionAttributeValues']


def test_append_turn_foreign_owner_raises_ownership_error():
    """The DB-level owner guard rejects a write to a foreign session."""
    from botocore.exceptions import ClientError
    from shared.chat_sessions import SessionOwnershipError
    svc, table = _svc_with_table()
    table.update_item.side_effect = ClientError(
        {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem')
    with pytest.raises(SessionOwnershipError):
        svc.append_turn(session_id='s1', role='user', text='hi', turn_id='t1',
                        user_id='attacker')


def test_history_window_returns_empty_for_foreign_session():
    """A forged sessionId must not leak the victim's transcript."""
    svc, table = _svc_with_table()
    table.get_item.return_value = {'Item': {
        'sessionId': 's1', 'userId': 'victim',
        'messages': [{'role': 'user', 'text': 'secret'}]}}
    assert svc.history_window(session_id='s1', user_id='attacker') == []


def test_history_window_owner_sees_messages():
    svc, table = _svc_with_table()
    msgs = [{'role': 'user', 'text': 'mine'}]
    table.get_item.return_value = {'Item': {
        'sessionId': 's1', 'userId': 'u1', 'messages': msgs}}
    assert svc.history_window(session_id='s1', user_id='u1') == msgs


def test_enforce_session_cap_archives_overflow(monkeypatch):
    """Sessions beyond the cap (newest-first) are archived."""
    import shared.chat_sessions as cs
    monkeypatch.setattr(cs, '_MAX_SESSIONS_PER_USER', 2)
    svc, table = _svc_with_table()
    # 4 sessions, newest-first → s3, s2 kept; s1, s0 archived.
    table.query.return_value = {'Items': [
        {'sessionId': 's3'}, {'sessionId': 's2'},
        {'sessionId': 's1'}, {'sessionId': 's0'}]}
    archived = svc.enforce_session_cap(user_id='u1')
    assert archived == 2
    archived_ids = {c.kwargs['Key']['sessionId']
                    for c in table.update_item.call_args_list}
    assert archived_ids == {'s1', 's0'}


def test_enforce_session_cap_noop_under_cap(monkeypatch):
    import shared.chat_sessions as cs
    monkeypatch.setattr(cs, '_MAX_SESSIONS_PER_USER', 50)
    svc, table = _svc_with_table()
    table.query.return_value = {'Items': [{'sessionId': 's0'}]}
    assert svc.enforce_session_cap(user_id='u1') == 0
    table.update_item.assert_not_called()
