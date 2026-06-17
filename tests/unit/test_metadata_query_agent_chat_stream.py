"""Tests for the AG-UI chat dispatch on the metadata (Semantic-RAG) query agent."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'agents'),
)


@pytest.fixture
def agent_module():
    from metadata_query_agent import main as mqa  # noqa: WPS433
    return mqa


def test_chat_dispatch_emits_full_event_sequence(agent_module):
    single_shot = {
        'answer': 'Found 3 active customers.',
        'sql_query': 'SELECT * FROM normalized.party',
        'results': [{'a': 1}, {'a': 2}, {'a': 3}],
        'n_quads': [{
            'sourceUri': 'kb://doc',
            'content': 'Full chunk content streamed back to UI.',
            'excerpt': 'Full chunk content streamed back to UI.',
            'score': 0.91,
        }],
    }
    allow = MagicMock()
    allow.apply.return_value = {'blocked': False, 'message': '', 'action': 'NONE'}
    with patch.object(agent_module, '_run_query', return_value=single_shot), \
         patch.object(agent_module, '_guardrails', allow), \
         patch.object(agent_module, '_chat_sessions', MagicMock()):
        gen = agent_module.invoke(
            {
                'turnId': 't1',
                'sessionId': 'sess',
                'ontologyId': 'ont-1',
                'message': 'how many customers?',
                'messages': [],
                'mode': 'semantic-rag',
            },
            None,
        )
        events = list(gen)

    types = [evt['type'] for evt in events]
    assert types[0] == 'run_started'
    assert types[-1] == 'run_finished'
    assert 'tool_call_start' in types
    assert 'message_chunk' in types

    deltas = [evt['delta'] for evt in events if evt['type'] == 'message_chunk']
    assert ''.join(deltas) == 'Found 3 active customers.'

    totals = events[-1]['totals']
    assert totals['sql'] == 'SELECT * FROM normalized.party'
    assert totals['rowCount'] == 3
    assert totals['rows'] == [{'a': 1}, {'a': 2}, {'a': 3}]
    kb_sources = totals['kbSources']
    assert len(kb_sources) == 1
    assert kb_sources[0]['sourceUri'] == 'kb://doc'
    assert kb_sources[0]['content'] == 'Full chunk content streamed back to UI.'


def test_single_shot_payload_returns_dict(agent_module):
    single_shot = {'answer': 'ok'}
    with patch.object(agent_module, '_run_query', return_value=single_shot):
        out = agent_module.invoke({'question': 'q', 'id': 'i'}, None)
    assert out == single_shot


def test_chat_emits_run_error_on_failure(agent_module):
    with patch.object(
        agent_module, '_run_query', side_effect=RuntimeError('bad')
    ):
        gen = agent_module.invoke(
            {'turnId': 't1', 'message': 'q', 'ontologyId': 'i'}, None
        )
        events = list(gen)
    assert events[-1]['type'] == 'run_error'
    assert 'bad' in events[-1]['error']


def test_input_guardrail_blocks_before_model(agent_module):
    from unittest.mock import patch, MagicMock
    blocking = MagicMock()
    blocking.apply.return_value = {'blocked': True, 'message': 'nope', 'action': 'GUARDRAIL_INTERVENED'}
    with patch.object(agent_module, '_guardrails', blocking), \
         patch.object(agent_module, '_run_query') as inv, \
         patch.object(agent_module, '_chat_sessions', MagicMock()):
        events = list(agent_module.invoke(
            {'turnId': 't1', 'sessionId': 's', 'ontologyId': 'o', 'mode': 'semantic-rag',
             'message': 'bad', 'messages': []}, None))
    inv.assert_not_called()
    err = [e for e in events if e.get('type') == 'run_error'][0]
    assert err['reason'] == 'GUARDRAIL_INPUT' and err['error'] == 'nope'


def test_user_and_assistant_turns_persisted_fallback(agent_module):
    from unittest.mock import patch, MagicMock
    allow = MagicMock(); allow.apply.return_value = {'blocked': False, 'message': '', 'action': 'NONE'}
    sessions = MagicMock()
    with patch.object(agent_module, '_guardrails', allow), \
         patch.object(agent_module, '_chat_sessions', sessions), \
         patch.object(agent_module, '_run_query',
                      return_value={'answer': 'hi', 'sql_query': '', 'results': [], 'n_quads': [], 'metadata': {}}):
        list(agent_module.invoke(
            {'turnId': 't1', 'sessionId': 's', 'ontologyId': 'o', 'mode': 'semantic-rag',
             'message': 'q', 'messages': []}, None))
    roles = [c.kwargs['role'] for c in sessions.append_turn.call_args_list]
    assert roles == ['user', 'assistant']
