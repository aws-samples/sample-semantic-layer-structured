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


def _item(metric_id: str, version: int = 1) -> dict:
    """Return a minimal valid DDB metric item for hydration.

    :param metric_id: The metric id to embed in the item.
    :param version: Lifecycle version to report.
    :return: A dict shaped like a stored ``semantic-layer-metrics`` item.
    """
    return {
        "metric_id": metric_id, "namespace": "ns",
        "name": f"Name {metric_id}", "description": "desc",
        "compiled_sql": "SELECT 1", "dialect": "athena",
        "lifecycle": "PUBLISHED", "version": version,
    }


def _hydrating_table() -> MagicMock:
    """Return a DDB Table mock that hydrates any requested metric id."""
    table = _table_with_no_items()
    table.get_item.side_effect = lambda Key: {
        "Item": _item(Key["sk"].split("#", 1)[1]),
    }
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
    assert out.matched is not None
    assert out.matched.metric_id == "monthly_revenue"
    assert out.matched_score == 0.91
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
    assert out.matched is None


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
    assert out.matched is None


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
    assert out.matched is None


def test_match_carries_near_misses_below_threshold(monkeypatch):
    """Sub-threshold hits above RELATED_FLOOR are retained, not discarded."""
    _reset()
    monkeypatch.setattr(metric_lookup, "embed_text", lambda t: [0.0] * 1024)
    fake_knn = MagicMock()
    fake_knn.knn_search.return_value = [
        {"id": "net_revenue", "namespace": "ns", "score": 0.83, "metadata": {}},
        {"id": "gross_revenue", "namespace": "ns", "score": 0.71, "metadata": {}},
    ]
    out = metric_lookup.lookup(
        question="revenue in EMEA", namespace="ns", ddb_table=_hydrating_table(),
        knn=fake_knn, knn_endpoint="", knn_index="metrics", threshold=0.85,
    )
    assert out.matched is None
    assert [n.score for n in out.near_misses] == [0.83, 0.71]
    assert out.near_misses[0].metric.metric_id == "net_revenue"


def test_near_miss_below_floor_is_excluded(monkeypatch):
    """A weak hit below RELATED_FLOOR is noise — dropped, not shown."""
    _reset()
    monkeypatch.setattr(metric_lookup, "embed_text", lambda t: [0.0] * 1024)
    fake_knn = MagicMock()
    fake_knn.knn_search.return_value = [
        {"id": "weak", "namespace": "ns", "score": 0.42, "metadata": {}},
    ]
    out = metric_lookup.lookup(
        question="unrelated", namespace="ns", ddb_table=_hydrating_table(),
        knn=fake_knn, knn_endpoint="", knn_index="metrics", threshold=0.85,
    )
    assert out.near_misses == []


def test_near_misses_capped_at_limit(monkeypatch):
    """At most NEAR_MISS_LIMIT near-misses are carried."""
    _reset()
    monkeypatch.setattr(metric_lookup, "embed_text", lambda t: [0.0] * 1024)
    fake_knn = MagicMock()
    fake_knn.knn_search.return_value = [
        {"id": "a", "namespace": "ns", "score": 0.80, "metadata": {}},
        {"id": "b", "namespace": "ns", "score": 0.75, "metadata": {}},
        {"id": "c", "namespace": "ns", "score": 0.70, "metadata": {}},
    ]
    out = metric_lookup.lookup(
        question="q", namespace="ns", ddb_table=_hydrating_table(), knn=fake_knn,
        knn_endpoint="", knn_index="metrics", threshold=0.85,
    )
    assert len(out.near_misses) == metric_lookup.NEAR_MISS_LIMIT
    assert [n.metric.metric_id for n in out.near_misses] == ["a", "b"]


def test_hit_with_near_misses_carries_both(monkeypatch):
    """A clear winner returns early — runners-up are not surfaced."""
    _reset()
    monkeypatch.setattr(metric_lookup, "embed_text", lambda t: [0.0] * 1024)
    fake_knn = MagicMock()
    fake_knn.knn_search.return_value = [
        {"id": "winner", "namespace": "ns", "score": 0.93, "metadata": {}},
        {"id": "runner", "namespace": "ns", "score": 0.72, "metadata": {}},
    ]
    out = metric_lookup.lookup(
        question="q", namespace="ns", ddb_table=_hydrating_table(), knn=fake_knn,
        knn_endpoint="", knn_index="metrics", threshold=0.85,
    )
    assert out.matched.metric_id == "winner"
    assert out.near_misses == []


def test_hydration_failure_yields_empty_match(monkeypatch):
    """A hydration error must yield an empty MetricMatch, never None."""
    _reset()

    def _boom(**_):
        raise RuntimeError("ddb down")

    # ``lookup`` imports knn_hydration inside the function body, so patch the
    # real module rather than an attribute on metric_lookup.
    monkeypatch.setattr(knn_hydration, "hydrate_metrics_namespace", _boom)
    out = metric_lookup.lookup(
        question="q", namespace="ns", ddb_table=_table_with_no_items(),
        knn=MagicMock(), knn_endpoint="", knn_index="metrics",
    )
    assert out.matched is None and out.near_misses == []
