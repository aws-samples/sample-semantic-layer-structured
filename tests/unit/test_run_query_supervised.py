"""Tests for ontology_query_agent Tier 2 routing.

The ontology agent no longer uses the supervised-worker loop (``run_supervised``)
or a Tier 3 fallback — a Tier 1 miss now routes straight into the VKG Strands
graph workflow (``tier2_resolve``), which reaches Neptune via the gateway MCP.
This asserts that wiring and the byte-compatible response shape.
"""
from unittest.mock import MagicMock

from agents.shared.tier2_graph import WorkflowContext


def test_run_query_routes_into_tier2_workflow(monkeypatch):
    from agents.ontology_query_agent import main

    seen = {}

    def fake_tier2_resolve(question, namespace, *, ontology_id="", phase_sink=None,
                           clarification_resolution=None, recall_resolver=None,
                           conversation_history=None):
        seen['question'] = question
        seen['namespace'] = namespace
        seen['ontology_id'] = ontology_id
        return WorkflowContext(
            question=question, namespace=namespace,
            candidates=["ex:Customer"], sql="SELECT ?c WHERE { ?c a ex:Customer }",
            execution_result={"columns": ["c"], "rows": [["1"]],
                              "answer": "ok", "n_quads": []},
            disambiguation={"customer": {"iri": "ex:Customer"}},
        )

    monkeypatch.setattr(main, 'tier1_lookup', lambda **_: None)
    monkeypatch.setattr(main, 'tier2_resolve', fake_tier2_resolve)
    monkeypatch.setattr(main, 'get_latest_metadata_item',
                        lambda _id: {'name': 'demo', 'namespace': 'ns-x'})

    out = main._run_query({'question': 'how many customers?', 'id': 'ont-1'})

    assert seen['question'] == 'how many customers?'
    assert seen['namespace'] == 'ns-x'
    # The ontology id flows to tier2_resolve so the gateway fetch_ontology
    # targets the right named graph.
    assert seen['ontology_id'] == 'ont-1'
    assert out['sql_query'] == "SELECT ?c WHERE { ?c a ex:Customer }"
    assert out['results'] == [{"c": "1"}]
    assert out['answer'] == "ok"
    # graph-traversal summary built from the resolved term→IRI binding
    assert "customer" in out['reasoning']['graphTraversal'].lower()
