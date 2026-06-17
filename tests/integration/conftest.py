"""Shared fixtures for the progressive-disclosure integration tests."""
from __future__ import annotations

import os
from typing import Any, Dict, List

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws


@pytest.fixture
def aws_credentials():
    """Credentials placeholders so boto3 doesn't try real auth under moto."""
    os.environ["AWS_ACCESS_KEY_ID"] = "test"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
    os.environ["AWS_SESSION_TOKEN"] = "test"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    yield


@pytest.fixture
def fake_ddb_table(aws_credentials):
    """A moto-backed semantic-layer-metrics table with the canonical schema."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="semantic-layer-metrics",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb.Table("semantic-layer-metrics")


@pytest.fixture
def fake_knn():
    """Dict-backed KNN stub matching agents.shared.knn_index's call shape."""
    class _Knn:
        def __init__(self) -> None:
            self.docs: List[Dict[str, Any]] = []

        def index(self, *, doc_id: str, score: float, metadata: Dict[str, Any]) -> None:
            self.docs.append({"id": doc_id, "score": score, "metadata": metadata})

        def knn_search(self, *, endpoint: str, index: str, vector: List[float],
                       k: int, filter_terms: Dict[str, Any] | None = None,
                       ) -> List[Dict[str, Any]]:
            results = self.docs
            if filter_terms:
                results = [
                    d for d in results
                    if all(d["metadata"].get(k) == v for k, v in filter_terms.items())
                ]
            return sorted(results, key=lambda d: d["score"], reverse=True)[:k]

    return _Knn()


@pytest.fixture
def fake_athena():
    """boto3 Athena client wrapped in a Stubber primed for SUCCESS + 1 row."""
    client = boto3.client("athena", region_name="us-east-1")
    stub = Stubber(client)
    stub.add_response(
        "start_query_execution", {"QueryExecutionId": "qid-1"},
    )
    stub.add_response(
        "get_query_execution",
        {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}},
        expected_params={"QueryExecutionId": "qid-1"},
    )
    stub.add_response(
        "get_query_results",
        {
            "ResultSet": {
                "ResultSetMetadata": {
                    "ColumnInfo": [{"Name": "x", "Type": "varchar"}],
                },
                "Rows": [
                    {"Data": [{"VarCharValue": "x"}]},
                    {"Data": [{"VarCharValue": "1"}]},
                ],
            },
        },
        expected_params={"QueryExecutionId": "qid-1"},
    )
    stub.activate()
    yield client
    stub.deactivate()
