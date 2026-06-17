"""Unit tests for MetricService — DDB CRUD + sqlglot validation + KNN sync.

The repo convention is to sys.path.insert ``lambda/rest-api`` and import
modules as ``services.X`` (mirrors how the FastAPI app imports them).
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lambda", "rest-api"))

from services.metric_service import MetricService  # noqa: E402
from agents.shared.metric_models import Metric, MetricLifecycle  # noqa: E402


def _svc(table=None, embed=None):
    return MetricService(
        ddb_table=table or MagicMock(),
        embed_fn=embed or (lambda t: [0.0] * 1024),
    )


def test_create_validates_sql_via_sqlglot():
    svc = _svc()
    with pytest.raises(ValueError, match="invalid SQL"):
        svc.create(
            Metric(
                metric_id="x", namespace="ns", name="X", description="x",
                compiled_sql="SELEC 1 FROM", dialect="athena",
            )
        )


def test_create_rejects_non_select_root():
    """SE.1: governed metrics are read-only — DROP/DELETE/INSERT must reject."""
    svc = _svc()
    for bad in ("DROP TABLE x", "DELETE FROM x WHERE 1=1", "INSERT INTO x VALUES (1)"):
        with pytest.raises(ValueError, match="invalid SQL"):
            svc.create(
                Metric(
                    metric_id="x", namespace="ns", name="X", description="x",
                    compiled_sql=bad, dialect="athena",
                )
            )


def test_create_writes_ddb_with_embedding_when_published():
    table = MagicMock()
    embed = MagicMock(return_value=[0.1] * 1024)
    svc = _svc(table=table, embed=embed)
    m = Metric(
        metric_id="x", namespace="ns", name="X", description="x",
        compiled_sql="SELECT 1", dialect="athena",
        lifecycle=MetricLifecycle.PUBLISHED,
    )

    svc.create(m)

    table.put_item.assert_called_once()
    item = table.put_item.call_args.kwargs["Item"]
    # Embedding is persisted on the row (Decimal-encoded for DDB).
    assert len(item["embedding"]) == 1024


def test_create_skips_embedding_when_draft():
    table = MagicMock()
    embed = MagicMock()
    svc = _svc(table=table, embed=embed)
    svc.create(
        Metric(
            metric_id="x", namespace="ns", name="X", description="x",
            compiled_sql="SELECT 1", dialect="athena",
        )
    )
    embed.assert_not_called()
    item = table.put_item.call_args.kwargs["Item"]
    assert "embedding" not in item


def test_publish_writes_embedding_and_bumps_version():
    table = MagicMock()
    table.get_item.return_value = {
        "Item": Metric(
            metric_id="x", namespace="ns", name="X", description="x",
            compiled_sql="SELECT 1", dialect="athena",
            lifecycle=MetricLifecycle.APPROVED, version=2,
        ).to_ddb_item()
    }
    svc = _svc(table=table)
    out = svc.publish(namespace="ns", metric_id="x")
    assert out.lifecycle == MetricLifecycle.PUBLISHED
    assert out.version == 3
    item = table.put_item.call_args.kwargs["Item"]
    assert "embedding" in item
