"""emit_grounding_span must stamp the executed SQL as an ``execute_sql_query``
tool attribute (gen_ai.tool.name / gen_ai.tool.input) on the deterministic Phase-5
grounding span — so the codeBased ExecutionAccuracy / RewardVES / SoftF1 evaluator
(benchmark-eval-handler._extract_predicted_sql) can extract the VKG agent's predicted
(Ontop-translated) SQL on the SUCCESSFUL path. Regression guard for the null-EX root
cause (2026-07-26): the SQL was previously only in the span's message body, invisible
to the attribute-scanning codeBased evaluator.
"""

from __future__ import annotations

import json

from agents.shared import grounding_span


class _FakeSpan:
    def __init__(self):
        self.attrs = {}

    def set_attribute(self, key, value):
        self.attrs[key] = value


class _FakeTracer:
    def __init__(self):
        self.span = _FakeSpan()
        self.output_message = None

    def start_model_invoke_span(self, *, messages, model_id=None, **kw):
        return self.span

    def end_model_invoke_span(self, *, span, message, **kw):
        self.output_message = message


def _patch(monkeypatch, fake):
    import strands.telemetry.tracer as st
    monkeypatch.setattr(st, "get_tracer", lambda: fake)


def test_grounding_span_stamps_execute_sql_query_tool_attribute(monkeypatch):
    fake = _FakeTracer()
    _patch(monkeypatch, fake)
    grounding_span.emit_grounding_span(
        retrieved_schema="@prefix : <http://x/> . :Customer a owl:Class .",
        executed_sql="SELECT count(*) FROM bird_x.customers",
        question="how many customers?",
    )
    # The codeBased evaluator's Pass 1 matches on these exact attributes.
    assert fake.span.attrs.get("gen_ai.tool.name") == "execute_sql_query"
    assert json.loads(fake.span.attrs["gen_ai.tool.input"])["sql"] == \
        "SELECT count(*) FROM bird_x.customers"
    # The message body still carries the SQL for the LLM SqlGrounded judge.
    body = "".join(
        p.get("text", "") for p in fake.output_message["content"]
    )
    assert "SELECT count(*) FROM bird_x.customers" in body


def test_grounding_span_noop_without_sql(monkeypatch):
    fake = _FakeTracer()
    _patch(monkeypatch, fake)
    # No executed SQL → no span emitted at all (early return), so no attributes.
    grounding_span.emit_grounding_span(
        retrieved_schema="some schema", executed_sql="",
    )
    assert fake.span.attrs == {}, "no SQL → no tool attribute stamped"
