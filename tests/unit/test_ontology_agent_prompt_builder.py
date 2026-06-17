import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

from ontology_agent.prompt_builder import build_phase2_table_prompt

TABLE_INFO_DYNAMO = {
    "database": "default",
    "table": "semantic-layer-admin-codes",
    "catalogId": "dynamodb_catalog",
    "dataSource": "dynamodb_catalog",
    "tableId": "dynamodb_catalog::default.semantic-layer-admin-codes",
    "glueDatabaseName": "semantic_layer_dynamodb",
    "glueTableName": "semantic_layer_admin_codes",
}


def test_phase2_prompt_includes_glue_coords_for_dynamodb():
    prompt = build_phase2_table_prompt(
        ontology_id="test-001",
        namespace="https://semantic-layer.aws/vkg/test-001",
        table_info=TABLE_INFO_DYNAMO,
        fk_relationships=[],
    )
    assert 'glue_database_name="semantic_layer_dynamodb"' in prompt
    assert 'glue_table_name="semantic_layer_admin_codes"' in prompt


def test_phase2_prompt_no_glue_coords_for_s3tables():
    table_info = {
        "database": "semantic_layer_iceberg",
        "table": "admincode",
        "catalogId": "s3tablescatalog/my-bucket",
        "dataSource": "AwsDataCatalog",
        "tableId": "s3tablescatalog/my-bucket::semantic_layer_iceberg.admincode",
    }
    prompt = build_phase2_table_prompt(
        ontology_id="test-002",
        namespace="https://semantic-layer.aws/vkg/test-002",
        table_info=table_info,
        fk_relationships=[],
    )
    assert "glue_database_name" not in prompt
    assert "glue_table_name" not in prompt
