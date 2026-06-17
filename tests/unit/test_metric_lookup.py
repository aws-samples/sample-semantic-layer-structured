"""Unit tests for the Tier 1 governed-metric lookup helper.

Each test resets ``knn_hydration`` state so the hydration step inside
``metric_lookup.lookup`` is exercised once per test rather than being
silently skipped after the first call.
"""
from unittest.mock import MagicMock

from agents.shared import knn_hydration, knn_index, metric_lookup


def _reset() -> None:
    """Reset shared in-memory KNN + hydration state between tests."""
    knn_index.reset_for_tests()
    knn_hydration.reset_for_tests()


def _table_with_no_items() -> MagicMock:
    """Return a DDB Table mock whose query() returns an empty Items list."""
    table = MagicMock()
    table.query.return_value = {"Items": []}
    return table


def test_lookup_returns_hit_above_threshold(monkeypatch):
    _reset()
    monkeypatch.setattr(metric_lookup, "embed_text", lambda t: [0.0] * 1024)
    fake_knn = MagicMock()
    captured_kwargs: dict = {}

    def _capture(**kw):
        captured_kwargs.update(kw)
        return [
            {"id": "monthly_revenue", "namespace": "ns", "score": 0.91,
             "metadata": {"name": "Monthly revenue", "version": 3}},
        ]
    fake_knn.knn_search.side_effect = _capture

    table = _table_with_no_items()
    table.get_item.return_value = {"Item": {
        "metric_id": "monthly_revenue", "namespace": "ns",
        "name": "Monthly revenue", "description": "x",
        "compiled_sql": "SELECT 1", "dialect": "athena",
        "lifecycle": "PUBLISHED", "version": 3,
    }}

    out = metric_lookup.lookup(
        question="show me monthly revenue", namespace="ns",
        ddb_table=table, knn=fake_knn,
        knn_endpoint="", knn_index="metrics",
        threshold=0.85,
    )
    assert out is not None
    assert out.metric_id == "monthly_revenue"
    assert captured_kwargs.get("filter_terms") == {"namespace": "ns"}


def test_lookup_swallows_knn_error_and_returns_none(monkeypatch):
    """KNN error must NOT 500 — fall through to Tier 2."""
    _reset()
    monkeypatch.setattr(metric_lookup, "embed_text", lambda t: [0.0] * 1024)
    fake_knn = MagicMock()
    fake_knn.knn_search.side_effect = ConnectionError("knn unreachable")
    out = metric_lookup.lookup(
        question="x", namespace="ns",
        ddb_table=_table_with_no_items(), knn=fake_knn,
        knn_endpoint="", knn_index="metrics", threshold=0.85,
    )
    assert out is None


def test_lookup_falls_through_below_threshold(monkeypatch):
    _reset()
    monkeypatch.setattr(metric_lookup, "embed_text", lambda t: [0.0] * 1024)
    fake_knn = MagicMock()
    fake_knn.knn_search.return_value = [
        {"id": "x", "namespace": "ns", "score": 0.5, "metadata": {}}
    ]
    out = metric_lookup.lookup(
        question="random", namespace="ns",
        ddb_table=_table_with_no_items(), knn=fake_knn,
        knn_endpoint="", knn_index="metrics",
        threshold=0.85,
    )
    assert out is None


def test_lookup_returns_none_on_empty_index(monkeypatch):
    _reset()
    monkeypatch.setattr(metric_lookup, "embed_text", lambda t: [0.0] * 1024)
    fake_knn = MagicMock()
    fake_knn.knn_search.return_value = []
    out = metric_lookup.lookup(
        question="x", namespace="ns",
        ddb_table=_table_with_no_items(), knn=fake_knn,
        knn_endpoint="", knn_index="metrics", threshold=0.85,
    )
    assert out is None
