"""AG-UI event emitter for the Hybrid query agent.

Copied-and-owned (per the design's copy-not-import rule) from
``agents/shared/agui_emitter.py``. The emitter buffers plain envelope dicts of
the shape ``{'type': <event_type>, 'turnId': <turn_id>, ...payload}``. The
agent yields those dicts directly from its streaming entrypoint; the
``BedrockAgentCoreApp`` runtime handles SSE framing (``data: {json}\\n\\n``).
The Lambda proxy then re-frames each record as a proper
``event: <type>\\ndata: {json}\\n\\n`` SSE record for the browser.

Why dicts (not pre-formatted SSE strings): the AgentCore SDK's
``_convert_to_sse`` JSON-encodes every yielded value into ``data: <json>\\n\\n``
regardless of whether the agent already SSE-formatted it — yielding dicts is the
only shape that survives the two layers of framing intact.

Isolation note: standard library ONLY — no ``agents.shared`` / sibling-agent
imports (the Hybrid copy-and-own isolation rule).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

# Allow-listed event types (mirrors the design doc taxonomy). Anything outside
# this set is rejected at emit time so a typo in agent code fails loudly rather
# than silently breaking the frontend.
_ALLOWED_EVENTS = frozenset(
    {
        'run_started',
        'tool_call_start',
        'tool_call_end',
        'message_chunk',
        'thinking_chunk',
        'run_finished',
        'run_error',
        'tier_event',
    }
)


class AGUIEmitter:
    """Per-turn event buffer.

    The agent instantiates one emitter for the duration of a single turn and
    calls ``emit`` whenever something interesting happens (run start, tool call,
    message chunk). The AgentCore entrypoint drains the buffer at intervals and
    yields the envelope dicts.
    """

    def __init__(self, *, turn_id: str) -> None:
        """Construct an emitter scoped to a single turn id."""
        if not turn_id:
            raise ValueError("turn_id is required")
        self._turn_id = turn_id
        self._buffer: List[Dict[str, Any]] = []
        # Tool calls keep simple wall-clock duration tracking so the UI can
        # show "Athena query took 2.4 s" without a separate timing channel.
        # ``time.monotonic`` measures the duration delta (immune to clock
        # adjustments); ``time.time`` (epoch ms) is sent as an absolute
        # ``startedAt``/``endedAt`` stamp so the UI can display exactly when
        # each tool ran and make the execution order unambiguous.
        self._tool_started_at: Dict[str, float] = {}

    @property
    def turn_id(self) -> str:
        """Return the turn id this emitter is scoped to."""
        return self._turn_id

    def emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Append one event envelope to the buffer.

        Always stamps ``turnId`` and the event ``type`` into the envelope so the
        frontend can route events to the right transcript bubble even if chunks
        interleave from concurrent retries.

        Args:
            event_type: One of ``_ALLOWED_EVENTS``.
            payload: Extra fields merged into the envelope.
        """
        if event_type not in _ALLOWED_EVENTS:
            raise ValueError(f"unknown AG-UI event type: {event_type!r}")
        envelope: Dict[str, Any] = {
            'type': event_type,
            'turnId': self._turn_id,
        }
        envelope.update(payload)
        self._buffer.append(envelope)

    # ------------------------------------------------------------------
    # Convenience helpers — keep the call-sites in agent code terse.
    # ------------------------------------------------------------------

    def run_started(self, *, agent: str, model: str) -> None:
        """Emit the run-start event carrying the agent + model id."""
        self.emit('run_started', {'agent': agent, 'model': model})

    def tool_call_start(
        self, *, tool_name: str, call_id: str, args: Dict[str, Any]
    ) -> None:
        """Emit a tool-call-start event and record its start time."""
        self._tool_started_at[call_id] = time.monotonic()
        # Absolute epoch-ms stamp so the UI can render a precise start time and
        # order tool cards by when they actually fired.
        started_at_ms = int(time.time() * 1000)
        self.emit(
            'tool_call_start',
            {
                'toolName': tool_name,
                'callId': call_id,
                'args': args,
                'startedAt': started_at_ms,
            },
        )

    def tool_call_end(self, *, call_id: str, result: Any) -> None:
        """Emit a tool-call-end event with the elapsed duration."""
        started = self._tool_started_at.pop(call_id, None)
        duration_ms = (
            int((time.monotonic() - started) * 1000) if started is not None else None
        )
        # Absolute epoch-ms stamp for when the tool returned — pairs with the
        # ``startedAt`` on the matching tool_call_start event.
        ended_at_ms = int(time.time() * 1000)
        self.emit(
            'tool_call_end',
            {
                'callId': call_id,
                'result': result,
                'durationMs': duration_ms,
                'endedAt': ended_at_ms,
            },
        )

    def message_chunk(self, *, delta: str) -> None:
        """Emit a chunk of the final answer text."""
        self.emit('message_chunk', {'delta': delta})

    def thinking_chunk(self, *, delta: str) -> None:
        """Emit a chunk of model reasoning/thinking text.

        Streamed separately from ``message_chunk`` so the UI can render thinking
        in its own collapsible section without mixing it into the final answer.
        """
        self.emit('thinking_chunk', {'delta': delta})

    def run_finished(self, *, message_id: str, totals: Dict[str, Any]) -> None:
        """Emit the terminal run-finished event with the totals payload."""
        self.emit('run_finished', {'messageId': message_id, 'totals': totals})

    def run_error(self, *, error: str, reason: str | None = None) -> None:
        """Emit a terminal error event.

        Args:
            error: Human-readable error/canned-block message.
            reason: Optional machine code (e.g. 'GUARDRAIL_INPUT'); included in
                the envelope only when provided so non-guardrail errors keep the
                original shape.
        """
        payload: Dict[str, Any] = {'error': error}
        if reason:
            payload['reason'] = reason
        self.emit('run_error', payload)

    def emit_tier(self, *, tier: int, phase: int | None, action: str,
                  payload: Dict[str, Any] | None = None) -> None:
        """Emit a progressive-disclosure tier/phase event.

        Args:
            tier: 1 (governed metric) or 2 (hybrid semantic SQL).
            phase: 1-5 inside Tier 2; ``None`` for Tier 1 events.
            action: Discriminator like ``"phase_start"`` / ``"phase_result"``.
            payload: Optional extra context fields merged into the envelope.
        """
        body: Dict[str, Any] = {'tier': tier, 'action': action}
        if phase is not None:
            body['phase'] = phase
        if payload:
            body.update(payload)
        self.emit('tier_event', body)

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    def drain(self) -> List[Dict[str, Any]]:
        """Return the buffered envelope dicts and reset.

        Returning a fresh list (rather than the internal one) keeps the emitter
        safe to reuse if the caller drains midway through a run.
        """
        out, self._buffer = self._buffer, []
        return out

    def __len__(self) -> int:
        """Return the number of buffered (undrained) events."""
        return len(self._buffer)
