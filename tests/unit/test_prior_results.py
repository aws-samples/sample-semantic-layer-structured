"""Unit tests for ``agents.shared.prior_results.get_previous_query_result``.

The tool is a Strands ``@tool``-decorated callable; under test we drive its
underlying function directly via ``.original`` (Strands convention) so we can
verify the DDB read path without spinning up a real agent.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.shared import prior_results
from agents.shared.prior_results import (
    get_previous_query_result,
    set_session_id,
)


# Strands wraps @tool functions in a callable. The wrapped function lives on
# ``.original`` for test harnesses (or on ``__wrapped__`` for older versions).
def _call(**kwargs):
    fn = getattr(get_previous_query_result, 'original', None) or getattr(
        get_previous_query_result, '__wrapped__', None,
    ) or get_previous_query_result
    return fn(**kwargs)


def test_returns_error_when_no_session() -> None:
    set_session_id('')
    result = json.loads(_call(turn_id='t1'))
    assert result == {'error': 'no chat session in context'}


def test_returns_error_when_table_env_missing(monkeypatch) -> None:
    set_session_id('s1')
    monkeypatch.delenv('CHAT_SESSIONS_TABLE', raising=False)
    result = json.loads(_call(turn_id='t1'))
    assert 'CHAT_SESSIONS_TABLE' in result['error']


def test_returns_rows_for_matching_turn(monkeypatch) -> None:
    set_session_id('s1')
    monkeypatch.setenv('CHAT_SESSIONS_TABLE', 'chat-sessions')

    fake_table = MagicMock()
    fake_table.get_item.return_value = {
        'Item': {
            'sessionId': 's1',
            'messages': [
                {'role': 'user', 'turnId': 't1', 'text': 'q'},
                {
                    'role': 'assistant',
                    'turnId': 't1',
                    'text': 'a',
                    'totals': {
                        'sql': 'SELECT id FROM t',
                        'columns': ['id'],
                        'rows': [['1'], ['2']],
                        'rowCount': 2,
                    },
                },
            ],
        },
    }
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with patch.object(prior_results.boto3, 'resource', return_value=fake_resource):
        result = json.loads(_call(turn_id='t1'))

    assert result['turnId'] == 't1'
    assert result['sql'] == 'SELECT id FROM t'
    assert result['columns'] == ['id']
    assert result['rows'] == [['1'], ['2']]
    assert result['rowCount'] == 2
    assert result['truncated'] is False
    fake_table.get_item.assert_called_once_with(Key={'sessionId': 's1'})


def test_caps_returned_rows(monkeypatch) -> None:
    set_session_id('s1')
    monkeypatch.setenv('CHAT_SESSIONS_TABLE', 'chat-sessions')
    monkeypatch.setattr(prior_results, '_MAX_ROWS_TO_RETURN', 3)

    big_rows = [[i] for i in range(10)]
    fake_table = MagicMock()
    fake_table.get_item.return_value = {
        'Item': {
            'messages': [
                {
                    'role': 'assistant',
                    'turnId': 't1',
                    'totals': {
                        'sql': 'q',
                        'columns': ['id'],
                        'rows': big_rows,
                        'rowCount': 10,
                    },
                },
            ],
        },
    }
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with patch.object(prior_results.boto3, 'resource', return_value=fake_resource):
        result = json.loads(_call(turn_id='t1'))

    assert len(result['rows']) == 3
    assert result['truncated'] is True
    assert result['rowCount'] == 10


def test_returns_error_when_turn_missing(monkeypatch) -> None:
    set_session_id('s1')
    monkeypatch.setenv('CHAT_SESSIONS_TABLE', 'chat-sessions')

    fake_table = MagicMock()
    fake_table.get_item.return_value = {
        'Item': {
            'messages': [{'role': 'assistant', 'turnId': 'other', 'totals': {}}],
        },
    }
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with patch.object(prior_results.boto3, 'resource', return_value=fake_resource):
        result = json.loads(_call(turn_id='t1'))

    assert 'not found' in result['error']


def test_returns_error_when_turn_has_no_totals(monkeypatch) -> None:
    set_session_id('s1')
    monkeypatch.setenv('CHAT_SESSIONS_TABLE', 'chat-sessions')

    fake_table = MagicMock()
    fake_table.get_item.return_value = {
        'Item': {'messages': [{'role': 'assistant', 'turnId': 't1'}]},
    }
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table

    with patch.object(prior_results.boto3, 'resource', return_value=fake_resource):
        result = json.loads(_call(turn_id='t1'))

    assert 'no stored result' in result['error']
