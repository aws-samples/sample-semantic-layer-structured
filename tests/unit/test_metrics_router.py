"""Unit tests for the metrics FastAPI router."""
import os
import sys
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lambda", "rest-api"))

from routers.metrics import build_router  # noqa: E402
from agents.shared.metric_models import Metric, MetricLifecycle  # noqa: E402


def _client(svc):
    app = FastAPI()
    app.include_router(build_router(lambda: svc))
    return TestClient(app)


def test_post_metric_returns_201():
    svc = MagicMock()
    svc.create.return_value = Metric(
        metric_id="x", namespace="ns", name="X", description="x",
        compiled_sql="SELECT 1", dialect="athena",
    )
    c = _client(svc)
    r = c.post(
        "/namespaces/ns/metrics",
        json={
            "metric_id": "x", "namespace": "ns", "name": "X", "description": "x",
            "compiled_sql": "SELECT 1", "dialect": "athena",
        },
    )
    assert r.status_code == 201
    assert r.json()["lifecycle"] == "DRAFT"


def test_post_publish_route():
    svc = MagicMock()
    svc.publish.return_value = Metric(
        metric_id="x", namespace="ns", name="X", description="x",
        compiled_sql="SELECT 1", dialect="athena",
        lifecycle=MetricLifecycle.PUBLISHED,
    )
    c = _client(svc)
    r = c.post("/namespaces/ns/metrics/x:publish")
    assert r.status_code == 200
    assert r.json()["lifecycle"] == "PUBLISHED"


def test_get_404_when_missing():
    svc = MagicMock()
    svc.get.return_value = None
    c = _client(svc)
    r = c.get("/namespaces/ns/metrics/x")
    assert r.status_code == 404
