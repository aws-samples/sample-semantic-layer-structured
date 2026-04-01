"""
Unit tests for sample_table_data DynamoDB ARN auto-detection and re-routing.
"""

import json
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))


def _make_athena_mock(state="SUCCEEDED"):
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "qid-123"}
    athena.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": state}}
    }
    athena.get_query_results.return_value = {
        "ResultSet": {
            "ResultSetMetadata": {
                "ColumnInfo": [{"Label": "id"}, {"Label": "name"}]
            },
            "Rows": [
                # header row (skipped)
                {"Data": [{"VarCharValue": "id"}, {"VarCharValue": "name"}]},
                # data row
                {"Data": [{"VarCharValue": "abc123"}, {"VarCharValue": "test-item"}]},
            ],
        }
    }
    return athena


def test_sample_table_data_reroutes_dynamodb_arn_table():
    """When Glue Location is a DynamoDB ARN, query is re-routed through dynamodb_catalog."""
    from ontology_agent.main import sample_table_data

    with patch("ontology_agent.main.get_boto_session") as mock_sess:
        glue = MagicMock()
        glue.get_table.return_value = {
            "Table": {
                "StorageDescriptor": {
                    "Location": "arn:aws:dynamodb:us-east-1:123456789012:table/semantic-layer-admin-codes"
                }
            }
        }
        athena = _make_athena_mock()

        def client_factory(svc, **kw):
            if svc == "glue":
                return glue
            return athena

        mock_sess.return_value.client.side_effect = client_factory

        with patch.dict(os.environ, {
            "ATHENA_OUTPUT_LOCATION": "s3://test-bucket/athena/",
            "DYNAMODB_CONNECTOR_CATALOG": "dynamodb_catalog",
        }):
            result = json.loads(
                sample_table_data(
                    "semantic_layer_dynamodb",
                    "semantic_layer_admin_codes",
                    "AWSDataCatalog",
                )
            )

        # Glue was queried to resolve the location
        glue.get_table.assert_called_once_with(
            DatabaseName="semantic_layer_dynamodb",
            Name="semantic_layer_admin_codes",
        )

        # Athena query used connector coordinates
        call_kwargs = athena.start_query_execution.call_args.kwargs
        assert '"default"."semantic-layer-admin-codes"' in call_kwargs["QueryString"]
        assert call_kwargs["QueryExecutionContext"]["Catalog"] == "dynamodb_catalog"
        assert call_kwargs["QueryExecutionContext"]["Database"] == "default"

        assert result["success"] is True
        assert result["database_name"] == "default"
        assert result["table_name"] == "semantic-layer-admin-codes"
        assert result["columns"] == ["id", "name"]


def test_sample_table_data_normal_glue_table_unchanged():
    """Non-DynamoDB Glue tables (S3-backed) are queried directly without re-routing."""
    from ontology_agent.main import sample_table_data

    with patch("ontology_agent.main.get_boto_session") as mock_sess:
        glue = MagicMock()
        glue.get_table.return_value = {
            "Table": {
                "StorageDescriptor": {
                    "Location": "s3://my-bucket/data/my_table/"
                }
            }
        }
        athena = _make_athena_mock()

        def client_factory(svc, **kw):
            if svc == "glue":
                return glue
            return athena

        mock_sess.return_value.client.side_effect = client_factory

        with patch.dict(os.environ, {
            "ATHENA_OUTPUT_LOCATION": "s3://test-bucket/athena/",
        }):
            result = json.loads(
                sample_table_data("my_database", "my_table", "AWSDataCatalog")
            )

        # Query uses original coordinates
        call_kwargs = athena.start_query_execution.call_args.kwargs
        assert '"my_database"."my_table"' in call_kwargs["QueryString"]
        # AWSDataCatalog should NOT add a Catalog key to query context
        assert "Catalog" not in call_kwargs["QueryExecutionContext"]

        assert result["success"] is True
        assert result["database_name"] == "my_database"
        assert result["table_name"] == "my_table"


def test_sample_table_data_glue_lookup_failure_falls_back():
    """If the Glue lookup raises, sampling proceeds with original coordinates."""
    from ontology_agent.main import sample_table_data

    with patch("ontology_agent.main.get_boto_session") as mock_sess:
        glue = MagicMock()
        glue.get_table.side_effect = Exception("Glue unavailable")
        athena = _make_athena_mock()

        def client_factory(svc, **kw):
            if svc == "glue":
                return glue
            return athena

        mock_sess.return_value.client.side_effect = client_factory

        with patch.dict(os.environ, {
            "ATHENA_OUTPUT_LOCATION": "s3://test-bucket/athena/",
        }):
            result = json.loads(
                sample_table_data("my_database", "my_table", "AWSDataCatalog")
            )

        # Falls back to original coordinates
        call_kwargs = athena.start_query_execution.call_args.kwargs
        assert '"my_database"."my_table"' in call_kwargs["QueryString"]
        assert result["success"] is True


def test_sample_table_data_s3tables_catalog_skips_glue_lookup():
    """S3 Tables catalogs skip the Glue ARN detection (catalog_id is not AWSDataCatalog)."""
    from ontology_agent.main import sample_table_data

    with patch("ontology_agent.main.get_boto_session") as mock_sess:
        glue = MagicMock()
        athena = _make_athena_mock()

        def client_factory(svc, **kw):
            if svc == "glue":
                return glue
            return athena

        mock_sess.return_value.client.side_effect = client_factory

        with patch.dict(os.environ, {
            "ATHENA_OUTPUT_LOCATION": "s3://test-bucket/athena/",
        }):
            result = json.loads(
                sample_table_data(
                    "my_namespace",
                    "my_iceberg_table",
                    "s3tablescatalog/my-bucket",
                )
            )

        # Glue was NOT called (non-AWSDataCatalog catalog)
        glue.get_table.assert_not_called()

        # S3 Tables catalog forwarded in query context
        call_kwargs = athena.start_query_execution.call_args.kwargs
        assert call_kwargs["QueryExecutionContext"]["Catalog"] == "s3tablescatalog/my-bucket"
        assert result["success"] is True
