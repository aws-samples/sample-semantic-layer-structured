"""Tests for the hybrid query agent's copy-and-own AG-UI streaming runner.

Mirrors ``tests/unit/test_streaming_runner.py`` against the vendored copy at
``agents.semantic_layer_query_agent.streaming_runner`` so the duplicated code is
exercised. The hybrid copy's ``executedSql`` logic only reads the top-level
``executed_sql`` slot (no ``reasoning.sqlQuery`` fallback), so that one scenario
is adapted accordingly.
"""

from __future__ import annotations

import time

from agents.semantic_layer_query_agent.agui_emitter import AGUIEmitter
from agents.semantic_layer_query_agent.streaming_runner import stream_agent_run


def test_streaming_runner_yields_callback_events_then_finishes():
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        callback(toolUse={'toolUseId': 'tu-1', 'name': 'execute_sql_query', 'input': {'sql': 'SELECT 1'}})
        time.sleep(0.01)  # nosemgrep: arbitrary-sleep — gap so the poll loop captures both events separately
        callback(toolResult={'toolUseId': 'tu-1', 'content': [{'json': {'rows': 1}}]})
        return {'answer': 'OK', 'sql_query': 'SELECT 1', 'results': [{'a': 1}]}

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
        )
    )
    types = [evt['type'] for evt in out]
    assert 'tool_call_start' in types
    assert 'tool_call_end' in types
    assert types[-1] == 'run_finished'


def test_streaming_runner_emits_message_chunk_when_no_text_streamed():
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        return {'answer': 'Static answer', 'sql_query': '', 'results': []}

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
        )
    )
    deltas = [evt.get('delta') for evt in out if evt['type'] == 'message_chunk']
    assert deltas == ['Static answer']


def test_streaming_runner_surfaces_graph_traversal_in_totals():
    emitter = AGUIEmitter(turn_id="t-1")

    def run_agent(callback):
        return {
            "answer": "15 addresses.",
            "sql_query": "SELECT count(*) FROM normalized.address",
            "results": [{"c": 15}],
            "n_quads": ["<a> <b> <c> ."],
            "reasoning": {
                "graphTraversal": "addresses → Address (normalized.address)",
            },
        }

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
        )
    )
    finished = [e for e in out if e["type"] == "run_finished"]
    assert finished, "expected a run_finished event"
    assert (
        finished[-1]["totals"]["graphTraversal"]
        == "addresses → Address (normalized.address)"
    )


def test_streaming_runner_executed_sql_uses_toplevel():
    """The hybrid copy surfaces the executed SQL from the top-level
    ``executed_sql`` slot into ``totals['executedSql']``."""
    emitter = AGUIEmitter(turn_id="t-rag")

    def run_agent(callback):
        return {
            "answer": "ok",
            "sql_query": "SELECT 1",
            "results": [],
            "executed_sql": "SELECT 1 FROM dual",
            "reasoning": {},
        }

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
        )
    )
    totals = [e for e in out if e["type"] == "run_finished"][-1]["totals"]
    assert totals["executedSql"] == "SELECT 1 FROM dual"


def test_streaming_runner_renders_clarification_as_message_chunk():
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        return {
            'answer': '',
            'needs_clarification': True,
            'clarification_question': 'Which code did you mean?',
            'options': [
                {'id': 'state_tc', 'label': 'State Code — normalized.address.state_tc'},
                {'id': 'zip', 'label': 'ZIP Code — normalized.address.zip'},
            ],
            'sql_query': '',
            'results': [],
        }

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
        )
    )
    text = ''.join(
        evt.get('delta') or '' for evt in out if evt['type'] == 'message_chunk'
    )
    assert 'Which code did you mean?' in text
    assert 'State Code — normalized.address.state_tc' in text
    assert 'ZIP Code — normalized.address.zip' in text
    assert out[-1]['type'] == 'run_finished'


def test_streaming_runner_surfaces_callback_text_chunks():
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        callback(data='hello ')
        callback(data='world')
        return {'answer': 'hello world', 'sql_query': '', 'results': []}

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
        )
    )
    deltas = [evt.get('delta') for evt in out if evt['type'] == 'message_chunk']
    assert deltas == ['hello ', 'world']


def test_streaming_runner_surfaces_run_error_on_exception():
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        raise RuntimeError('Bedrock down')

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
        )
    )
    last = out[-1]
    assert last['type'] == 'run_error'
    assert 'Bedrock down' in last['error']


def test_run_finished_totals_carry_sql_rows_and_kb_sources():
    emitter = AGUIEmitter(turn_id='t-1')
    rows = [{'a': i} for i in range(3)]
    kb = [{'sourceUri': 's3://bucket/doc', 'relevance': 0.91}]

    def run_agent(callback):
        return {
            'answer': 'three rows',
            'sql_query': 'SELECT a FROM t',
            'results': rows,
            'n_quads': kb,
        }

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
        )
    )
    finished = out[-1]
    assert finished['type'] == 'run_finished'
    totals = finished['totals']
    assert totals['sql'] == 'SELECT a FROM t'
    assert totals['rowCount'] == 3
    assert totals['rows'] == rows
    assert totals['truncated'] is False
    assert totals['kbSources'] == kb


def test_run_finished_totals_caps_rows_at_200():
    emitter = AGUIEmitter(turn_id='t-1')
    rows = [{'a': i} for i in range(500)]

    def run_agent(callback):
        return {
            'answer': '500 rows',
            'sql_query': 'SELECT *',
            'results': rows,
            'n_quads': [],
        }

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
        )
    )
    totals = out[-1]['totals']
    assert totals['rowCount'] == 500
    assert len(totals['rows']) == 200
    assert totals['truncated'] is True


def test_streaming_runner_emits_thinking_chunk_on_reasoning_kwargs():
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        callback(reasoning_text='I should query admin_codes table.')
        callback(reasoningContent={'text': ' Then count the rows.'})
        return {'answer': 'final', 'sql_query': '', 'results': []}

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
        )
    )
    thinking_deltas = [evt.get('delta') for evt in out if evt['type'] == 'thinking_chunk']
    msg_deltas = [evt.get('delta') for evt in out if evt['type'] == 'message_chunk']
    assert thinking_deltas == [
        'I should query admin_codes table.',
        ' Then count the rows.',
    ]
    assert msg_deltas == ['final']


def test_streaming_runner_reclassifies_pre_tool_text_as_thinking():
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        callback(data='Let me query the metadata first. ')
        callback(data='I need to look up the table name.')
        callback(toolUse={'toolUseId': 'tu-1', 'name': 'retrieve_kb_context', 'input': {'q': 'metric'}})
        callback(toolResult={'toolUseId': 'tu-1', 'content': [{'json': {'rows': []}}]})
        callback(data='The answer is 42.')
        return {'answer': 'The answer is 42.', 'sql_query': '', 'results': []}

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
        )
    )
    thinking = [evt.get('delta') for evt in out if evt['type'] == 'thinking_chunk']
    msg = [evt.get('delta') for evt in out if evt['type'] == 'message_chunk']
    assert thinking == [
        'Let me query the metadata first. ',
        'I need to look up the table name.',
    ]
    assert msg == ['The answer is 42.']


def test_streaming_runner_tool_events_carry_real_args_and_results():
    emitter = AGUIEmitter(turn_id='t-1')
    sql_args = {'sql': 'SELECT count(*) FROM admin_codes'}
    result_content = [{'json': {'rows': [[42]], 'columns': ['count']}}]

    def run_agent(callback):
        callback(toolUse={'toolUseId': 'tu-1', 'name': 'execute_sql_query', 'input': sql_args})
        callback(toolResult={'toolUseId': 'tu-1', 'content': result_content})
        return {'answer': 'done', 'sql_query': sql_args['sql'], 'results': []}

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
        )
    )
    starts = [evt for evt in out if evt['type'] == 'tool_call_start']
    ends = [evt for evt in out if evt['type'] == 'tool_call_end']
    assert len(starts) == 1
    assert starts[0]['toolName'] == 'execute_sql_query'
    assert starts[0]['args'] == sql_args
    assert len(ends) == 1
    assert ends[0]['result'] == result_content


def test_streaming_runner_times_out_a_hung_agent():
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        time.sleep(5)  # nosemgrep: arbitrary-sleep — simulates hung agent to verify timeout path
        return {'answer': 'never'}

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=0.1,
        )
    )
    last = out[-1]
    assert last['type'] == 'run_error'
    assert 'max_wait_seconds' in last['error']


def test_on_result_called_with_run_finished_answer_and_totals():
    emitter = AGUIEmitter(turn_id='t-1')
    rows = [{'a': i} for i in range(3)]
    kb = [{'sourceUri': 's3://bucket/doc', 'relevance': 0.91}]

    def run_agent(callback):
        return {
            'answer': 'three rows',
            'sql_query': 'SELECT a FROM t',
            'results': rows,
            'n_quads': kb,
        }

    captured = {}

    def on_result(answer_text, totals):
        captured['answer'] = answer_text
        captured['totals'] = totals

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
            on_result=on_result,
        )
    )
    finished = out[-1]
    assert finished['type'] == 'run_finished'
    assert captured['answer'] == 'three rows'
    assert captured['totals'] == finished['totals']
    assert captured['totals']['sql'] == 'SELECT a FROM t'
    assert captured['totals']['rowCount'] == 3
    assert captured['totals']['kbSources'] == kb


def test_on_result_failure_does_not_break_stream():
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        return {'answer': 'still finishes', 'sql_query': '', 'results': []}

    def on_result(answer_text, totals):
        raise RuntimeError('DDB write failed')

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
            on_result=on_result,
        )
    )
    assert out[-1]['type'] == 'run_finished'


def test_on_result_not_called_on_error_path():
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        raise RuntimeError('Bedrock down')

    calls = []

    def on_result(answer_text, totals):
        calls.append((answer_text, totals))

    out = list(
        stream_agent_run(
            emitter=emitter, run_agent=run_agent,
            poll_interval=0.005, max_wait_seconds=2,
            on_result=on_result,
        )
    )
    assert out[-1]['type'] == 'run_error'
    assert calls == []
