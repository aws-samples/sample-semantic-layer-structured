"""Unit tests for the VKG Phase 1 topic router."""
from unittest.mock import MagicMock

import pytest

from agents.shared import knn_hydration, knn_index
from agents.shared.knn_index import IndexNotFoundError
from agents.ontology_query_agent.tier2.vkg_topic_router import VkgTopicRouter


@pytest.fixture(autouse=True)
def _reset_hydration_and_stub_embed(monkeypatch) -> None:
    """Per-test setup:
      - clear hydration ledger and shared in-memory KNN store so dim/index
        state from previous tests can't leak in
      - stub knn_hydration.embed_text so the hydrate step doesn't call Bedrock
    """
    knn_index.reset_for_tests()
    knn_hydration.reset_for_tests()
    monkeypatch.setattr(knn_hydration, "embed_text", lambda t: [0.0] * 1024)


def test_finds_candidates_via_knn():
    knn = MagicMock()
    knn.knn_search.return_value = [
        {"id": "ex:Customer", "score": 0.9, "metadata": {}},
        {"id": "ex:Policy", "score": 0.7, "metadata": {}},
    ]
    r = VkgTopicRouter(
        endpoint="", knn=knn,
        embed_fn=lambda t: [0.0] * 1024, neptune_lexical=MagicMock(),
        # Hydration fetcher returns one row so hydration succeeds and the
        # KNN path is exercised.
        fetch_iri_metadata=lambda ns: [
            {"iri": "ex:Customer", "label": "Customer",
             "comment": "a person", "synonyms": [], "kind": "class"},
        ],
    )
    out = r.find_candidates(question="who has a policy?", namespace="ns-default")
    assert out == ["ex:Customer", "ex:Policy"]
    knn.knn_search.assert_called_once_with(
        endpoint="", index="topic-router-ns-default",
        vector=[0.0] * 1024, k=20,
    )


def test_falls_back_to_lexical_when_index_missing():
    knn = MagicMock()
    knn.knn_search.side_effect = IndexNotFoundError("topic-router-ns")
    lex = MagicMock()
    lex.lexical_match.return_value = ["ex:LexHit"]
    r = VkgTopicRouter(
        endpoint="", knn=knn,
        embed_fn=lambda t: [0.0] * 1024, neptune_lexical=lex,
        fetch_iri_metadata=lambda ns: [
            {"iri": "ex:Customer", "label": "Customer",
             "comment": "a person", "synonyms": [], "kind": "class"},
        ],
    )
    out = r.find_candidates(question="customer", namespace="ns")
    assert out == ["ex:LexHit"]
    assert r.last_degraded == "phase1_cold_start"


def test_falls_back_to_lexical_on_hydration_failure():
    """Hydration failure (e.g. Neptune unavailable) must fall through to lexical."""
    knn = MagicMock()
    lex = MagicMock()
    lex.lexical_match.return_value = ["ex:LexHit"]

    def _boom(ns: str):
        raise RuntimeError("neptune unavailable")

    r = VkgTopicRouter(
        endpoint="", knn=knn,
        embed_fn=lambda t: [0.0] * 1024, neptune_lexical=lex,
        fetch_iri_metadata=_boom,
    )
    out = r.find_candidates(question="customer", namespace="ns")
    assert out == ["ex:LexHit"]
    assert r.last_degraded == "phase1_cold_start"
    knn.knn_search.assert_not_called()
