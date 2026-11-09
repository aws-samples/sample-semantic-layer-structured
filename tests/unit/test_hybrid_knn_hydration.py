"""Unit tests for the hybrid query agent's copy-and-own lazy KNN hydration.

Exercises ``agents.semantic_layer_query_agent.knn_hydration`` — the on-demand,
per-namespace hydration of the in-memory KNN index for both the shared
``metrics`` index and per-namespace ``topic-router-<ns>`` indexes.
"""
from unittest.mock import MagicMock

import pytest

from agents.semantic_layer_query_agent import knn_hydration, knn_index


@pytest.fixture(autouse=True)
def _reset() -> None:
    """Reset vendored KNN + hydration state between tests."""
    knn_index.reset_for_tests()
    knn_hydration.reset_for_tests()


def _table_with_items(items: list) -> MagicMock:
    table = MagicMock()
    table.query.return_value = {"Items": items}
    return table


def test_hydrate_metrics_indexes_published_rows():
    table = _table_with_items([
        {
            "metric_id": "monthly_revenue", "lifecycle": "PUBLISHED",
            "embedding": [0.1] * knn_hydration.EMBED_DIM,
            "name": "Monthly revenue", "version": 3,
        },
    ])
    n = knn_hydration.hydrate_metrics_namespace(namespace="ns", ddb_table=table)
    assert n == 1
    hits = knn_index.knn_search(
        endpoint="", index=knn_hydration.METRICS_INDEX,
        vector=[0.1] * knn_hydration.EMBED_DIM, k=5,
        filter_terms={"namespace": "ns"},
    )
    assert [h["id"] for h in hits] == ["monthly_revenue"]


def test_hydrate_metrics_is_idempotent():
    table = _table_with_items([
        {
            "metric_id": "m", "lifecycle": "PUBLISHED",
            "embedding": [0.2] * knn_hydration.EMBED_DIM,
            "name": "M", "version": 1,
        },
    ])
    assert knn_hydration.hydrate_metrics_namespace(namespace="ns", ddb_table=table) == 1
    # Second call is a no-op (already hydrated).
    assert knn_hydration.hydrate_metrics_namespace(namespace="ns", ddb_table=table) == 0


def test_hydrate_metrics_skips_non_published_and_missing_embedding():
    table = _table_with_items([
        {"metric_id": "draft", "lifecycle": "DRAFT",
         "embedding": [0.1] * knn_hydration.EMBED_DIM, "name": "D"},
        {"metric_id": "no_embed", "lifecycle": "PUBLISHED", "name": "N"},
    ])
    n = knn_hydration.hydrate_metrics_namespace(namespace="ns", ddb_table=table)
    assert n == 0


def test_hydrate_topic_router_indexes_iris(monkeypatch):
    # Stub embed_text so no Bedrock call happens.
    monkeypatch.setattr(
        knn_hydration, "embed_text",
        lambda text: [0.3] * knn_hydration.EMBED_DIM,
    )

    def fetch(ns: str):
        return [
            {"iri": "http://x/Customer", "label": "Customer",
             "comment": "A buyer", "synonyms": ["client"], "kind": "class"},
            {"iri": "http://x/blank", "label": "", "comment": "",
             "synonyms": [], "kind": "class"},  # no text -> skipped
        ]

    n = knn_hydration.hydrate_topic_router_namespace(
        namespace="ns", fetch_iri_metadata=fetch,
    )
    assert n == 1
    hits = knn_index.knn_search(
        endpoint="", index="topic-router-ns",
        vector=[0.3] * knn_hydration.EMBED_DIM, k=5,
    )
    assert [h["id"] for h in hits] == ["http://x/Customer"]


def test_hydrate_topic_router_is_idempotent(monkeypatch):
    monkeypatch.setattr(
        knn_hydration, "embed_text",
        lambda text: [0.4] * knn_hydration.EMBED_DIM,
    )
    fetch = MagicMock(return_value=[
        {"iri": "http://x/A", "label": "A", "comment": "", "synonyms": [],
         "kind": "class"},
    ])
    assert knn_hydration.hydrate_topic_router_namespace(
        namespace="ns", fetch_iri_metadata=fetch) == 1
    assert knn_hydration.hydrate_topic_router_namespace(
        namespace="ns", fetch_iri_metadata=fetch) == 0
    # Idempotent: fetch only called on the first (non-cached) hydration.
    assert fetch.call_count == 1
