"""Tests for the RAG Phase 1 topic router."""
from unittest.mock import MagicMock

from agents.metadata_query_agent.tier2.rag_topic_router import RagTopicRouter


def test_returns_unique_table_ids_top_n():
    retrieve = MagicMock(return_value={
        "candidates": [
            {"table_id": "db.customers", "score": 0.91},
            {"table_id": "db.customers", "score": 0.86,
             "column_id": "db.customers.first_name"},
            {"table_id": "db.policy", "score": 0.82},
        ],
        "chunks": [],
    })
    r = RagTopicRouter(retrieve_fn=retrieve, kb_id_for=lambda ns: f"kb-{ns}")
    out = r.find_candidates(question="q", namespace="ns")
    assert out == ["db.customers", "db.policy"]
    retrieve.assert_called_once_with(user_query="q", kb_id="kb-ns", top_k=20)


def test_last_structured_property_holds_full_payload():
    payload = {"candidates": [{"table_id": "t1", "score": 0.5}], "chunks": ["c"]}
    retrieve = MagicMock(return_value=payload)
    r = RagTopicRouter(retrieve_fn=retrieve, kb_id_for=lambda ns: "kb")
    r.find_candidates(question="q", namespace="ns")
    assert r.last_structured == payload


def test_relevance_floor_drops_weak_tail_when_enough_strong():
    # 9 strong candidates clear the 0.10 floor; the weak tail (< 0.10) is dropped
    # so the slice doesn't overflow the token budget and evict a needed table.
    candidates = [{"table_id": f"db.strong{i}", "score": 0.5} for i in range(9)]
    candidates += [{"table_id": f"db.weak{i}", "score": 0.03} for i in range(10)]
    retrieve = MagicMock(return_value={"candidates": candidates, "chunks": []})
    r = RagTopicRouter(retrieve_fn=retrieve, kb_id_for=lambda ns: "kb")
    out = r.find_candidates(question="q", namespace="ns")
    assert out == [f"db.strong{i}" for i in range(9)]
    assert not any(t.startswith("db.weak") for t in out)


def test_min_candidates_floor_when_all_weak():
    # When every candidate is below the floor, keep the top min_candidates so a
    # weakly-matched question still gets a non-empty slice to judge.
    candidates = [{"table_id": f"db.t{i}", "score": 0.02} for i in range(20)]
    retrieve = MagicMock(return_value={"candidates": candidates, "chunks": []})
    r = RagTopicRouter(retrieve_fn=retrieve, kb_id_for=lambda ns: "kb",
                       min_candidates=8)
    out = r.find_candidates(question="q", namespace="ns")
    assert out == [f"db.t{i}" for i in range(8)]
