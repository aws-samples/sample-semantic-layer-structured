"""Tests for the AG-UI streaming chat dispatch path on the ontology query agent.

The agent's single ``invoke`` entrypoint dispatches between the legacy
single-shot path and the AG-UI streaming chat generator based on payload
shape. We patch ``_run_query`` so the test stays hermetic — it doesn't
need Neptune, Athena, or Bedrock.
"""

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
    from ontology_query_agent import main as oqa  # noqa: WPS433
    return oqa


def test_chat_payload_dispatches_to_stream(agent_module):
    legacy_result = {
        'answer': 'Two policies are active.',
        'sql_query': 'SELECT count(*) FROM holdings',
        'results': [{'count': 2}],
        'n_quads': [],
        'reasoning': {'dataSourceSelection': 'Athena execution: ex-1'},
    }
    allow = MagicMock()
    allow.apply.return_value = {'blocked': False, 'message': '', 'action': 'NONE'}
    with patch.object(agent_module, '_run_query', return_value=legacy_result), \
         patch.object(agent_module, '_guardrails', allow), \
         patch.object(agent_module, '_chat_sessions', MagicMock()):
        gen = agent_module.invoke(
            {
                'turnId': 't1',
                'sessionId': 'sess',
                'ontologyId': 'ont-1',
                'message': 'how many active policies?',
                'messages': [],
                'mode': 'vkg',
            }
        )
        events = list(gen)

    types = [evt['type'] for evt in events]
    # Required sequence: run_started → tool_call_start → tool_call_end →
    # at least one message_chunk → run_finished
    assert types[0] == 'run_started'
    assert 'tool_call_start' in types
    assert 'tool_call_end' in types
    assert 'message_chunk' in types
    assert types[-1] == 'run_finished'

    # The reassembled message_chunks reproduce the legacy answer text.
    deltas = [evt['delta'] for evt in events if evt['type'] == 'message_chunk']
    assert ''.join(deltas) == 'Two policies are active.'

    # run_finished totals carry sql + rowCount + the inline row payload so
    # the frontend can render the result panel without a second call.
    totals = events[-1]['totals']
    assert totals['sql'] == 'SELECT count(*) FROM holdings'
    assert totals['rowCount'] == 1
    assert totals['rows'] == [{'count': 2}]
    assert totals['truncated'] is False
    assert totals['kbSources'] == []


def test_legacy_payload_returns_dict_directly(agent_module):
    """A payload without turnId/messages takes the legacy path."""
    legacy_result = {'answer': 'ok', 'sql_query': '', 'results': [], 'n_quads': []}
    with patch.object(agent_module, '_run_query', return_value=legacy_result):
        out = agent_module.invoke({'question': 'hi', 'id': 'ont-1'})
    assert out == legacy_result


def test_chat_emits_run_error_on_legacy_exception(agent_module):
    allow = MagicMock()
    allow.apply.return_value = {'blocked': False, 'message': '', 'action': 'NONE'}
    with patch.object(
        agent_module, '_run_query', side_effect=RuntimeError('boom')
    ), patch.object(agent_module, '_guardrails', allow), \
            patch.object(agent_module, '_chat_sessions', MagicMock()):
        gen = agent_module.invoke(
            {'turnId': 't1', 'message': 'q', 'ontologyId': 'ont-1'}
        )
        events = list(gen)

    types = [evt['type'] for evt in events]
    assert types[0] == 'run_started'
    assert types[-1] == 'run_error'
    assert 'boom' in events[-1]['error']


def test_input_guardrail_blocks_before_model(agent_module):
    from unittest.mock import patch, MagicMock
    blocking = MagicMock()
    blocking.apply.return_value = {'blocked': True, 'message': 'nope', 'action': 'GUARDRAIL_INTERVENED'}
    with patch.object(agent_module, '_guardrails', blocking), \
         patch.object(agent_module, '_run_query') as rq, \
         patch.object(agent_module, '_chat_sessions', MagicMock()):
        events = list(agent_module.invoke(
            {'turnId': 't1', 'sessionId': 's', 'ontologyId': 'o', 'mode': 'vkg',
             'message': 'bad', 'messages': []}, None))
    rq.assert_not_called()
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
            {'turnId': 't1', 'sessionId': 's', 'ontologyId': 'o', 'mode': 'vkg',
             'message': 'q', 'messages': []}, None))
    roles = [c.kwargs['role'] for c in sessions.append_turn.call_args_list]
    assert roles == ['user', 'assistant']
