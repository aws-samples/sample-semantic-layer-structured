"""Unit tests for the AG-UI event emitter shared by query agents."""

from __future__ import annotations

import os
import sys
import time

import pytest

# Make the agents tree importable.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'agents'),
)

from shared.agui_emitter import AGUIEmitter  # noqa: E402


def test_emitter_requires_turn_id() -> None:
    with pytest.raises(ValueError):
        AGUIEmitter(turn_id='')


def test_run_started_event_carries_turn_id_and_agent() -> None:
    emitter = AGUIEmitter(turn_id='t-1')
    emitter.run_started(agent='ontology_query', model='claude-sonnet-4-6')
    drained = emitter.drain()
    assert len(drained) == 1
    assert drained[0] == {
        'type': 'run_started',
        'turnId': 't-1',
        'agent': 'ontology_query',
        'model': 'claude-sonnet-4-6',
    }


def test_tool_call_start_and_end_record_duration() -> None:
    emitter = AGUIEmitter(turn_id='t-1')
    emitter.tool_call_start(
        tool_name='execute_athena_query',
        call_id='c1',
        args={'sql': 'SELECT 1'},
    )
    time.sleep(0.01)  # nosemgrep: arbitrary-sleep — ensures measurable durationMs between start/end events
    emitter.tool_call_end(call_id='c1', result={'rows': 1})
    events = emitter.drain()
    assert events[0]['type'] == 'tool_call_start'
    assert events[1]['type'] == 'tool_call_end'
    assert events[1]['durationMs'] is not None
    assert events[1]['durationMs'] >= 0


def test_tool_call_events_carry_absolute_timestamps() -> None:
    """tool_call_start carries startedAt and tool_call_end carries endedAt
    as epoch-ms wall-clock stamps, with endedAt >= startedAt — these let the
    UI display when each tool ran and order cards by actual execution time."""
    emitter = AGUIEmitter(turn_id='t-1')
    emitter.tool_call_start(
        tool_name='execute_athena_query',
        call_id='c1',
        args={'sql': 'SELECT 1'},
    )
    time.sleep(0.01)  # nosemgrep: arbitrary-sleep — ensures endedAt > startedAt in epoch-ms timestamps
    emitter.tool_call_end(call_id='c1', result={'rows': 1})
    start_evt, end_evt = emitter.drain()

    assert isinstance(start_evt['startedAt'], int)
    assert isinstance(end_evt['endedAt'], int)
    # Both are epoch-ms, so they sit well past year-2001 (1e12 ms).
    assert start_evt['startedAt'] > 1_000_000_000_000
    assert end_evt['endedAt'] >= start_evt['startedAt']


def test_message_chunk_emits_delta() -> None:
    emitter = AGUIEmitter(turn_id='t-1')
    emitter.message_chunk(delta='hello ')
    emitter.message_chunk(delta='world')
    events = emitter.drain()
    assert [e['delta'] for e in events] == ['hello ', 'world']


def test_run_finished_event() -> None:
    emitter = AGUIEmitter(turn_id='t-1')
    emitter.run_finished(message_id='m-1', totals={'inputTokens': 100})
    payload = emitter.drain()[0]
    assert payload['messageId'] == 'm-1'
    assert payload['totals'] == {'inputTokens': 100}


def test_run_error_event() -> None:
    emitter = AGUIEmitter(turn_id='t-1')
    emitter.run_error(error='boom')
    payload = emitter.drain()[0]
    assert payload == {'type': 'run_error', 'turnId': 't-1', 'error': 'boom'}


def test_unknown_event_type_rejected() -> None:
    emitter = AGUIEmitter(turn_id='t-1')
    with pytest.raises(ValueError):
        emitter.emit('not_a_real_event', {})


def test_drain_resets_buffer() -> None:
    emitter = AGUIEmitter(turn_id='t-1')
    emitter.run_started(agent='a', model='m')
    assert len(emitter) == 1
    assert len(emitter.drain()) == 1
    assert len(emitter) == 0
    assert emitter.drain() == []


def test_run_error_includes_reason_when_given():
    em = AGUIEmitter(turn_id='t1')
    em.run_error(error='blocked msg', reason='GUARDRAIL_INPUT')
    evt = em.drain()[0]
    assert evt['type'] == 'run_error'
    assert evt['turnId'] == 't1'
    assert evt['error'] == 'blocked msg'
    assert evt['reason'] == 'GUARDRAIL_INPUT'


def test_run_error_omits_reason_when_none():
    em = AGUIEmitter(turn_id='t1')
    em.run_error(error='generic failure')
    evt = em.drain()[0]
    assert 'reason' not in evt
