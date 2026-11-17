"""Unit tests for the Tier 1 pre-check → Tier 2 graph-workflow cascade.

The old 3-tier cascade (Tier 2 slice/query → Tier 3 supervised worker) was
replaced by a single Strands graph workflow (Phase 1→5). These tests assert:
  * Tier 1 still short-circuits on a governed-metric hit.
  * A Tier 1 miss routes into the Tier 2 workflow (``tier2_resolve``).
  * The workflow's degraded paths and clarification produce a graceful answer
    rather than a 5xx.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from agents.metadata_query_agent import main
from agents.metadata_query_agent.tier2.workflow import WorkflowContext
from agents.shared.metric_lookup import MetricMatch


def _stub_metadata(monkeypatch):
    monkeypatch.setattr(main, "get_latest_metadata_item",
                        lambda _id: {"name": "demo", "namespace": "ns-default",
                                     "kbId": "kb-1", "version": "v1"})


def test_tier1_short_circuits_when_metric_matches(monkeypatch):
    _stub_metadata(monkeypatch)
    fake_metric = MagicMock(metric_id="active_policies", supported_filters=[],
                            supported_dimensions=[], compiled_sql="SELECT 1",
                            dialect="athena")
    monkeypatch.setattr(
        main, "tier1_lookup",
        lambda **_: MetricMatch(matched=fake_metric, matched_score=0.91),
    )
    monkeypatch.setattr(main, "tier1_execute",
                        lambda **_: {"columns": ["x"], "rows": [{"x": 1}],
                                     "metric_id": "active_policies"})
    spy = MagicMock()
    monkeypatch.setattr(main, "tier2_resolve", spy)

    out = main._run_query({"question": "active policies", "id": "ns-1"})

    spy.assert_not_called()
    assert "active_policies" in str(out)


def test_tier1_response_has_real_answer_sql_and_positional_rows(monkeypatch):
    """Regression: the governed-metric (Tier 1) response must (a) give a real NL
    answer from the rows — NOT a bare 'returned N rows across M columns', (b)
    surface the compiled SQL, and (c) return POSITIONAL rows + columns so the
    chat UI renders the SQL + results table the same as the Tier 2 path."""
    _stub_metadata(monkeypatch)
    fake_metric = MagicMock(metric_id="cash_value_per_policy", supported_filters=[],
                            supported_dimensions=[],
                            compiled_sql="SELECT policy_id, cash_value FROM normalized.holding",
                            dialect="athena")
    monkeypatch.setattr(
        main, "tier1_lookup",
        lambda **_: MetricMatch(matched=fake_metric, matched_score=0.91),
    )
    # execute_metric returns DICT rows; the UI needs positional lists.
    monkeypatch.setattr(main, "tier1_execute",
                        lambda **_: {"columns": ["policy_id", "cash_value"],
                                     "rows": [{"policy_id": "POL001", "cash_value": "1200"},
                                              {"policy_id": "POL002", "cash_value": "3400"}],
                                     "metric_id": "cash_value_per_policy"})
    monkeypatch.setattr(main, "tier2_resolve", MagicMock())

    out = main._run_query({"question": "Cash Value per policy", "id": "ns-1"})

    # (a) real answer, not the bare count
    assert "returned 2 row(s) across" not in out["answer"]
    assert "cash_value_per_policy" in out["answer"]
    # (b) SQL surfaced (top-level + reasoning)
    assert out["sql_query"].startswith("SELECT policy_id, cash_value")
    assert out["reasoning"]["sqlQuery"].startswith("SELECT")
    # (c) positional rows + columns for the UI table (NOT dicts)
    assert out["columns"] == ["policy_id", "cash_value"]
    assert out["results"] == [["POL001", "1200"], ["POL002", "3400"]]


def test_tier1_emits_phase_event_so_ui_renders_sql_and_results(monkeypatch):
    """Regression: the chat UI renders the SQL + results table ONLY inside the
    reasoning panel, which needs a phase/tool event. Tier 1 short-circuits before
    the Tier 2 graph (the only other phase source), so it must emit its own phase
    event carrying the SQL + columns + rows — else the governed-metric answer
    shows no SQL and no results (the reported bug)."""
    _stub_metadata(monkeypatch)
    fake_metric = MagicMock(metric_id="cash_value_per_policy", supported_filters=[],
                            supported_dimensions=[],
                            compiled_sql="SELECT policy_id, cash_value FROM x",
                            dialect="athena")
    monkeypatch.setattr(
        main, "tier1_lookup",
        lambda **_: MetricMatch(matched=fake_metric, matched_score=0.91),
    )
    monkeypatch.setattr(main, "tier1_execute",
                        lambda **_: {"columns": ["policy_id", "cash_value"],
                                     "rows": [{"policy_id": "POL001", "cash_value": "1200"}],
                                     "metric_id": "cash_value_per_policy"})
    monkeypatch.setattr(main, "tier2_resolve", MagicMock())
    # Install a phase sink like the live streaming path does.
    events = []
    monkeypatch.setattr(main, "_STREAMING_PHASE_SINK",
                        lambda phase, action, payload: events.append((phase, action, payload)))

    main._run_query({"question": "Cash Value per policy", "id": "ns-1"})

    # A phase_start + phase_result were emitted; the result carries SQL + the
    # positional rows/columns the PhaseTimeline results table renders.
    actions = [(p, a) for (p, a, _) in events]
    assert (5, "phase_start") in actions
    results = [pl for (p, a, pl) in events if a == "phase_result"]
    assert results, "no phase_result emitted"
    r = results[0]
    assert r["sql_query"].startswith("SELECT")
    assert r["columns"] == ["policy_id", "cash_value"]
    assert r["rows"] == [["POL001", "1200"]]
    assert r["rowCount"] == 1


def test_tier1_scalar_metric_answers_with_value(monkeypatch):
    """A 1x1 governed-metric result answers with the value ('The result is N')."""
    _stub_metadata(monkeypatch)
    fake_metric = MagicMock(metric_id="total_policies", supported_filters=[],
                            supported_dimensions=[], compiled_sql="SELECT COUNT(*) FROM x",
                            dialect="athena")
    monkeypatch.setattr(
        main, "tier1_lookup",
        lambda **_: MetricMatch(matched=fake_metric, matched_score=0.91),
    )
    monkeypatch.setattr(main, "tier1_execute",
                        lambda **_: {"columns": ["n"], "rows": [{"n": "42"}],
                                     "metric_id": "total_policies"})
    monkeypatch.setattr(main, "tier2_resolve", MagicMock())

    out = main._run_query({"question": "how many policies", "id": "ns-1"})
    assert out["answer"] == "The result is 42."
    assert out["results"] == [["42"]]


def test_tier1_miss_routes_into_tier2_workflow(monkeypatch):
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: MetricMatch())
    wf = WorkflowContext(question="q", namespace="ns", kb_id="kb-1",
                         candidates=["db.customers"], sql="SELECT 1",
                         execution_result={"columns": ["c"], "rows": [["1"]],
                                           "answer": "1 row"})
    spy = MagicMock(return_value=wf)
    monkeypatch.setattr(main, "tier2_resolve", spy)

    out = main._run_query({"question": "list customers", "id": "ns-1"})

    spy.assert_called_once()
    assert out["sql_query"] == "SELECT 1"
    assert out["results"] == [{"c": "1"}]


def test_tier2_clarification_short_circuits(monkeypatch):
    """Phase 2 / 3b clarification → needs_clarification payload, no 5xx."""
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: MetricMatch())
    wf = WorkflowContext(question="q", namespace="ns", kb_id="kb-1",
                         candidates=["db.a", "db.b"],
                         needs_clarification={
                             "needs_clarification": True,
                             "clarification_question": "Which one?",
                             "options": [{"id": "a", "label": "A"}]},
                         clarification_source="phase2")
    monkeypatch.setattr(main, "tier2_resolve", MagicMock(return_value=wf))

    out = main._run_query({"question": "ambiguous", "id": "ns-1"})

    assert out["needs_clarification"] is True
    assert out["answer"] == "Which one?"


def test_tier2_degraded_grounding_unresolved(monkeypatch):
    """Grounding ceiling hit → graceful explanation, never executes/5xx."""
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: MetricMatch())
    wf = WorkflowContext(question="q", namespace="ns", kb_id="kb-1",
                         candidates=["db.a"], sql="SELECT bad FROM a",
                         degraded="grounding_unresolved")
    monkeypatch.setattr(main, "tier2_resolve", MagicMock(return_value=wf))

    out = main._run_query({"question": "x", "id": "ns-1"})

    assert "error" not in out
    assert "grounded" in out["answer"].lower()


def test_tier2_degraded_execution_failed(monkeypatch):
    """A grounded query that fails at Athena degrades to a failure message —
    never reported as a valid 0-row success."""
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: MetricMatch())
    wf = WorkflowContext(question="q", namespace="ns", kb_id="kb-1",
                         candidates=["db.a"], sql="SELECT x FROM a",
                         degraded="execution_failed",
                         degraded_detail="SYNTAX_ERROR: bad cast",
                         execution_result={"execution_failed": True,
                                           "error": "SYNTAX_ERROR: bad cast",
                                           "rows": [], "columns": []})
    monkeypatch.setattr(main, "tier2_resolve", MagicMock(return_value=wf))

    out = main._run_query({"question": "x", "id": "ns-1"})

    assert "error" not in out  # graceful, not a 5xx
    assert "failed to execute" in out["answer"].lower()
    # Must NOT masquerade as a successful empty result.
    assert out.get("results") in ([], None)


def test_tier3_recovery_stamps_agentic_provenance(monkeypatch):
    """A Tier-3-rescued answer reports tier "agentic", not "semantic_sql".

    The monitoring tab buckets answered turns by ``provenance.tier``. Tier 3
    success also clears ``degraded``, so if the tier stayed "semantic_sql" a
    rescued turn would be indistinguishable from a clean Tier 2 run and the
    agentic layer would read as permanently unused.
    """
    from agents.metadata_query_agent.tier3.supervisor import SupervisorOutcome

    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: MetricMatch())
    wf = WorkflowContext(question="q", namespace="ns", kb_id="kb-1",
                         candidates=["db.a"], degraded="grounding_unresolved")
    monkeypatch.setattr(main, "tier2_resolve", MagicMock(return_value=wf))
    monkeypatch.setattr(main, "_run_tier3_supervisor", lambda **_: SupervisorOutcome(
        ran=True, accepted=True, answer="42 claims",
        sql="SELECT count(*) FROM db.claim", results=[["42"]], columns=["c"],
        rounds=2,
    ))

    out = main._run_query({"question": "x", "id": "ns-1"})

    assert out["executionMode"] == "agentic"
    assert out["provenance"]["tier"] == "agentic"
    assert out["provenance"]["degraded"] is None
    assert out["answer"] == "42 claims"
    # A rescued run executed, so the executed-SQL contract must reflect that.
    assert out["executed"] is True
    assert out["executed_sql"] == "SELECT count(*) FROM db.claim"


def test_tier3_non_recovery_keeps_semantic_sql_provenance(monkeypatch):
    """Tier 3 that fails to recover keeps the Tier 2 degraded answer AND tier."""
    from agents.metadata_query_agent.tier3.supervisor import SupervisorOutcome

    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: MetricMatch())
    wf = WorkflowContext(question="q", namespace="ns", kb_id="kb-1",
                         candidates=["db.a"], degraded="grounding_unresolved")
    monkeypatch.setattr(main, "tier2_resolve", MagicMock(return_value=wf))
    monkeypatch.setattr(main, "_run_tier3_supervisor", lambda **_: SupervisorOutcome(
        ran=True, accepted=False, rounds=3, reason="max_rounds",
    ))

    out = main._run_query({"question": "x", "id": "ns-1"})

    assert out["executionMode"] == "workflow"
    assert out["provenance"]["tier"] == "semantic_sql"
    assert out["provenance"]["degraded"] == "grounding_unresolved"


def test_run_execution_flags_failure_when_cache_empty(monkeypatch):
    """_run_execution: a cache with no 'query_result' (every error path) yields
    execution_failed, not a fabricated empty success."""
    from agents.metadata_query_agent import main as m

    m.reset_agent_state("s-exec")
    # No 'query_result' cached → simulates a failed execute_sql_query.
    deps = m._build_phase_deps(kb_id="kb-1")
    monkeypatch.setattr(m, "build_execution_agent", lambda **_: object())
    monkeypatch.setattr(m, "run_execution",
                        lambda **_: {"answer": "Athena error: bad cast",
                                     "usage": {}})

    result = deps.run_execution("SELECT 1", "db", "cat", slice_text="{}")

    assert result["execution_failed"] is True
    assert "bad cast" in result["error"]
    assert result["rows"] == []


def test_run_execution_returns_rows_on_success(monkeypatch):
    """_run_execution: a populated cache is returned as a normal result."""
    import json as _json
    from agents.metadata_query_agent import main as m

    m.reset_agent_state("s-exec2")
    deps = m._build_phase_deps(kb_id="kb-1")
    monkeypatch.setattr(m, "build_execution_agent", lambda **_: object())

    def _fake_run(**_):
        # Populate the per-invocation cache the way execute_sql_query would.
        m._get_state()["cached_results"]["query_result"] = _json.dumps(
            {"columns": ["id"], "rows": [["1"]]})
        return {"answer": "1 row", "usage": {}}

    monkeypatch.setattr(m, "run_execution", _fake_run)

    result = deps.run_execution("SELECT id FROM t", "db", "cat", slice_text="{}")

    assert result.get("execution_failed") is not True
    assert result["rows"] == [["1"]]
    assert result["answer"] == "1 row"


def test_tier2_workflow_exception_returns_error_not_raise(monkeypatch):
    """An unexpected workflow error degrades to an error answer, not a crash."""
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: MetricMatch())
    monkeypatch.setattr(main, "tier2_resolve",
                        MagicMock(side_effect=RuntimeError("kb timeout")))

    out = main._run_query({"question": "x", "id": "ns-1"})

    assert "error" in out


def test_unbound_filter_falls_through_to_tier2_with_related_metric(monkeypatch):
    """A scoped question against a filterable metric must NOT answer at Tier 1.

    THE BUG this branch fixes: the metric matched semantically, but nothing bound
    its declared ``region`` filter, so executing its compiled SQL would return the
    GLOBAL number under a green "Governed Metric" badge.
    """
    _stub_metadata(monkeypatch)
    # Real strings, not MagicMock auto-attributes: metric_ref puts these straight
    # into provenance, which is JSON-serialized onto the SSE frame.
    metric = SimpleNamespace(
        metric_id="net_revenue", name="Net Revenue (TTM)",
        description="gross - returns", lifecycle="PUBLISHED", version=3,
        supported_filters=["region"], supported_dimensions=[],
        allowed_values=None, compiled_sql="SELECT sum(amt) FROM t",
        dialect="athena",
    )
    monkeypatch.setattr(
        main, "tier1_lookup",
        lambda **_: MetricMatch(matched=metric, matched_score=0.91),
    )
    executed = []
    monkeypatch.setattr(main, "tier1_execute",
                        lambda **kw: executed.append(kw))
    wf = WorkflowContext(question="q", namespace="ns", kb_id="kb-1",
                         candidates=["db.sales"], sql="SELECT 1",
                         execution_result={"columns": ["c"], "rows": [["1"]],
                                           "answer": "1 row"})
    monkeypatch.setattr(main, "tier2_resolve", MagicMock(return_value=wf))

    out = main._run_query(
        {"question": "net revenue for the EMEA region", "id": "ns-1"},
    )

    assert executed == [], "Tier 1 must not run when scope is undetermined"
    prov = out["provenance"]
    assert prov["tier"] != "governed_metric"
    assert prov["related_metrics"][0]["reason"] == "unbound_parameter"
    assert prov["related_metrics"][0]["metric_id"] == "net_revenue"


def test_tier1_hit_states_the_scope_it_applied(monkeypatch):
    """A bound filter executes SCOPED and provenance states the binding."""
    _stub_metadata(monkeypatch)
    metric = SimpleNamespace(
        metric_id="net_revenue", name="Net Revenue (TTM)",
        description="gross - returns", lifecycle="PUBLISHED", version=3,
        supported_filters=["region"], supported_dimensions=[],
        allowed_values={"region": ["EMEA", "AMER"]},
        compiled_sql="SELECT sum(amt) AS total FROM t", dialect="athena",
    )
    monkeypatch.setattr(
        main, "tier1_lookup",
        lambda **_: MetricMatch(matched=metric, matched_score=0.91),
    )
    seen = {}

    def _execute(**kw):
        seen.update(kw)
        return {"columns": ["total"], "rows": [{"total": "42"}],
                "metric_id": "net_revenue"}

    monkeypatch.setattr(main, "tier1_execute", _execute)
    monkeypatch.setattr(main, "tier2_resolve", MagicMock())

    out = main._run_query({"question": "net revenue in EMEA", "id": "ns-1"})

    assert seen["filters"] == {"region": "EMEA"}, "must execute scoped"
    assert out["provenance"]["metric"]["binding"] == {
        "bound": {"region": "EMEA"}, "group_by": [],
    }
