"""Unit tests for the Hybrid query agent's recall phases.

Covers the copy-and-own KB recall modules (Task 6):

* :class:`agents.semantic_layer_query_agent.topic_router.TopicRouter` — embed
  the question, KNN-search the KB, return candidate table ids.
* :class:`agents.semantic_layer_query_agent.slice_builder.SliceBuilder` —
  assemble the JSON schema slice and inject sample-SQL / glossary few-shots.

Both are exercised through injected callables so the tests need no AWS.
"""
from agents.semantic_layer_query_agent.slice_builder import SliceBuilder
from agents.semantic_layer_query_agent.topic_router import TopicRouter


def test_slice_includes_few_shots() -> None:
    """build() must serialize a slice carrying a ``few_shots`` key."""
    sb = SliceBuilder(kb_retrieve=lambda q, ns: [{"sample_sql": "SELECT 1"}],
                      judge_fn=lambda x: {"sufficient": True, "missing": []},
                      token_counter=len, budget=20000)
    slice_text = sb.build(candidates=["db.address"], namespace="ns", question="q")
    assert "few_shots" in slice_text


def test_router_returns_candidates() -> None:
    """find_candidates() must embed, KNN-search, and return the hit ids."""
    tr = TopicRouter(knn_search=lambda *, vector, k: [{"id": "db.address"}],
                     embed=lambda q: [0.0, 0.1])
    cands = tr.find_candidates(question="where do customers live", namespace="ns")
    assert "db.address" in cands


def test_slice_is_sufficient_reports_verdict() -> None:
    """is_sufficient() must forward to judge_fn and unpack (sufficient, missing)."""
    captured = {}

    def _judge(payload):
        captured.update(payload)
        return {"sufficient": False, "missing": ["db.party"]}

    sb = SliceBuilder(kb_retrieve=lambda q, ns: [],
                      judge_fn=_judge, token_counter=len, budget=20000)
    slice_text = sb.build(candidates=["db.address"], namespace="ns", question="q")
    ok, missing = sb.is_sufficient(slice_text=slice_text, question="q")
    assert ok is False
    assert missing == ["db.party"]
    # Judge must have received both the slice and the question.
    assert captured.get("question") == "q"
    assert captured.get("slice") == slice_text


def test_slice_expand_widens_candidates() -> None:
    """expand() must fold judge-named missing tables into the slice."""
    sb = SliceBuilder(kb_retrieve=lambda q, ns: [{"glossary": "term"}],
                      judge_fn=lambda x: {"sufficient": True, "missing": []},
                      token_counter=len, budget=20000)
    slice_text = sb.build(candidates=["db.address"], namespace="ns", question="q")
    widened = sb.expand(slice_text=slice_text, missing=["db.party"])
    assert "db.party" in widened
    assert "db.address" in widened


def test_router_dedups_and_preserves_order() -> None:
    """find_candidates() must de-duplicate ids while keeping KNN rank order."""
    hits = [{"id": "db.a"}, {"id": "db.b"}, {"id": "db.a"}]
    tr = TopicRouter(knn_search=lambda *, vector, k: hits,
                     embed=lambda q: [0.1, 0.2])
    cands = tr.find_candidates(question="q", namespace="ns")
    assert cands == ["db.a", "db.b"]
