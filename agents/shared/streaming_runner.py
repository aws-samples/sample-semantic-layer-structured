"""Live streaming runner for query agents (item #1 follow-up).

Problem: the single-shot ``_run_query`` path runs the Strands agent
synchronously to completion, which means the AG-UI events are emitted only
*after* the agent finishes. For a real chat UX we want tool_call_start /
tool_call_end events to land in the browser as they happen.

Solution: run the agent in a worker thread; install both
  * a Strands ``HookProvider`` (BeforeToolCallEvent + AfterToolCallEvent)
    for *real* tool args/results — the only path that exposes them
    synchronously, since ``ToolResultEvent`` is not a callback event.
  * a Strands callback handler for streaming text + reasoning deltas.

Both push envelope dicts onto a thread-safe queue; the generator polls
the queue and yields events as the agent emits them.

Text-vs-reasoning reclassification:
  Plain narrative text the model writes *before* announcing a tool call
  is intermediate reasoning. We buffer it via ``_PendingTextBuffer`` and
  flush as ``thinking_chunk`` when the first tool fires; only text after
  the last tool call (or text from a tool-less turn) becomes the visible
  ``message_chunk`` final answer.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterator, Optional

from .agui_emitter import AGUIEmitter
from .streaming_hook import _PendingTextBuffer, _make_streaming_hook

logger = logging.getLogger(__name__)


_SENTINEL: object = object()


def _make_callback(
    emitter: AGUIEmitter,
    evt_queue: "queue.Queue[Any]",
    pending: _PendingTextBuffer,
):
    """Build a Strands callback handler for text + reasoning deltas.

    Tool events (start / end) come in via the StreamingHook now —
    this callback handles only the streaming-text channels:
      - ``data`` (final-answer text)
      - ``reasoningText`` / ``reasoning_text`` / ``reasoningContent.text``
        (extended-thinking output)

    The synthetic ``toolUse=`` / ``toolResult=`` kwargs are kept for
    backwards compat with the unit tests that drive the runner without
    a real Strands agent.
    """
    open_calls: Dict[str, str] = {}

    def _push_drain():
        for line in emitter.drain():
            evt_queue.put(line)

    def callback(**kwargs: Any) -> None:
        try:
            data = kwargs.get('data')
            reasoning_text = kwargs.get('reasoningText')
            if not isinstance(reasoning_text, str) or not reasoning_text:
                reasoning_text = kwargs.get('reasoning_text')
            if not isinstance(reasoning_text, str) or not reasoning_text:
                rc = kwargs.get('reasoningContent') or kwargs.get('reasoning_content')
                if isinstance(rc, dict):
                    rc_text = rc.get('text')
                    if isinstance(rc_text, str) and rc_text:
                        reasoning_text = rc_text

            # Synthetic tool events — only used by unit tests that don't
            # run a real Strands agent (no HookProvider in those tests).
            tool_use = kwargs.get('toolUse') or kwargs.get('tool_use')
            tool_result = kwargs.get('toolResult') or kwargs.get('tool_result')
            if isinstance(tool_use, dict):
                tool_id = str(tool_use.get('toolUseId') or tool_use.get('id') or uuid.uuid4())
                tool_name = str(tool_use.get('name') or 'tool')
                args = tool_use.get('input') or tool_use.get('args') or {}
                if tool_id not in open_calls:
                    call_id = f"{tool_name}-{tool_id[:8]}"
                    open_calls[tool_id] = call_id
                    pending.flush_as_thinking()
                    emitter.tool_call_start(
                        tool_name=tool_name, call_id=call_id, args=args,
                    )
                    _push_drain()
                return
            if isinstance(tool_result, dict):
                tool_id = str(tool_result.get('toolUseId') or tool_result.get('id') or '')
                call_id = open_calls.pop(tool_id, f"tool-{tool_id[:8] or 'x'}")
                emitter.tool_call_end(
                    call_id=call_id, result=tool_result.get('content') or [],
                )
                _push_drain()
                return

            # Reasoning text → always thinking_chunk (model's extended thinking
            # block), regardless of buffer state.
            if isinstance(reasoning_text, str) and reasoning_text:
                emitter.thinking_chunk(delta=reasoning_text)
                _push_drain()
                return

            # Plain ``data`` text — let the pending buffer decide whether
            # this is pre-tool narrative (thinking) or post-tool answer
            # (message_chunk).
            if isinstance(data, str) and data:
                pending.append(delta=data)
                return
        except Exception as exc:  # noqa: BLE001 — never crash the agent
            logger.warning("AG-UI callback handler error: %s", exc)

    callback.open_calls = open_calls  # type: ignore[attr-defined]
    return callback


def make_phase_sink(emitter: AGUIEmitter, evt_queue: "queue.Queue[Any]"):
    """Build a live per-phase trace sink for the Tier 2 graph workflow.

    The returned callable ``(phase, action, payload) -> None`` emits a
    ``tier_event`` on the emitter and **immediately** push-drains it onto the
    live ``evt_queue`` — so a phase event reaches the SSE stream the moment a
    node fires, rather than sitting in the emitter buffer until end-of-run
    (the live-flush gotcha). Lives next to ``_make_callback``'s ``_push_drain``
    so the queue-wiring stays in one place.

    The sink also accumulates a ``phases`` list (same shape as
    ``useChatStream``'s ``streamingPhases``) so the runner can include
    ``phaseTimeline`` in the ``totals`` payload passed to ``on_result``.
    This lets a reloaded session render the phase trace identically to a
    live stream (previously only ``rowCount`` was persisted, losing the
    result table on reload).

    Args:
        emitter: The per-turn AGUIEmitter.
        evt_queue: The runner's live event queue drained by the yield loop.
    """
    # Mirror the frontend's phaseIndex/phases accumulation from useChatStream.
    _phases: list = []
    _phase_index: Dict[str, int] = {}

    def _phase_key(phase: int, step: Any, round_: int) -> str:
        return f"{phase}:{step or ''}:{round_ or 1}"

    def phase_sink(phase, action, payload):
        """Emit one tier_event (tier=2), flush it, and accumulate for persistence."""
        try:
            emitter.emit_tier(tier=2, phase=phase, action=action, payload=payload)
            for line in emitter.drain():
                evt_queue.put(line)
        except Exception as exc:  # noqa: BLE001 — tracing must never break the run
            logger.warning("phase_sink emit failed (non-fatal): %s", exc)

        # Accumulate phase rows so stream_agent_run can persist phaseTimeline.
        try:
            step = payload.get("step")
            round_ = payload.get("round") or payload.get("groundingRound") or 1
            key = _phase_key(phase, step, round_)
            if action == "phase_start":
                if key not in _phase_index:
                    _phase_index[key] = len(_phases)
                    _phases.append({
                        "phase": phase,
                        "step": step,
                        "round": round_,
                        "status": "running",
                        "startedAt": payload.get("startedAt"),
                    })
            elif action == "phase_result":
                result_payload = {k: v for k, v in payload.items()
                                  if k not in ("phase", "action", "step", "round",
                                               "groundingRound", "startedAt", "endedAt")}
                if key in _phase_index:
                    idx = _phase_index[key]
                    _phases[idx] = {**_phases[idx], "status": "success",
                                    "endedAt": payload.get("endedAt"),
                                    "result": result_payload}
                else:
                    _phase_index[key] = len(_phases)
                    _phases.append({"phase": phase, "step": step, "round": round_,
                                    "status": "success",
                                    "endedAt": payload.get("endedAt"),
                                    "result": result_payload})
        except Exception as exc:  # noqa: BLE001 — accumulation must never break the run
            logger.warning("phase_sink accumulate failed (non-fatal): %s", exc)

    phase_sink.phases = _phases  # type: ignore[attr-defined]
    return phase_sink


def stream_agent_run(
    *,
    emitter: AGUIEmitter,
    run_agent: Callable[..., Dict[str, Any]],
    poll_interval: float = 0.05,
    max_wait_seconds: float = 600.0,
    on_result: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Iterator[Dict[str, Any]]:
    """Run ``run_agent`` in a worker thread and yield AG-UI envelope dicts live.

    Args:
        emitter: AGUIEmitter scoped to the current turn.
        run_agent: Callable invoked in a worker thread. Called with
            ``run_agent(callback)`` for backwards compat with tests, OR
            ``run_agent(callback, hook=...)`` when the agent factory can
            wire a Strands HookProvider for real tool args/results.
        poll_interval: Seconds to wait on each queue.get; tuning knob.
        max_wait_seconds: Hard cap so a hung agent can't block a Lambda.
        on_result: Optional ``(answer_text, totals)`` callback invoked once,
            immediately BEFORE the terminal ``run_finished`` event, with the
            exact same ``answer_text`` and ``totals`` that go into it. The chat
            entrypoints use this to persist the assistant turn so a reload
            renders identically to the live stream. Not called on the error /
            timeout paths (there is no assistant answer to persist). Exceptions
            raised by the callback are caught and logged — persistence must
            never break the stream.

    Yields:
        Envelope dicts (``{'type', 'turnId', ...}``). The final yielded
        envelope is always a ``run_finished`` or ``run_error`` event.
    """
    evt_queue: "queue.Queue[Any]" = queue.Queue()
    result_box: Dict[str, Any] = {}
    error_box: Dict[str, BaseException] = {}
    chunk_state: Dict[str, bool] = {'streamed_text': False}

    pending = _PendingTextBuffer(
        emitter=emitter, evt_queue=evt_queue, chunk_state=chunk_state,
    )
    callback = _make_callback(emitter, evt_queue, pending)
    streaming_hook = _make_streaming_hook(
        emitter=emitter, evt_queue=evt_queue, pending=pending,
    )
    phase_sink = make_phase_sink(emitter, evt_queue)

    def _worker():
        try:
            # Pass the hook + phase sink by kwarg — the agent factory can choose
            # to ignore them (eg. tests that build no real Strands agent).
            try:
                result_box['result'] = run_agent(
                    callback, hook=streaming_hook, phase_sink=phase_sink,
                )
            except TypeError:
                # Older run_agent signatures only accept (callback[, hook]).
                try:
                    result_box['result'] = run_agent(callback, hook=streaming_hook)
                except TypeError:
                    result_box['result'] = run_agent(callback)
        except BaseException as exc:  # noqa: BLE001 — surface to main thread
            error_box['error'] = exc
        finally:
            evt_queue.put(_SENTINEL)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    deadline = time.monotonic() + max_wait_seconds
    while True:
        if time.monotonic() > deadline:
            emitter.run_error(error='agent run exceeded max_wait_seconds')
            for line in emitter.drain():
                yield line
            return
        try:
            item = evt_queue.get(timeout=poll_interval)
        except queue.Empty:
            continue
        if item is _SENTINEL:
            break
        yield item

    # Worker finished — surface error or final result.
    if error_box:
        emitter.run_error(error=f'agent failed: {error_box["error"]}')
        for line in emitter.drain():
            yield line
        return

    result = result_box.get('result') or {}
    answer_text = (
        result.get('answer') or '' if isinstance(result, dict) else str(result)
    )

    # Clarification turns return an empty ``answer`` plus a
    # ``clarification_question`` + ``options`` (see ontology/metadata query
    # agents' needs_clarification branch). Synthesise that into displayable
    # answer text so it streams into the chat bubble instead of being buried
    # in the QueryAnswer tool-call result.
    if isinstance(result, dict) and not answer_text and result.get('needs_clarification'):
        question = result.get('clarification_question') or 'Could you clarify your request?'
        # When the turn carries structured ``clarification.options``, the frontend
        # renders those as clickable chips (ClarificationOptions) and OWNS the
        # option display — so the bubble text is just the question, with no
        # duplicate markdown bullet list. Only fall back to text bullets when no
        # structured options will be rendered (e.g. older/other payload shapes).
        chips_will_render = bool(
            (result.get('clarification') or {}).get('options')
        )
        parts = [question]
        options = result.get('options') or []
        if not chips_will_render and isinstance(options, list) and options:
            parts.append('')  # blank line before the list
            for opt in options:
                if isinstance(opt, dict):
                    label = opt.get('label') or opt.get('id') or ''
                    if label:
                        parts.append(f"- {label}")
                elif opt:
                    parts.append(f"- {opt}")
        answer_text = '\n'.join(parts)

    # End-of-run: whatever text remains in the buffer is the final answer.
    # (Text leading up to each tool was already drained as thinking_chunk
    # by the StreamingHook on every BeforeToolCallEvent.)
    if pending.chunks:
        pending.flush_as_message()
        # drain envelopes the buffer just pushed onto the queue
        while True:
            try:
                yield evt_queue.get_nowait()
            except queue.Empty:
                break

    streamed_text = bool(chunk_state.get('streamed_text'))
    if answer_text and not streamed_text:
        # Callback never streamed text and the buffer was empty (eg. tests
        # that just return a result dict). Emit the answer as one chunk.
        emitter.message_chunk(delta=answer_text)
        for line in emitter.drain():
            yield line

    # Flush any tool_call_start events that never received a matching end.
    # This catches mid-stream errors where AfterToolCallEvent didn't fire.
    open_calls: Dict[str, str] = getattr(callback, 'open_calls', {}) or {}
    if streaming_hook is not None:
        open_calls = {**open_calls, **getattr(streaming_hook, 'open_calls', {})}
    for tool_id, call_id in list(open_calls.items()):
        emitter.tool_call_end(call_id=call_id, result={})
        for line in emitter.drain():
            yield line

    sql_query = result.get('sql_query', '') if isinstance(result, dict) else ''
    rows = result.get('results', []) or [] if isinstance(result, dict) else []
    n_quads = result.get('n_quads', []) or [] if isinstance(result, dict) else []
    metadata = result.get('metadata', {}) if isinstance(result, dict) else {}
    reasoning = result.get('reasoning', {}) if isinstance(result, dict) else {}
    # Human-readable term → Class (table) mapping summary the VKG agent builds
    # in _run_query (result['reasoning']['graphTraversal']). The chat UI renders
    # this as readable chips (e.g. "addresses → Address (normalized.address)")
    # instead of the raw n-quad sub-graph. Fall back to top-level for safety.
    graph_traversal = (
        (reasoning.get('graphTraversal') if isinstance(reasoning, dict) else '')
        or (result.get('graphTraversal', '') if isinstance(result, dict) else '')
    )
    # Cap row payload so a 50k-row result doesn't blow up the SSE frame.
    _ROW_CAP = 200
    truncated = len(rows) > _ROW_CAP
    # Include the accumulated phase timeline so a reloaded session renders
    # the Tier 2 reasoning trace (and Phase 5 result table) identically to
    # the live stream. phase_sink.phases is the same shape as useChatStream's
    # streamingPhases / phaseTimeline so no frontend mapping is needed.
    phase_timeline = list(getattr(phase_sink, 'phases', []))
    # Answer-source label so a live-streamed turn renders the same per-tier
    # trust badge as the fallback path. This is the DEFAULT chat UX
    # (ENABLE_LIVE_STREAMING=true), so omitting it here would drop the badge on
    # nearly every turn. ``result`` is the agent's return dict (Tier 1/2/VKG).
    provenance = result.get('provenance') if isinstance(result, dict) else None
    totals = {
        'sql': sql_query,
        'rowCount': len(rows),
        'rows': rows[:_ROW_CAP],
        'truncated': truncated,
        'kbSources': n_quads,
        'graphTraversal': graph_traversal,
        'usage': metadata.get('usage') or {},
        'runtimeMs': metadata.get('runtimeMs') or 0,
        'phaseTimeline': phase_timeline,
        'provenance': provenance,
    }
    # Carry the pending-clarification record (the standalone question + offered
    # options) into the persisted totals when this turn asked a clarification,
    # so the NEXT turn's _run_query can resolve the user's selection and re-run
    # the original query instead of re-firing the identical clarification.
    if isinstance(result, dict) and result.get('clarification'):
        totals['clarification'] = result['clarification']

    # Hand the final answer + totals back to the caller (chat entrypoints
    # persist the assistant turn here) BEFORE emitting run_finished, using the
    # exact same payload the stream is about to send so a reload renders
    # identically. Fail-soft: a persistence error must never break the stream.
    if on_result is not None:
        try:
            on_result(answer_text, totals)
        except Exception as exc:  # noqa: BLE001 — persistence must not break the run
            logger.warning("stream_agent_run on_result callback failed (non-fatal): %s", exc)

    emitter.run_finished(message_id=f"m-{emitter.turn_id}", totals=totals)
    for line in emitter.drain():
        yield line
