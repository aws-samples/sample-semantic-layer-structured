"""Unit tests for the Tier 1 Athena executor."""
from unittest.mock import MagicMock

import pytest

from agents.shared.metric_executor import _build_query_context, execute_metric


def _succeeding_athena() -> MagicMock:
    """Return an Athena mock whose query SUCCEEDS with a single row."""
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "q-1"}
    athena.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}},
    }
    athena.get_query_results.return_value = {
        "ResultSet": {
            "Rows": [
                {"Data": [{"VarCharValue": "n"}]},
                {"Data": [{"VarCharValue": "1"}]},
            ],
            "ResultSetMetadata": {"ColumnInfo": [{"Name": "n"}]},
        }
    }
    return athena


def test_execute_uses_compiled_sql_and_returns_rows():
    metric = MagicMock(
        metric_id="x", compiled_sql="SELECT 1 AS n", dialect="athena",
        supported_filters=[], supported_dimensions=[],
    )
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "q-1"}
    athena.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}},
    }
    athena.get_query_results.return_value = {
        "ResultSet": {
            "Rows": [
                {"Data": [{"VarCharValue": "n"}]},
                {"Data": [{"VarCharValue": "1"}]},
            ],
            "ResultSetMetadata": {"ColumnInfo": [{"Name": "n"}]},
        }
    }

    out = execute_metric(
        metric=metric, filters={}, athena=athena,
        workgroup="wg", output_loc="s3://bkt/",
    )
    assert out["columns"] == ["n"]
    assert out["rows"] == [{"n": "1"}]
    athena.start_query_execution.assert_called_once()


def test_execute_rejects_unknown_filter():
    metric = MagicMock(
        metric_id="x", compiled_sql="SELECT 1", dialect="athena",
        supported_filters=["region"], supported_dimensions=[],
    )
    with pytest.raises(ValueError, match="unsupported filter"):
        execute_metric(
            metric=metric, filters={"year": "2024"},
            athena=MagicMock(), workgroup="wg", output_loc="s3://bkt/",
        )


def test_build_query_context_names_federated_catalog():
    """S3 Tables catalog MUST be named so the schema resolves (the bug fix)."""
    ctx = _build_query_context(
        catalog_id="s3tablescatalog/my-bucket", database_name="normalized",
    )
    assert ctx == {"Database": "normalized", "Catalog": "s3tablescatalog/my-bucket"}


def test_build_query_context_omits_default_glue_catalog():
    """Default Glue catalog needs only the database — no Catalog key."""
    assert _build_query_context(catalog_id="AwsDataCatalog", database_name="db") == {
        "Database": "db",
    }
    assert _build_query_context(catalog_id="", database_name="db") == {"Database": "db"}
    assert _build_query_context(catalog_id="", database_name="") == {}


def test_execute_passes_federated_catalog_into_query_context():
    """Regression: a metric over an S3 Tables layer must run with the catalog
    in the QueryExecutionContext, else Athena fails SCHEMA_NOT_FOUND."""
    metric = MagicMock(
        metric_id="cash_value_per_policy",
        compiled_sql="SELECT cash_value FROM normalized.holding",
        dialect="athena", supported_filters=[], supported_dimensions=[],
    )
    athena = _succeeding_athena()

    execute_metric(
        metric=metric, filters={}, athena=athena,
        workgroup="wg", output_loc="s3://bkt/",
        catalog_id="s3tablescatalog/my-bucket", database_name="normalized",
    )

    _, kwargs = athena.start_query_execution.call_args
    assert kwargs["QueryExecutionContext"] == {
        "Database": "normalized", "Catalog": "s3tablescatalog/my-bucket",
    }


def test_execute_omits_result_configuration_when_no_output_loc():
    """Empty output_loc ⇒ defer to the workgroup's enforced result location."""
    metric = MagicMock(
        metric_id="x", compiled_sql="SELECT 1 AS n", dialect="athena",
        supported_filters=[], supported_dimensions=[],
    )
    athena = _succeeding_athena()

    execute_metric(
        metric=metric, filters={}, athena=athena,
        workgroup="wg", output_loc="",
    )

    _, kwargs = athena.start_query_execution.call_args
    assert "ResultConfiguration" not in kwargs
