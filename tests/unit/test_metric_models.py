"""Unit tests for the shared Metric Pydantic model + DDB serializer."""
from decimal import Decimal

import pytest

from agents.shared.metric_models import Metric, MetricLifecycle


def test_metric_minimal_valid():
    m = Metric(
        metric_id="monthly_revenue",
        namespace="ns-default",
        name="Monthly revenue",
        description="Total revenue per month",
        compiled_sql="SELECT 1",
        dialect="athena",
    )
    assert m.lifecycle == MetricLifecycle.DRAFT
    assert m.version == 1
    assert m.synonyms == []


def test_metric_to_ddb_round_trip():
    m = Metric(
        metric_id="x",
        namespace="ns",
        name="X",
        description="x",
        compiled_sql="SELECT 1",
        dialect="athena",
        synonyms=["a", "b"],
    )
    item = m.to_ddb_item(embedding=[0.1, 0.2])
    assert item["pk"] == "NS#ns"
    assert item["sk"] == "METRIC#x"
    assert item["lifecycle"] == "DRAFT"
    # Embeddings must serialize to Decimal — DDB resource rejects native float.
    assert item["embedding"] == [Decimal("0.1"), Decimal("0.2")]
    assert all(isinstance(v, Decimal) for v in item["embedding"])
    assert Metric.from_ddb_item(item) == m


def test_metric_rejects_bad_dialect():
    with pytest.raises(ValueError, match="dialect"):
        Metric(
            metric_id="x",
            namespace="ns",
            name="X",
            description="x",
            compiled_sql="SELECT 1",
            dialect="oracle",
        )
