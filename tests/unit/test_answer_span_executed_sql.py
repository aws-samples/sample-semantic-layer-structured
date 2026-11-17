"""emit_answer_span must attach the executed SQL as an execute_sql_query tool
attribute on the exported invoke_agent span, so the codeBased exec-metric Lambda
can extract + score the VKG agent's predicted (Ontop-translated) SQL.
"""

from __future__ import annotations

import json

from agents.shared import answer_span


class _FakeSpan:
    pass


class _FakeTracer:
    def __init__(self):
        self.custom = None

    def start_agent_span(self, *, messages, agent_name, model_id=None,
                         custom_trace_attributes=None, **kw):
        self.custom = custom_trace_attributes
        return _FakeSpan()

    def end_agent_span(self, *, span, response, **kw):
        pass


def _patch(monkeypatch, fake):
    import strands.telemetry.tracer as st
    monkeypatch.setattr(st, "get_tracer", lambda: fake)


def test_executed_sql_becomes_tool_attribute(monkeypatch):
    fake = _FakeTracer()
    _patch(monkeypatch, fake)
    answer_span.emit_answer_span(
        question="how many customers?", answer="42 customers.",
        executed_sql="SELECT count(*) FROM customers",
    )
    assert fake.custom is not None, "executed_sql must set custom_trace_attributes"
    assert fake.custom["gen_ai.tool.name"] == "execute_sql_query"
    assert json.loads(fake.custom["gen_ai.tool.input"])["sql"] == \
        "SELECT count(*) FROM customers"


def test_no_executed_sql_leaves_attrs_none(monkeypatch):
    fake = _FakeTracer()
    _patch(monkeypatch, fake)
    answer_span.emit_answer_span(question="hi", answer="clarify?")
    assert fake.custom is None, "no SQL → no tool attributes"
