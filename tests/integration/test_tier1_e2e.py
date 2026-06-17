"""End-to-end Tier 1: persist a metric in DDB → register in KNN → look it up
via metric_lookup → execute against (stubbed) Athena.

The REST authoring path is TypeScript, so this test exercises the Python
agent-runtime contract directly: write a Metric record using its DDB
serializer, register it in the in-memory KNN stub, then prove the agent's
Tier 1 lookup + executor produce the same payload the runtime returns to
the user.
"""
from __future__ import annotations

from unittest.mock import patch

from agents.shared import metric_executor, metric_lookup
from agents.shared.metric_models import Metric, MetricLifecycle


def test_tier1_round_trip(fake_ddb_table, fake_knn, fake_athena):
    """Author → publish → lookup → execute round trip with mocked AWS."""
    metric = Metric(
        metric_id="monthly_revenue",
        namespace="ns-default",
        name="Monthly revenue",
        description="Total revenue per month",
        synonyms=["revenue per month"],
        compiled_sql="SELECT 1 AS x",
        dialect="athena",
        lifecycle=MetricLifecycle.PUBLISHED,
    )
    # DynamoDB rejects float; embedding is irrelevant for the lookup path here.
    fake_ddb_table.put_item(Item=metric.to_ddb_item())
    fake_knn.index(
        doc_id="monthly_revenue", score=0.95,
        metadata={"namespace": "ns-default"},
    )

    with patch.object(metric_lookup, "embed_text", return_value=[0.1] * 1024):
        out = metric_lookup.lookup(
            question="show me monthly revenue", namespace="ns-default",
            ddb_table=fake_ddb_table, knn=fake_knn,
            knn_endpoint="https://x", knn_index="metrics", threshold=0.85,
        )
    assert out is not None
    assert out.metric_id == "monthly_revenue"

    rows = metric_executor.execute_metric(
        metric=out, filters={}, athena=fake_athena,
        workgroup="wg", output_loc="s3://bkt/",
    )
    assert rows["rows"] == [{"x": "1"}]
    assert rows["metric_id"] == "monthly_revenue"


def test_tier1_below_threshold_falls_through(
    fake_ddb_table, fake_knn,
):
    """Below-threshold KNN hit must return None so caller falls through."""
    fake_knn.index(
        doc_id="monthly_revenue", score=0.50,
        metadata={"namespace": "ns-default"},
    )

    with patch.object(metric_lookup, "embed_text", return_value=[0.1] * 1024):
        out = metric_lookup.lookup(
            question="something unrelated", namespace="ns-default",
            ddb_table=fake_ddb_table, knn=fake_knn,
            knn_endpoint="https://x", knn_index="metrics", threshold=0.85,
        )
    assert out is None


def test_tier1_namespace_isolation(fake_ddb_table, fake_knn):
    """Cross-namespace KNN hits must be filtered out before scoring."""
    fake_knn.index(
        doc_id="other_metric", score=0.95,
        metadata={"namespace": "ns-other"},
    )
    with patch.object(metric_lookup, "embed_text", return_value=[0.1] * 1024):
        out = metric_lookup.lookup(
            question="show me monthly revenue", namespace="ns-default",
            ddb_table=fake_ddb_table, knn=fake_knn,
            knn_endpoint="https://x", knn_index="metrics", threshold=0.85,
        )
    assert out is None
