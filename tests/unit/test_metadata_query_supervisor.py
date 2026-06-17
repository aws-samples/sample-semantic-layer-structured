"""Tests for metadata_query_agent Tier 2 routing.

The metadata agent no longer uses the supervised-worker loop (``run_supervised``)
— a Tier 1 miss now routes straight into the Strands graph workflow
(``tier2_resolve``). This asserts that wiring.
"""
from unittest.mock import MagicMock

from agents.metadata_query_agent.tier2.workflow import WorkflowContext


def test_run_query_routes_into_tier2_workflow(monkeypatch):
    from agents.metadata_query_agent import main

    seen = {}

    def fake_tier2_resolve(question, namespace, kb_id="", phase_sink=None,
                           clarification_resolution=None, recall_resolver=None,
                           **_kwargs):
        seen['question'] = question
        seen['namespace'] = namespace
        return WorkflowContext(
            question=question, namespace=namespace, kb_id=kb_id,
            candidates=["db.customers"], sql="SELECT 1",
            execution_result={"columns": ["c"], "rows": [["1"]], "answer": "ok"},
        )

    monkeypatch.setattr(main, 'tier1_lookup', lambda **_: None)
    monkeypatch.setattr(main, 'tier2_resolve', fake_tier2_resolve)
    monkeypatch.setattr(main, 'get_latest_metadata_item',
                        lambda _id: {'name': 'demo', 'namespace': 'ns-x',
                                     'version': 'v1'})

    out = main._run_query({'question': 'how many customers?', 'id': 'ns-1'})

    assert seen['question'] == 'how many customers?'
    assert seen['namespace'] == 'ns-x'
    assert out['answer'] == 'ok'
