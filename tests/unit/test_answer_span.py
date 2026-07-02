"""Unit tests for the eval-only final-answer span helper.

emit_answer_span exists so the SESSION-level judges grade the agent's REAL final
answer (the clarification question + options, or the NL summary) instead of an
intermediate graph span (the follow-up rewrite, or a SliceSufficiency result) —
and so clarify-only turns that make no model call still emit an evaluable span.
These tests pin the contract without a real OTEL/Strands tracer.
"""
import sys
import types

import pytest

from agents.shared.answer_span import emit_answer_span


class _FakeSpan:
    pass


class _FakeTracer:
    """Records the messages passed to start/end so tests can assert on them.

    Stubs ``start_agent_span`` / ``end_agent_span`` (the ``invoke_agent`` turn-span
    pair the eval service recognizes) — emit_answer_span emits an ``invoke_agent``
    span, NOT a ``chat`` model-invoke span, so a deterministic clarify turn is still a
    recognized turn in the SESSION context. ``end_agent_span`` takes a ``response``
    object whose ``str()`` is the answer text (mirrors the real Tracer).
    """

    def __init__(self):
        self.started = None
        self.ended = None

    def start_agent_span(self, *, messages, agent_name, model_id=None, **kwargs):
        self.started = {"messages": messages, "agent_name": agent_name, "model_id": model_id}
        return _FakeSpan()

    def end_agent_span(self, *, span, response=None, error=None):
        # The real tracer records the assistant output via str(response); mirror that
        # so tests can assert on the answer text the judge will see.
        self.ended = {
            "span": span,
            "message": {"role": "assistant", "content": [{"text": str(response)}]},
            "response": response,
            "error": error,
        }


def _install_fake_tracer(monkeypatch) -> _FakeTracer:
    """Install a fake strands.telemetry.tracer module exposing get_tracer()."""
    tracer = _FakeTracer()
    mod = types.ModuleType("strands.telemetry.tracer")
    mod.get_tracer = lambda: tracer
    monkeypatch.setitem(sys.modules, "strands", types.ModuleType("strands"))
    monkeypatch.setitem(sys.modules, "strands.telemetry",
                        types.ModuleType("strands.telemetry"))
    monkeypatch.setitem(sys.modules, "strands.telemetry.tracer", mod)
    return tracer


def test_emits_final_answer_as_output_message(monkeypatch):
    tracer = _install_fake_tracer(monkeypatch)
    emit_answer_span(question="How many parties are there?",
                     answer="There are 15 parties.")
    # The user question is the input; the real answer is the assistant output.
    assert "How many parties are there?" in tracer.started["messages"][0]["content"][0]["text"]
    # Emitted as an invoke_agent turn span (agent_name carries the operation label).
    assert tracer.started["agent_name"] == "answer:final_answer"
    out = tracer.ended["message"]
    assert out["role"] == "assistant"
    assert out["content"][0]["text"] == "There are 15 parties."
    # Eval-only telemetry — the shim has no .metrics, so no token usage is attached.
    assert not hasattr(tracer.ended["response"], "metrics")


def test_clarification_options_folded_into_output(monkeypatch):
    tracer = _install_fake_tracer(monkeypatch)
    emit_answer_span(
        question="How many are there?",
        answer="Which interpretation of 'there' do you mean?",
        options=[{"id": "party", "label": "party (database: normalized)"},
                 {"id": "rider", "label": "rider (database: normalized)"}],
        operation_label="clarification",
    )
    text = tracer.ended["message"]["content"][0]["text"]
    assert "Which interpretation of 'there' do you mean?" in text
    assert "CLARIFICATION" in text
    assert "party (database: normalized)" in text
    assert "rider (database: normalized)" in text
    assert tracer.started["agent_name"] == "answer:clarification"


def test_no_op_on_empty_answer(monkeypatch):
    tracer = _install_fake_tracer(monkeypatch)
    emit_answer_span(question="q", answer="")
    assert tracer.started is None and tracer.ended is None


def test_empty_options_add_no_marker(monkeypatch):
    tracer = _install_fake_tracer(monkeypatch)
    emit_answer_span(question="q", answer="real answer", options=[])
    text = tracer.ended["message"]["content"][0]["text"]
    assert text == "real answer"
    assert "CLARIFICATION" not in text


def test_fail_soft_when_tracer_unavailable(monkeypatch):
    # get_tracer raising must never propagate — eval telemetry can't break a query.
    mod = types.ModuleType("strands.telemetry.tracer")

    def _boom():
        raise RuntimeError("no tracer")

    mod.get_tracer = _boom
    monkeypatch.setitem(sys.modules, "strands", types.ModuleType("strands"))
    monkeypatch.setitem(sys.modules, "strands.telemetry",
                        types.ModuleType("strands.telemetry"))
    monkeypatch.setitem(sys.modules, "strands.telemetry.tracer", mod)
    # Should not raise.
    emit_answer_span(question="q", answer="a")


def test_conversation_history_folded_into_input(monkeypatch):
    """The final-turn span must carry prior turns so a SESSION judge can score the
    full multi-turn trajectory even if only this span reaches it."""
    tracer = _install_fake_tracer(monkeypatch)
    emit_answer_span(
        question="How many are there? (for party)",
        answer="There are 15 parties.",
        conversation_history=[
            {"role": "user", "content": "How many are there?"},
            {"role": "assistant", "content": "Which interpretation do you mean?"},
            {"role": "user", "content": "party"},
        ],
    )
    in_text = tracer.started["messages"][0]["content"][0]["text"]
    assert "[conversation_so_far]" in in_text
    assert "user: How many are there?" in in_text
    assert "assistant: Which interpretation do you mean?" in in_text
    assert "user: party" in in_text
    # The OUTPUT leads with the real answer (so answer-matching judges anchor on it)
    # and then appends the trajectory recap (so the path judge sees the full
    # multi-turn conversation even when only the OUTPUT reaches it).
    out_text = tracer.ended["message"]["content"][0]["text"]
    assert out_text.startswith("There are 15 parties.")
    assert "[conversation_so_far]" in out_text
    assert "user: How many are there?" in out_text


def test_conversation_history_handles_segmented_content(monkeypatch):
    """content given as a list of {text} segments is flattened, not stringified."""
    tracer = _install_fake_tracer(monkeypatch)
    emit_answer_span(
        question="q", answer="a",
        conversation_history=[{"role": "user", "content": [{"text": "hello"}, {"text": "world"}]}],
    )
    in_text = tracer.started["messages"][0]["content"][0]["text"]
    assert "user: hello world" in in_text


def test_conversation_history_handles_ddb_text_key(monkeypatch):
    """ChatSessionService.history_window returns messages keyed by ``text`` (not
    ``content``); the span must render those, or the multi-turn history silently
    drops and the SESSION judge sees only the final turn (regression: clarify
    scenarios scored 0.0 because the history block was empty)."""
    tracer = _install_fake_tracer(monkeypatch)
    emit_answer_span(
        question="How many are there? (for party)",
        answer="There are 15 parties.",
        conversation_history=[
            {"role": "user", "text": "How many are there?", "turnId": "t0"},
            {"role": "assistant", "text": "Which interpretation do you mean?", "turnId": "t1"},
            {"role": "user", "text": "party", "turnId": "t2"},
        ],
    )
    in_text = tracer.started["messages"][0]["content"][0]["text"]
    assert "[conversation_so_far]" in in_text
    assert "user: How many are there?" in in_text
    assert "assistant: Which interpretation do you mean?" in in_text
    assert "user: party" in in_text


def test_no_history_block_when_history_absent(monkeypatch):
    tracer = _install_fake_tracer(monkeypatch)
    emit_answer_span(question="q", answer="a")
    assert "[conversation_so_far]" not in tracer.started["messages"][0]["content"][0]["text"]


def test_retrieved_schema_folded_into_input(monkeypatch):
    """The schema slice must travel on this turn span so SqlGrounded can verify the
    executed SQL — the invoke_agent answer span now anchors {context} and displaces
    the separate execute_sql_query tool span that used to carry the slice."""
    tracer = _install_fake_tracer(monkeypatch)
    emit_answer_span(
        question="How many parties are there?",
        answer="There are 15 parties.",
        retrieved_schema='{"tables": ["normalized.party"], "columns": ["is_deleted"]}',
    )
    in_text = tracer.started["messages"][0]["content"][0]["text"]
    assert "[retrieved_schema_context]" in in_text
    assert "normalized.party" in in_text
    assert "is_deleted" in in_text


def test_no_schema_block_when_schema_absent(monkeypatch):
    tracer = _install_fake_tracer(monkeypatch)
    emit_answer_span(question="q", answer="a")
    assert "[retrieved_schema_context]" not in tracer.started["messages"][0]["content"][0]["text"]
