"""Hybrid Phase-5 answer verbalization + terminal answer-span emission.

Covers the two halves of the fix for the measured Hybrid GoalSuccess collapse
(nb15, 2026-08-06: ``SqlGrounded`` 0.93 but ``GoalSuccess`` 0.07 — the SQL was
right, the ANSWER was a row-count stub):

1. :class:`BoundedSqlExecutor` verbalizes the real rows through an injected
   ``render_answer`` seam (a bounded LLM in production), and fails SOFT back to
   the deterministic summary so an answer-rendering failure never loses a good
   result.
2. The graph's clarify / degraded TERMINAL nodes call ``deps.answer_emitter``
   from INSIDE the node, while the graph's multiagent span is still the active
   OTEL context. Those terminals make no model call, so without this the turn
   emits no recognized turn span and is dropped from the SESSION ``{context}``.
"""
from agents.semantic_layer_query_agent.execution_agent import BoundedSqlExecutor
from agents.semantic_layer_query_agent.graph import HybridDeps, run_hybrid_workflow


def _ok_query(*, sql, database_name, catalog_id):
    """Stand-in ``run_query`` seam returning one fixed 3-row result.

    Args:
        sql: The bounded SQL (ignored — this stub never executes anything).
        database_name: Athena database (ignored).
        catalog_id: Athena catalog (ignored).

    Returns:
        A parsed-result dict with ``columns`` and 3 ``rows``.
    """
    return {
        "columns": ["party_type", "n"],
        "rows": [["Organization", "7"], ["Trust", "5"], ["Individual", "3"]],
    }


# ---------------------------------------------------------------------------
# 1. BoundedSqlExecutor answer rendering.
# ---------------------------------------------------------------------------
def test_renderer_output_becomes_the_answer():
    # The whole point of the fix: the user-facing answer must carry the ACTUAL
    # values, not "The query returned 3 row(s)."
    seen = {}

    def _render(*, question, columns, rows, over_limit, usage_sink, slice_text):
        seen.update(question=question, columns=columns, rows=rows,
                    over_limit=over_limit, slice_text=slice_text)
        usage_sink["totalTokens"] = 42
        return "Organization (7), Trust (5), and Individual (3)."

    result = BoundedSqlExecutor(
        run_query=_ok_query, render_answer=_render,
    ).execute(
        sql="SELECT party_type, COUNT(id) n FROM party GROUP BY party_type",
        database_name="db", catalog_id="cat",
        question="What party types are there?", slice_text="{slice}",
    )

    assert result["answer"] == "Organization (7), Trust (5), and Individual (3)."
    # The renderer must receive the real question/rows/slice, not placeholders.
    assert seen["question"] == "What party types are there?"
    assert seen["rows"] == result["rows"]
    assert seen["slice_text"] == "{slice}"
    assert seen["over_limit"] is False
    # Renderer token spend is folded into the result's usage, not dropped.
    assert result["usage"]["totalTokens"] == 42


def test_no_renderer_keeps_the_deterministic_summary():
    # Unit tests and any caller that wires no renderer keep the old shape — the
    # executor must stay importable and runnable without Bedrock.
    result = BoundedSqlExecutor(run_query=_ok_query).execute(
        sql="SELECT 1", database_name="db", catalog_id="cat",
    )
    assert result["answer"] == "The query returned 3 row(s)."


def test_renderer_exception_falls_back_to_the_summary():
    # A renderer outage must degrade the WORDING, never lose the rows.
    def _boom(**_kwargs):
        raise RuntimeError("bedrock throttled")

    result = BoundedSqlExecutor(
        run_query=_ok_query, render_answer=_boom,
    ).execute(sql="SELECT 1", database_name="db", catalog_id="cat")

    assert result["answer"] == "The query returned 3 row(s)."
    assert len(result["rows"]) == 3


def test_blank_renderer_output_falls_back_to_the_summary():
    # A model that returns whitespace must not leave the user with an empty
    # answer — and an empty answer would also make emit_answer_span a no-op.
    result = BoundedSqlExecutor(
        run_query=_ok_query, render_answer=lambda **_kw: "   \n ",
    ).execute(sql="SELECT 1", database_name="db", catalog_id="cat")
    assert result["answer"] == "The query returned 3 row(s)."


def test_execution_error_is_not_rendered():
    # A failed query must surface the Athena error verbatim (fail loud) and must
    # NOT spend a model call verbalizing a result that does not exist.
    calls = []

    def _failing(*, sql, database_name, catalog_id):
        """Seam returning an Athena-level error."""
        return {"error": "SCHEMA_NOT_FOUND"}

    result = BoundedSqlExecutor(
        run_query=_failing,
        render_answer=lambda **kw: calls.append(kw) or "should not run",
    ).execute(sql="SELECT 1", database_name="db", catalog_id="cat")

    assert result["answer"] == "Query failed: SCHEMA_NOT_FOUND"
    assert calls == []


# ---------------------------------------------------------------------------
# 2. Terminal answer-span emission.
# ---------------------------------------------------------------------------
_SLICE_TTL = """\
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix vkg: <https://x#> .
<http://x/Address> vkg:mapsToTable "address" .
<http://x/Party>   vkg:mapsToTable "party" .
<http://x/hasStateTc> rdfs:domain <http://x/Address> .
<http://x/hasStateTc> vkg:mapsToColumn "address.state_tc" .
<http://x/hasStateTc> vkg:mapsToColumn "party.state_tc" .
"""


class _Fetch:
    def get(self, *, namespace, candidates):
        """Return the thin-VKG structural slice as an rdflib graph."""
        from rdflib import Graph

        g = Graph()
        g.parse(data=_SLICE_TTL, format="turtle")
        return g


def _deps(*, router, builder, generator_sql, emitted):
    """Build HybridDeps whose answer_emitter records each terminal emission.

    Args:
        router: Phase-1 router stub.
        builder: Phase-3 slice-builder stub.
        generator_sql: The SQL the generator always returns.
        emitted: List the emitter appends each context to.

    Returns:
        A wired :class:`HybridDeps`.
    """
    class _Gen:
        def generate(self, *, slice_text, question, negative_constraint=""):
            """Return the fixed SQL under test."""
            return generator_sql

        last_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}

    return HybridDeps(
        router=router,
        builder=builder,
        generator=_Gen(),
        fetch=_Fetch(),
        run_execution=lambda ctx: {"answer": "ok", "usage": {}},
        vocab="https://x#",
        dialect="athena",
        answer_emitter=emitted.append,
    )


class _Router:
    def find_candidates(self, *, question, namespace):
        """Return one unambiguous candidate table id."""
        return ["db.address"]


class _EmptyRouter:
    def find_candidates(self, *, question, namespace):
        """Return no candidates, driving the ``phase1_empty`` degraded terminal."""
        return []


class _Builder:
    def build(self, *, candidates, namespace, question):
        """Return a trivially sufficient slice."""
        return "{}"

    def is_sufficient(self, *, slice_text, question):
        """Report the slice sufficient so Phase 3 does not expand."""
        return True, None

    def expand(self, *, slice_text, missing):
        """Return the slice unchanged (never reached)."""
        return "{}"


def test_degraded_terminal_emits_one_answer_span():
    # A degraded turn makes NO model call, so without an in-graph emission the
    # turn has no recognized turn span and vanishes from the SESSION context.
    emitted = []
    deps = _deps(router=_EmptyRouter(), builder=_Builder(),
                 generator_sql="SELECT 1", emitted=emitted)
    ctx = run_hybrid_workflow(question="q", namespace="ns", deps=deps)

    assert ctx.degraded == "phase1_empty"
    assert len(emitted) == 1
    assert emitted[0] is ctx


def test_grounded_success_does_not_emit_a_terminal_span():
    # On the grounded path the bounded renderer inside run_execution makes a real
    # model call, so the SDK already emits an in-graph answer span. A second,
    # zero-usage synthetic span would be redundant and could shadow the real one.
    emitted = []
    deps = _deps(router=_Router(), builder=_Builder(),
                 generator_sql="SELECT a.state_tc FROM address a",
                 emitted=emitted)
    ctx = run_hybrid_workflow(question="q", namespace="ns", deps=deps)

    assert ctx.degraded is None
    assert emitted == []


def test_missing_emitter_is_a_no_op():
    # answer_emitter defaults to None (unit tests, and any caller that wants no
    # eval telemetry). A degraded terminal must still complete normally.
    deps = _deps(router=_EmptyRouter(), builder=_Builder(),
                 generator_sql="SELECT 1", emitted=[])
    deps.answer_emitter = None
    ctx = run_hybrid_workflow(question="q", namespace="ns", deps=deps)
    assert ctx.degraded == "phase1_empty"


# ---------------------------------------------------------------------------
# 3. The emitter closure itself: which text/label reaches emit_answer_span.
# ---------------------------------------------------------------------------
class _Ctx:
    """Minimal WorkflowContext stand-in for the emitter closure."""

    def __init__(self, *, degraded=None, needs_clarification=None,
                 execution_result=None, degraded_detail=None):
        """Store the only four fields the emitter closure reads."""
        self.degraded = degraded
        self.needs_clarification = needs_clarification
        self.execution_result = execution_result
        self.degraded_detail = degraded_detail
        self.conversation_history = []


def _capture(monkeypatch):
    """Replace ``main.emit_answer_span`` with a recorder and return the record list.

    Args:
        monkeypatch: pytest's monkeypatch fixture.

    Returns:
        A list that receives one kwargs dict per emit_answer_span call.
    """
    from agents.semantic_layer_query_agent import main

    calls = []
    monkeypatch.setattr(main, "emit_answer_span",
                        lambda **kwargs: calls.append(kwargs))
    return calls


def test_emitter_grades_the_clarification_question_and_options(monkeypatch):
    # On a clarify terminal the judge must see the question the user was asked
    # AND the options offered — the options otherwise live only in the structured
    # payload, invisible to a text judge.
    from agents.semantic_layer_query_agent.main import _make_answer_emitter

    calls = _capture(monkeypatch)
    ctx = _Ctx(needs_clarification={
        "clarification_question": "Which state did you mean?",
        "options": [{"id": "a", "label": "Address state"},
                    {"id": "b", "label": "Party state"}],
    })
    _make_answer_emitter(question="which state?")(ctx)

    assert len(calls) == 1
    assert calls[0]["answer"] == "Which state did you mean?"
    assert calls[0]["operation_label"] == "clarification"
    assert len(calls[0]["options"]) == 2


def test_emitter_grades_the_degraded_message_the_user_received(monkeypatch):
    # The span text must be IDENTICAL to the response text — a span carrying
    # different words than the user got would grade the wrong thing.
    from agents.semantic_layer_query_agent.main import (
        _DEGRADED_MESSAGES, _make_answer_emitter,
    )

    calls = _capture(monkeypatch)
    _make_answer_emitter(question="q")(_Ctx(degraded="phase1_empty"))

    assert calls[0]["answer"] == _DEGRADED_MESSAGES["phase1_empty"]
    assert calls[0]["operation_label"] == "final_answer"
    # A degraded turn executed no SQL, so no SQL/schema may be claimed — that
    # would let the SqlGrounded judge score a query that never ran.
    assert "executed_sql" not in calls[0]
    assert "retrieved_schema" not in calls[0]


def test_degraded_detail_wins_over_the_generic_message(monkeypatch):
    # When the graph recorded a specific reason, the user sees THAT, so the span
    # must too.
    from agents.semantic_layer_query_agent.main import _make_answer_emitter

    calls = _capture(monkeypatch)
    _make_answer_emitter(question="q")(
        _Ctx(degraded="phase3_max_rounds", degraded_detail="missing column foo"),
    )
    assert calls[0]["answer"] == "missing column foo"
