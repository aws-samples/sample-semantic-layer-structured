"""Strands ``HookProvider`` that turns BeforeToolCallEvent / AfterToolCallEvent
into AG-UI ``tool_call_start`` / ``tool_call_end`` envelopes — with the *real*
tool arguments and results, not the empty/synthetic shapes the old
callback-based path produced.

Why a hook (not the callback handler):
  - ``BeforeToolCallEvent`` exposes ``event.tool_use['input']`` synchronously
    once Strands has finished parsing the model's tool-use JSON, so the
    UI sees the full args (e.g. ``{"sql": "SELECT ..."}``) on tool start.
  - ``AfterToolCallEvent`` exposes ``event.result`` (a Strands ToolResult
    dict) which carries the tool's return content. The Strands callback
    handler does NOT receive tool results — ``ToolResultEvent.is_callback_event``
    is False — so we cannot get them through the callback path at all.

The hook also coordinates with the runner's ``_PendingTextBuffer`` so any
narrative text the model emitted *before* announcing a tool call is
reclassified as ``thinking_chunk`` rather than ``message_chunk``. That keeps
intermediate reasoning out of the final-answer area in the UI.
"""

from __future__ import annotations

import logging
import queue
import uuid
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:  # avoid hard import at module load
    from .agui_emitter import AGUIEmitter

logger = logging.getLogger(__name__)


class _PendingTextBuffer:
    """Buffers plain text deltas so the runner can split intermediate
    narrative (→ thinking_chunk) from the final answer (→ message_chunk).

    Strategy: always buffer. Every tool_call_start triggers a flush of
    accumulated text as ``thinking_chunk`` (that text was the model
    explaining what it's about to do). Only the buffer remaining at
    end-of-run is the final answer.

    Why not stream post-tool text directly: when the model fires multiple
    tools, the narrative *between* tools is also intermediate reasoning
    ("I got X, now let me check Y") and should not appear in the final
    answer area. We don't know whether another tool is coming, so every
    chunk is buffered until it's either (a) flushed as thinking on the
    next tool, or (b) flushed as message at end-of-run.
    """

    def __init__(
        self,
        *,
        emitter: "AGUIEmitter",
        evt_queue: "queue.Queue[Any]",
        chunk_state: Dict[str, bool],
    ) -> None:
        self.emitter = emitter
        self.evt_queue = evt_queue
        self.chunk_state = chunk_state
        self.chunks: list[str] = []
        # Tracks whether any tool has fired during this run — used by the
        # runner's end-of-run logic to decide whether to emit the
        # "no streamed text" fallback (single message_chunk from the
        # final answer string).
        self.tool_seen: bool = False

    def append(self, *, delta: str) -> None:
        """Buffer a text delta from the model. Always buffered — never
        streamed directly — because we can't tell yet whether more tool
        calls are coming. Tool starts and end-of-run drain the buffer."""
        if not delta:
            return
        self.chunks.append(delta)

    def flush_as_thinking(self) -> None:
        """Convert any buffered narrative into ``thinking_chunk`` envelopes.

        Called by the StreamingHook on every ``BeforeToolCallEvent`` —
        text the model wrote leading up to this tool call is intermediate
        reasoning, not the final answer.
        """
        for chunk in self.chunks:
            self.emitter.thinking_chunk(delta=chunk)
        self.chunks.clear()
        self.tool_seen = True
        self._drain()

    def flush_as_message(self) -> None:
        """End-of-run flush — whatever's left after the last tool is the
        final answer."""
        for chunk in self.chunks:
            self.emitter.message_chunk(delta=chunk)
            self.chunk_state['streamed_text'] = True
        self.chunks.clear()
        self._drain()

    def _drain(self) -> None:
        for line in self.emitter.drain():
            self.evt_queue.put(line)


def _make_streaming_hook(
    *,
    emitter: "AGUIEmitter",
    evt_queue: "queue.Queue[Any]",
    pending: _PendingTextBuffer,
):
    """Construct a Strands HookProvider lazily.

    Importing ``strands.hooks`` at module top would force-require the
    Strands SDK in test environments that don't have it installed; the
    runner only needs a hook when the SDK is present anyway.
    """
    try:
        from strands.hooks import HookProvider  # type: ignore
    except ImportError:  # pragma: no cover — Strands not installed
        return None

    class StreamingHook(HookProvider):  # type: ignore[misc]
        """Subscribes to BeforeToolCallEvent + AfterToolCallEvent and pushes
        AG-UI envelopes onto the runner's event queue."""

        def __init__(self) -> None:
            # Strands tool ids → emitter call ids so end events line up
            # with their starts (multiple tools may be in flight, eg. a
            # parallel tool call burst).
            self.open_calls: Dict[str, str] = {}

        # ------------------------------------------------------------------
        # Strands HookProvider contract
        # ------------------------------------------------------------------
        def register_hooks(self, registry: Any) -> None:
            """Register before/after tool callbacks."""
            from strands.hooks import (  # type: ignore
                BeforeToolCallEvent,
                AfterToolCallEvent,
            )

            registry.add_callback(BeforeToolCallEvent, self.on_before_tool)
            registry.add_callback(AfterToolCallEvent, self.on_after_tool)

        # ------------------------------------------------------------------
        # Event handlers
        # ------------------------------------------------------------------
        def on_before_tool(self, event: Any) -> None:
            """Emit ``tool_call_start`` with the parsed input args."""
            try:
                tool_use = getattr(event, 'tool_use', None) or {}
                tool_id = str(tool_use.get('toolUseId') or uuid.uuid4())
                tool_name = str(tool_use.get('name') or 'tool')
                # Strands names the parsed args ``input``; fall back to
                # ``args`` if any future version renames it.
                args = tool_use.get('input') or tool_use.get('args') or {}

                call_id = f"{tool_name}-{tool_id[:8]}"
                self.open_calls[tool_id] = call_id

                # Reclassify any pre-tool narrative as thinking before
                # announcing the new tool call — keeps the UI's final
                # answer panel free of intermediate reasoning text.
                pending.flush_as_thinking()

                emitter.tool_call_start(
                    tool_name=tool_name, call_id=call_id, args=args,
                )
                for line in emitter.drain():
                    evt_queue.put(line)
            except Exception as exc:  # noqa: BLE001 — never crash the agent
                logger.warning("StreamingHook on_before_tool error: %s", exc)

        def on_after_tool(self, event: Any) -> None:
            """Emit ``tool_call_end`` with the tool's actual return content."""
            try:
                tool_use = getattr(event, 'tool_use', None) or {}
                tool_id = str(tool_use.get('toolUseId') or '')
                result = getattr(event, 'result', None) or {}
                # ToolResult shape: {'toolUseId', 'status', 'content': [...]}
                if isinstance(result, dict):
                    result_content: Any = result.get('content') or []
                else:
                    result_content = result

                call_id = self.open_calls.pop(
                    tool_id, f"tool-{tool_id[:8] if tool_id else 'x'}"
                )
                emitter.tool_call_end(
                    call_id=call_id, result=result_content,
                )
                for line in emitter.drain():
                    evt_queue.put(line)
            except Exception as exc:  # noqa: BLE001 — never crash the agent
                logger.warning("StreamingHook on_after_tool error: %s", exc)

    return StreamingHook()


__all__ = ['_PendingTextBuffer', '_make_streaming_hook']
