"""Unit tests for the Tier 1 pre-check → Tier 2 VKG graph-workflow cascade.

The old 3-tier cascade (Tier 2 slice/query → Tier 3 supervised worker) was
replaced by a single Strands graph workflow (Phase 1→5). These tests assert:
  * Tier 1 still short-circuits on a governed-metric hit.
  * A Tier 1 miss routes into the Tier 2 VKG workflow (``tier2_resolve``).
  * The workflow's degraded paths and clarification produce a graceful answer
    rather than a 5xx.
"""
from unittest.mock import MagicMock

from agents.ontology_query_agent import main
from agents.shared.tier2_graph import WorkflowContext


def _stub_metadata(monkeypatch):
    monkeypatch.setattr(main, "get_latest_metadata_item",
                        lambda _id: {"name": "demo", "namespace": "ns-default"})


def test_tier1_short_circuits_when_metric_matches(monkeypatch):
    _stub_metadata(monkeypatch)
    fake_metric = MagicMock(metric_id="monthly_revenue", supported_filters=[],
                            supported_dimensions=[], compiled_sql="SELECT 1",
                            dialect="athena")
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: fake_metric)
    monkeypatch.setattr(main, "tier1_execute",
                        lambda **_: {"columns": ["x"], "rows": [{"x": 1}],
                                     "metric_id": "monthly_revenue"})
    spy_tier2 = MagicMock()
    monkeypatch.setattr(main, "tier2_resolve", spy_tier2)

    out = main._run_query({"question": "monthly revenue", "id": "ont-1"})

    spy_tier2.assert_not_called()
    assert "monthly_revenue" in str(out)


def test_tier1_miss_routes_into_tier2_workflow(monkeypatch):
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: None)
    wf = WorkflowContext(question="q", namespace="ns",
                         candidates=["ex:Policy"], sql="SELECT ?p WHERE { ?p a ex:Policy }",
                         execution_result={"columns": ["p"], "rows": [["1"]],
                                           "answer": "1 policy",
                                           "n_quads": ["<a> <b> <c> ."]})
    spy = MagicMock(return_value=wf)
    monkeypatch.setattr(main, "tier2_resolve", spy)

    out = main._run_query({"question": "list policies", "id": "ont-1"})

    spy.assert_called_once()
    assert out["sql_query"] == "SELECT ?p WHERE { ?p a ex:Policy }"
    assert out["results"] == [{"p": "1"}]
    assert out["n_quads"] == ["<a> <b> <c> ."]


def test_tier2_clarification_short_circuits(monkeypatch):
    """Phase 2 / 3b clarification → needs_clarification payload, no 5xx."""
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: None)
    wf = WorkflowContext(question="q", namespace="ns",
                         candidates=["ex:A", "ex:B"],
                         needs_clarification={
                             "needs_clarification": True,
                             "clarification_question": "Which one?",
                             "options": [{"id": "a", "label": "A"}]},
                         clarification_source="phase2")
    monkeypatch.setattr(main, "tier2_resolve", MagicMock(return_value=wf))

    out = main._run_query({"question": "ambiguous", "id": "ont-1"})

    assert out["needs_clarification"] is True
    assert out["answer"] == "Which one?"


def test_tier2_degraded_grounding_unresolved(monkeypatch):
    """Grounding ceiling hit → graceful explanation, never executes/5xx."""
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: None)
    wf = WorkflowContext(question="q", namespace="ns",
                         candidates=["ex:A"], sql="SELECT ?x WHERE { ?x ex:bad ?y }",
                         degraded="grounding_unresolved")
    monkeypatch.setattr(main, "tier2_resolve", MagicMock(return_value=wf))

    out = main._run_query({"question": "x", "id": "ont-1"})

    assert "error" not in out
    assert "grounded" in out["answer"].lower()


def test_tier2_degraded_sparql_repair_failed(monkeypatch):
    """SPARQL repair exhausted → graceful explanation, never 5xx."""
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: None)
    wf = WorkflowContext(question="q", namespace="ns",
                         candidates=["ex:A"], degraded="sparql_repair_failed")
    monkeypatch.setattr(main, "tier2_resolve", MagicMock(return_value=wf))

    out = main._run_query({"question": "x", "id": "ont-1"})

    assert "error" not in out
    assert "sparql" in out["answer"].lower()


def test_tier2_workflow_exception_returns_error_not_raise(monkeypatch):
    """An unexpected workflow error degrades to an error answer, not a crash."""
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: None)
    monkeypatch.setattr(main, "tier2_resolve",
                        MagicMock(side_effect=RuntimeError("neptune timeout")))

    out = main._run_query({"question": "x", "id": "ont-1"})

    assert "error" in out


def test_ontology_advisory_chunks_from_class_annotations(monkeypatch):
    """The VKG advisory KB is built from the ontology's class annotations
    (rdfs:comment + the curated vkg:* sections). A class with no description or
    annotations is skipped; newline escapes are unescaped for display."""
    import json
    monkeypatch.setitem(main._ontology_cache, "ont-x", json.dumps({
        "classes": {
            "http://ex/ontology/Coverage": {
                "label": "Coverage",
                "comment": "A coverage on a policy holding.",
                "businessPurpose": "Tracks what risks a policy insures.",
                "commonQueryPatterns": "count coverages by type\\ntotal sum insured",
            },
            "http://ex/ontology/EmptyClass": {"label": "Empty"},
        },
    }))
    out = json.loads(main._ontology_advisory_chunks(ontology_id="ont-x", namespace="ns"))
    ctx = out["context"]
    # EmptyClass (no description/annotations) is skipped.
    assert len(ctx) == 1
    content = ctx[0]["content"]
    assert "# Coverage" in content
    assert "## Business Purpose" in content
    assert "## Common Query Patterns" in content
    # N-Quads \n escapes are unescaped into real newlines.
    assert "count coverages by type\ntotal sum insured" in content
    assert "\\n" not in content
    assert ctx[0]["metadata"]["class"] == "Coverage"


def test_ontology_advisory_chunks_failsoft_on_empty(monkeypatch):
    """No ontology / gateway unavailable → empty context (advisory falls back to
    metrics-only), never raises."""
    import json
    monkeypatch.setattr(main, "_neptune_gateway_mcp", lambda: None)
    # Cache miss + no gateway → {"context": []}, not an exception.
    out = json.loads(main._ontology_advisory_chunks(ontology_id="absent", namespace="ns"))
    assert out == {"context": []}
