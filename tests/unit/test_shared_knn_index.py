"""Unit tests for the in-memory KNN helper.

Verifies the OSS-compatible call surface (``ensure_index``, ``upsert``,
``knn_search``, ``delete``) along with namespace pre-filtering and cold-start
``IndexNotFoundError`` behaviour.
"""
import pytest

from agents.shared import knn_index
from agents.shared.knn_index import IndexNotFoundError


@pytest.fixture(autouse=True)
def _reset() -> None:
    """Reset module state between tests so they don't leak indexes."""
    knn_index.reset_for_tests()


def test_ensure_index_is_idempotent() -> None:
    knn_index.ensure_index("", "metrics", dim=1024)
    knn_index.ensure_index("", "metrics", dim=1024)  # second call OK
    # Mismatched dim raises so silent index drift fails loudly.
    with pytest.raises(ValueError):
        knn_index.ensure_index("", "metrics", dim=512)


def test_upsert_requires_embedding() -> None:
    knn_index.ensure_index("", "metrics", dim=4)
    with pytest.raises(ValueError):
        knn_index.upsert("", "metrics", doc_id="x", doc={"id": "x"})


def test_upsert_validates_dim() -> None:
    knn_index.ensure_index("", "metrics", dim=4)
    with pytest.raises(ValueError):
        knn_index.upsert(
            "", "metrics", doc_id="x",
            doc={"id": "x", "embedding": [1.0, 0.0, 0.0]},
        )


def test_knn_search_returns_top_k_by_cosine() -> None:
    knn_index.ensure_index("", "metrics", dim=2)
    knn_index.upsert("", "metrics", doc_id="a",
                     doc={"id": "a", "embedding": [1.0, 0.0]})
    knn_index.upsert("", "metrics", doc_id="b",
                     doc={"id": "b", "embedding": [0.0, 1.0]})
    knn_index.upsert("", "metrics", doc_id="c",
                     doc={"id": "c", "embedding": [0.9, 0.1]})

    hits = knn_index.knn_search(
        endpoint="", index="metrics", vector=[1.0, 0.0], k=2,
    )
    assert [h["id"] for h in hits] == ["a", "c"]
    assert hits[0]["score"] == pytest.approx(1.0)
    # embedding is stripped from the returned hit (matches old contract).
    assert "embedding" not in hits[0]


def test_knn_search_namespace_filter() -> None:
    """F5: pre-filter by namespace so cross-namespace hits don't mask matches."""
    knn_index.ensure_index("", "metrics", dim=2)
    knn_index.upsert("", "metrics", doc_id="ns1:a",
                     doc={"id": "ns1:a", "namespace": "ns1",
                          "embedding": [1.0, 0.0]})
    knn_index.upsert("", "metrics", doc_id="ns2:b",
                     doc={"id": "ns2:b", "namespace": "ns2",
                          "embedding": [0.99, 0.01]})

    hits = knn_index.knn_search(
        endpoint="", index="metrics", vector=[1.0, 0.0], k=5,
        filter_terms={"namespace": "ns2"},
    )
    assert [h["id"] for h in hits] == ["ns2:b"]


def test_knn_search_raises_on_missing_index() -> None:
    with pytest.raises(IndexNotFoundError):
        knn_index.knn_search(
            endpoint="", index="never-created", vector=[0.0, 0.0], k=1,
        )


def test_knn_search_empty_index_returns_empty_list() -> None:
    knn_index.ensure_index("", "metrics", dim=2)
    assert knn_index.knn_search(
        endpoint="", index="metrics", vector=[1.0, 0.0], k=3,
    ) == []


def test_delete_is_idempotent() -> None:
    knn_index.ensure_index("", "metrics", dim=2)
    knn_index.upsert("", "metrics", doc_id="a",
                     doc={"id": "a", "embedding": [1.0, 0.0]})
    knn_index.delete(endpoint="", index="metrics", doc_id="a")
    knn_index.delete(endpoint="", index="metrics", doc_id="a")  # no-op
    knn_index.delete(endpoint="", index="never-created", doc_id="x")  # no-op
    assert knn_index.knn_search(
        endpoint="", index="metrics", vector=[1.0, 0.0], k=1,
    ) == []
