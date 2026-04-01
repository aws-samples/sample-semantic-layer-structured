import json
import os
import sys
from unittest.mock import MagicMock, patch, mock_open

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

from ontology_agent.main import update_glue_metadata_from_ontology

# Minimal nquads fixture that the parser can extract one column comment from
NQUADS = (
    '<http://example.org/ns/AdminCodes> '
    '<http://www.w3.org/2000/01/rdf-schema#comment> '
    '"Admin codes table" <http://example.org/ns> .\n'
    '<http://example.org/ns/AdminCodes/code_value> '
    '<http://www.w3.org/2000/01/rdf-schema#comment> '
    '"The code value" <http://example.org/ns> .\n'
    '<http://example.org/ns/AdminCodes/code_value> '
    '<https://semantic-layer.aws/virtual-kg/mapsToColumn> '
    '"semantic_layer_admin_codes.code_value" <http://example.org/ns> .\n'
    '<http://example.org/ns/AdminCodes> '
    '<https://semantic-layer.aws/virtual-kg/mapsToTable> '
    '"semantic_layer_dynamodb.semantic_layer_admin_codes" <http://example.org/ns> .\n'
)


def _make_glue_table_response(col_names):
    return {
        "Table": {
            "StorageDescriptor": {
                "Columns": [{"Name": n, "Type": "string"} for n in col_names]
            }
        }
    }


@patch("ontology_agent.main.get_boto_session")
@patch("os.path.isdir", return_value=True)
@patch("os.listdir", return_value=["admincode.md"])
def test_uses_glue_coords_when_provided(mock_ls, mock_isdir, mock_session):
    """When glue_database_name/glue_table_name are supplied, Glue is called with them."""
    file_content = "**Table:** semantic-layer-admin-codes\n```nquads\n" + NQUADS + "```"

    glue_client = MagicMock()
    glue_client.get_table.return_value = _make_glue_table_response(["code_value"])
    mock_session.return_value.client.return_value = glue_client

    with patch("builtins.open", mock_open(read_data=file_content)):
        result = json.loads(update_glue_metadata_from_ontology(
            ontology_id="test-001",
            database_name="default",
            table_name="semantic-layer-admin-codes",
            catalog_id="dynamodb_catalog",
            glue_database_name="semantic_layer_dynamodb",
            glue_table_name="semantic_layer_admin_codes",
        ))

    glue_client.get_table.assert_called_once_with(
        DatabaseName="semantic_layer_dynamodb",
        Name="semantic_layer_admin_codes",
    )
    assert result["success"] is True
    assert result["columns_updated"] == 1


@patch("ontology_agent.main.get_boto_session")
@patch("os.path.isdir", return_value=True)
@patch("os.listdir", return_value=["admincode.md"])
def test_update_glue_skips_preemptive_version_token_fetch(mock_ls, mock_isdir, mock_session):
    """_fetch_s3tables_version_token is NOT called before the first update_table attempt."""
    fetch_calls = []

    def fake_fetch(session, catalog_id, db_name, table_name):
        fetch_calls.append((catalog_id, db_name, table_name))
        return 'tok-123'

    file_content = "**Table:** semantic-layer-admin-codes\n```nquads\n" + NQUADS + "```"

    glue_client = MagicMock()
    # update_table succeeds on first try — no versionToken error
    glue_client.get_table.return_value = _make_glue_table_response(["code_value"])
    mock_session.return_value.client.return_value = glue_client

    with patch("builtins.open", mock_open(read_data=file_content)), \
         patch("ontology_agent.main._fetch_s3tables_version_token", side_effect=fake_fetch):
        result = json.loads(update_glue_metadata_from_ontology(
            ontology_id="test-001",
            database_name="semantic_layer_dynamodb",
            table_name="semantic-layer-admin-codes",
            catalog_id="s3tablescatalog/test-catalog",
        ))

    # _fetch_s3tables_version_token must NOT have been called (no preemptive inject)
    # when the first update_table succeeds
    assert fetch_calls == [], f"_fetch_s3tables_version_token was called preemptively: {fetch_calls}"
    assert result["success"] is True
    assert result["columns_updated"] == 1
