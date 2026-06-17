"""Tests for the AG-UI streaming runner."""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'agents'),
)

from shared.agui_emitter import AGUIEmitter  # noqa: E402
from shared.streaming_runner import stream_agent_run  # noqa: E402


def test_streaming_runner_yields_callback_events_then_finishes():
    """When run_agent fires the callback with tool events, those events
    must reach the generator before run_finished."""
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        callback(toolUse={'toolUseId': 'tu-1', 'name': 'execute_sql_query', 'input': {'sql': 'SELECT 1'}})
        time.sleep(0.01)  # nosemgrep: arbitrary-sleep — gap between tool_use/tool_result so the poll loop captures both events separately
        callback(toolResult={'toolUseId': 'tu-1', 'content': [{'json': {'rows': 1}}]})
        return {'answer': 'OK', 'sql_query': 'SELECT 1', 'results': [{'a': 1}]}

    out = list(
        stream_agent_run(
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
        )
    )
    types = [evt['type'] for evt in out]
    assert 'tool_call_start' in types
    assert 'tool_call_end' in types
    assert types[-1] == 'run_finished'


def test_streaming_runner_emits_message_chunk_when_no_text_streamed():
    """If the callback never emits text, the runner converts the final
    answer into a single message_chunk so the UI gets something to render."""
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        return {'answer': 'Static answer', 'sql_query': '', 'results': []}

    out = list(
        stream_agent_run(
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
        )
    )
    deltas = [evt.get('delta') for evt in out if evt['type'] == 'message_chunk']
    assert deltas == ['Static answer']


def test_streaming_runner_surfaces_graph_traversal_in_totals():
    """The VKG agent's readable term → Class (table) summary lives in
    result['reasoning']['graphTraversal']; run_finished totals must carry it so
    the chat UI can render readable mapping chips (not just raw n-quads)."""
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
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
        )
    )
    finished = [e for e in out if e["type"] == "run_finished"]
    assert finished, "expected a run_finished event"
    assert (
        finished[-1]["totals"]["graphTraversal"]
        == "addresses → Address (normalized.address)"
    )


def test_streaming_runner_renders_clarification_as_message_chunk():
    """A needs_clarification result (empty answer) must be synthesised into a
    message_chunk so the clarification question + options render in the chat
    bubble instead of only appearing in the QueryAnswer tool-call result."""
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
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
        )
    )
    text = ''.join(
        evt.get('delta') or '' for evt in out if evt['type'] == 'message_chunk'
    )
    assert 'Which code did you mean?' in text
    assert 'State Code — normalized.address.state_tc' in text
    assert 'ZIP Code — normalized.address.zip' in text
    # run_finished still terminates the stream (totals path unchanged).
    assert out[-1]['type'] == 'run_finished'


def test_streaming_runner_surfaces_callback_text_chunks():
    """When the callback streams text via the data kwarg, those chunks
    appear before run_finished and the runner does NOT emit a duplicate
    final chunk."""
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        callback(data='hello ')
        callback(data='world')
        return {'answer': 'hello world', 'sql_query': '', 'results': []}

    out = list(
        stream_agent_run(
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
        )
    )
    deltas = [evt.get('delta') for evt in out if evt['type'] == 'message_chunk']
    # Two from the callback + one from the runner's final-answer fallback
    # would be a regression. Assert we only see the two callback chunks.
    assert deltas == ['hello ', 'world']


def test_streaming_runner_surfaces_run_error_on_exception():
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        raise RuntimeError('Bedrock down')

    out = list(
        stream_agent_run(
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
        )
    )
    last = out[-1]
    assert last['type'] == 'run_error'
    assert 'Bedrock down' in last['error']


def test_run_finished_totals_carry_sql_rows_and_kb_sources():
    """``run_finished.totals`` must surface the rich result payload (sql,
    rows, kbSources) so the frontend can render the result panel without
    a second round-trip."""
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
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
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
    """Large result sets must not blow up the SSE frame — the runner caps
    the inline rows payload and flags truncation."""
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
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
        )
    )
    totals = out[-1]['totals']
    assert totals['rowCount'] == 500
    assert len(totals['rows']) == 200
    assert totals['truncated'] is True


def test_streaming_runner_emits_thinking_chunk_on_reasoning_kwargs():
    """Strands surfaces model reasoning via ``reasoning_text`` (or
    ``reasoningContent.text``). The runner must route it to a
    ``thinking_chunk`` event — and must NOT also emit it as a
    ``message_chunk`` (which would mix reasoning into the answer text)."""
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        callback(reasoning_text='I should query admin_codes table.')
        callback(reasoningContent={'text': ' Then count the rows.'})
        return {'answer': 'final', 'sql_query': '', 'results': []}

    out = list(
        stream_agent_run(
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
        )
    )
    thinking_deltas = [evt.get('delta') for evt in out if evt['type'] == 'thinking_chunk']
    msg_deltas = [evt.get('delta') for evt in out if evt['type'] == 'message_chunk']
    assert thinking_deltas == [
        'I should query admin_codes table.',
        ' Then count the rows.',
    ]
    # The reasoning text must not leak into the answer stream. The runner's
    # final-answer fallback (no streamed text) emits 'final' once.
    assert msg_deltas == ['final']


def test_streaming_runner_reclassifies_pre_tool_text_as_thinking():
    """Plain text the model writes BEFORE a tool call is intermediate
    reasoning, not the final answer. The runner must flush that text
    as ``thinking_chunk`` once the first tool fires; only text streamed
    AFTER the last tool stays as ``message_chunk``."""
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        # Pre-tool narrative — should become thinking_chunk.
        callback(data='Let me query the metadata first. ')
        callback(data='I need to look up the table name.')
        # Tool call → triggers the buffer flush.
        callback(toolUse={'toolUseId': 'tu-1', 'name': 'retrieve_kb_context', 'input': {'q': 'metric'}})
        callback(toolResult={'toolUseId': 'tu-1', 'content': [{'json': {'rows': []}}]})
        # Post-tool narrative — final answer text.
        callback(data='The answer is 42.')
        return {'answer': 'The answer is 42.', 'sql_query': '', 'results': []}

    out = list(
        stream_agent_run(
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
        )
    )
    thinking = [evt.get('delta') for evt in out if evt['type'] == 'thinking_chunk']
    msg = [evt.get('delta') for evt in out if evt['type'] == 'message_chunk']
    assert thinking == [
        'Let me query the metadata first. ',
        'I need to look up the table name.',
    ]
    # Only post-tool text should appear as final-answer chunks. The
    # runner must NOT emit a duplicate fallback chunk because the
    # callback already streamed text post-tool.
    assert msg == ['The answer is 42.']


def test_streaming_runner_tool_events_carry_real_args_and_results():
    """The synthetic toolUse path (used by tests) must surface input args
    on tool_call_start and content on tool_call_end so the UI can render
    Arguments / Result sections instead of empty placeholders."""
    emitter = AGUIEmitter(turn_id='t-1')
    sql_args = {'sql': 'SELECT count(*) FROM admin_codes'}
    result_content = [{'json': {'rows': [[42]], 'columns': ['count']}}]

    def run_agent(callback):
        callback(toolUse={'toolUseId': 'tu-1', 'name': 'execute_sql_query', 'input': sql_args})
        callback(toolResult={'toolUseId': 'tu-1', 'content': result_content})
        return {'answer': 'done', 'sql_query': sql_args['sql'], 'results': []}

    out = list(
        stream_agent_run(
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
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
        time.sleep(5)  # nosemgrep: arbitrary-sleep — simulates hung agent to verify timeout path fires run_error
        return {'answer': 'never'}

    out = list(
        stream_agent_run(
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=0.1,
        )
    )
    last = out[-1]
    assert last['type'] == 'run_error'
    assert 'max_wait_seconds' in last['error']


def test_on_result_called_with_run_finished_answer_and_totals():
    """The on_result sink (used by chat entrypoints to persist the assistant
    turn) must receive the SAME answer text + totals that go into run_finished,
    so a reopened chat renders identically to the live stream."""
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
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
            on_result=on_result,
        )
    )
    finished = out[-1]
    assert finished['type'] == 'run_finished'
    # on_result fired exactly once, with the run_finished payload.
    assert captured['answer'] == 'three rows'
    assert captured['totals'] == finished['totals']
    assert captured['totals']['sql'] == 'SELECT a FROM t'
    assert captured['totals']['rowCount'] == 3
    assert captured['totals']['kbSources'] == kb


def test_on_result_failure_does_not_break_stream():
    """A persistence error in on_result must be swallowed — run_finished must
    still terminate the stream (DDB errors can't drop the user's answer)."""
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        return {'answer': 'still finishes', 'sql_query': '', 'results': []}

    def on_result(answer_text, totals):
        raise RuntimeError('DDB write failed')

    out = list(
        stream_agent_run(
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
            on_result=on_result,
        )
    )
    assert out[-1]['type'] == 'run_finished'


def test_on_result_not_called_on_error_path():
    """When the agent raises, there is no assistant answer to persist — the
    on_result sink must NOT fire (only run_error terminates the stream)."""
    emitter = AGUIEmitter(turn_id='t-1')

    def run_agent(callback):
        raise RuntimeError('Bedrock down')

    calls = []

    def on_result(answer_text, totals):
        calls.append((answer_text, totals))

    out = list(
        stream_agent_run(
            emitter=emitter,
            run_agent=run_agent,
            poll_interval=0.005,
            max_wait_seconds=2,
            on_result=on_result,
        )
    )
    assert out[-1]['type'] == 'run_error'
    assert calls == []
