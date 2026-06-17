import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

from ontology_agent.main import _build_tables_list


def test_glue_coords_passed_through():
    ds = {
        "databaseName": "default",
        "tableName": "semantic-layer-admin-codes",
        "catalogId": "dynamodb_catalog",
        "dataSource": "dynamodb_catalog",
        "tableId": "dynamodb_catalog::default.semantic-layer-admin-codes",
        "glueDatabaseName": "semantic_layer_dynamodb",
        "glueTableName": "semantic_layer_admin_codes",
    }
    result = _build_tables_list([ds])
    assert result[0]["glueDatabaseName"] == "semantic_layer_dynamodb"
    assert result[0]["glueTableName"] == "semantic_layer_admin_codes"


def test_glue_coords_absent_when_not_set():
    ds = {
        "databaseName": "semantic_layer_iceberg",
        "tableName": "admincode",
        "catalogId": "s3tablescatalog/my-bucket",
        "dataSource": "AwsDataCatalog",
        "tableId": "s3tablescatalog/my-bucket::semantic_layer_iceberg.admincode",
    }
    result = _build_tables_list([ds])
    assert result[0].get("glueDatabaseName", "") == ""
    assert result[0].get("glueTableName", "") == ""
