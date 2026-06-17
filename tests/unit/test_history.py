"""Unit tests for ``agents.shared.history.to_strands_messages``."""

from __future__ import annotations

from agents.shared.history import to_strands_messages


def test_empty_input_returns_empty_list() -> None:
    assert to_strands_messages(None) == []
    assert to_strands_messages([]) == []


def test_round_trip_user_assistant() -> None:
    """Assistant turns with totals get a [Prior result] pointer appended so
    follow-up turns can reference them via get_previous_query_result."""
    history = [
        {'role': 'user', 'text': 'How many customers?', 'turnId': 't1'},
        {
            'role': 'assistant',
            'text': 'There are 42 customers.',
            'turnId': 't1',
            'reasoningSteps': [{'callId': 'a'}],
            'totals': {'rowCount': 42, 'sql': 'SELECT COUNT(*) FROM customers'},
            'thinking': 'Let me check…',
        },
    ]
    out = to_strands_messages(history)
    assert out[0] == {
        'role': 'user', 'content': [{'text': 'How many customers?'}],
    }
    assert out[1]['role'] == 'assistant'
    asst_text = out[1]['content'][0]['text']
    assert asst_text.startswith('There are 42 customers.')
    assert '[Prior result] turnId=t1' in asst_text
    assert 'rows=42' in asst_text
    assert 'SELECT COUNT(*) FROM customers' in asst_text


def test_assistant_without_totals_has_no_pointer() -> None:
    history = [
        {
            'role': 'assistant',
            'text': 'Hello there.',
            'turnId': 't1',
        },
    ]
    out = to_strands_messages(history)
    assert out == [{'role': 'assistant', 'content': [{'text': 'Hello there.'}]}]


def test_assistant_with_empty_totals_has_no_pointer() -> None:
    """Pointer is dropped when there's no SQL and zero rows — nothing to point at."""
    history = [
        {
            'role': 'assistant',
            'text': 'No rows matched.',
            'turnId': 't1',
            'totals': {'rowCount': 0, 'sql': ''},
        },
    ]
    out = to_strands_messages(history)
    assert out == [
        {'role': 'assistant', 'content': [{'text': 'No rows matched.'}]},
    ]


def test_drops_blank_and_unknown_roles() -> None:
    history = [
        {'role': 'user', 'text': '   '},
        {'role': 'system', 'text': 'ignored'},
        {'role': 'tool', 'text': 'also ignored'},
        {'role': 'assistant', 'text': 'ok'},
        'not-a-dict',
    ]
    out = to_strands_messages(history)
    assert out == [{'role': 'assistant', 'content': [{'text': 'ok'}]}]
