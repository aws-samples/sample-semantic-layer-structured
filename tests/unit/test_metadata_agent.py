"""
Unit Tests for Metadata Generation Agent
Tests basic functionality with mock data (no infrastructure required)

Run locally:
    cd /Users/huthmac/Documents/AWS/00_workspace/semantic-layer
    python tests/unit/test_metadata_agent.py
    # or via pytest:
    pytest tests/unit/test_metadata_agent.py -v
"""

import inspect
import json
import os
import sys

# Add agents directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))


def test_1_imports():
    """Test 1: Verify all imports work"""
    print("\n=== Test 1: Imports ===")
    try:
        from metadata_agent.main import (
            get_database_tables,
            get_table_schema,
            sample_table_data,
            update_glue_table_metadata,
            update_glue_database_description,
            save_metadata_document_to_s3,
            update_progress,
            create_metadata_agent,
            invoke,
        )
        print("✅ All main functions imported successfully")

        from shared.token_manager import count_tokens, get_token_status
        print("✅ token_manager imported successfully")

        token_count = count_tokens("Hello world")
        assert token_count > 0, "Token counting must return a positive value"
        print(f"✅ Token counting works: {token_count} tokens")

        return True
    except Exception as e:
        print(f"❌ Import failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_2_token_manager():
    """Test 2: Test token management utilities"""
    print("\n=== Test 2: Token Manager ===")
    try:
        from shared.token_manager import count_tokens, get_token_status

        test_cases = [
            ("", 0),
            ("Hello", 1),
            ("Hello world", 2),
            ("This is a longer test string with multiple words", 9),
        ]

        for text, expected_min in test_cases:
            count = count_tokens(text)
            if text == "":
                assert count == 0, f"Empty string should return 0 tokens, got {count}"
            else:
                assert count > 0, f"Non-empty string should return positive token count, got {count}"
            print(f"✅ '{text[:30]}...' → {count} tokens")

        status = get_token_status(1000, 150000)
        assert 'current' in status or 'tokens' in status or isinstance(status, dict), \
            "get_token_status must return a dict"
        print("✅ get_token_status works")

        return True
    except Exception as e:
        print(f"❌ Token manager test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_3_tool_definitions():
    """Test 3: Verify all 7 tools are defined and callable with correct signatures"""
    print("\n=== Test 3: Tool Definitions ===")
    try:
        from metadata_agent.main import (
            get_database_tables,
            get_table_schema,
            sample_table_data,
            update_glue_table_metadata,
            update_glue_database_description,
            save_metadata_document_to_s3,
            update_progress,
        )

        tools = [
            get_database_tables,
            get_table_schema,
            sample_table_data,
            update_glue_table_metadata,
            update_glue_database_description,
            save_metadata_document_to_s3,
            update_progress,
        ]

        for tool in tools:
            assert callable(tool), f"{tool.__name__} must be callable"
            print(f"✅ Tool defined: {tool.__name__}")

        # Verify key signatures
        sig = inspect.signature(get_database_tables)
        params = list(sig.parameters.keys())
        assert 'database_name' in params, "get_database_tables must accept database_name"

        sig = inspect.signature(get_table_schema)
        params = list(sig.parameters.keys())
        assert 'database_name' in params and 'table_name' in params

        sig = inspect.signature(update_glue_table_metadata)
        params = list(sig.parameters.keys())
        assert 'database_name' in params and 'table_name' in params
        assert 'description' in params

        sig = inspect.signature(update_progress)
        params = list(sig.parameters.keys())
        assert 'job_id' in params, \
            "update_progress must accept job_id parameter"
        assert 'tables_processed' in params
        assert 'total_tables' in params

        print("✅ All tool signatures validated")
        return True
    except Exception as e:
        print(f"❌ Tool definitions test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_4_invoke_signature():
    """Test 4: Verify invoke entrypoint has correct (payload, context) signature"""
    print("\n=== Test 4: invoke Signature ===")
    try:
        from metadata_agent.main import invoke

        sig = inspect.signature(invoke)
        params = list(sig.parameters.keys())

        assert 'payload' in params and 'context' in params, \
            f"invoke signature mismatch — expected (payload, context), got {params}"
        print(f"✅ invoke has correct AgentCore signature: {params}")

        return True
    except Exception as e:
        print(f"❌ invoke signature test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_5_update_progress_response_schema():
    """Test 5: Verify update_progress returns structured JSON with expected keys"""
    print("\n=== Test 5: update_progress Response Schema ===")
    try:
        from unittest.mock import MagicMock, patch
        from metadata_agent.main import update_progress

        # Mock DynamoDB to avoid real AWS calls
        mock_table = MagicMock()
        mock_table.update_item.return_value = {}

        with patch('metadata_agent.main.get_boto_session') as mock_session:
            mock_resource = MagicMock()
            mock_resource.Table.return_value = mock_table
            mock_session.return_value.resource.return_value = mock_resource

            result_str = update_progress(
                job_id="test-job-001",
                tables_processed=5,
                total_tables=10,
                current_table="policy_master",
            )

        result = json.loads(result_str)
        assert 'success' in result or 'status' in result or 'error' not in result, \
            f"update_progress must return structured JSON, got: {result}"
        print(f"✅ update_progress returns valid JSON: {list(result.keys())}")

        return True
    except Exception as e:
        print(f"❌ update_progress schema test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_6_agent_creation():
    """Test 6: Verify metadata agent can be created"""
    print("\n=== Test 6: Agent Creation ===")
    try:
        from metadata_agent.main import create_metadata_agent
        agent = create_metadata_agent()
        print(f"✅ Metadata agent created successfully")
        assert callable(agent), "Agent must be callable"
        print(f"✅ Agent is callable")

        return True
    except Exception as e:
        print(f"❌ Agent creation failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_7_catalog_routing():
    """Test 7: Verify per-table catalog routing helper works"""
    print("\n=== Test 7: Catalog Routing ===")
    try:
        import metadata_agent.main as m

        # Inject a table catalog mapping directly
        m._table_catalogs = {
            "insurance_db.policy_master": "AWSDataCatalog",
            "insurance_db.s3_table": "s3tablescatalog/my-bucket",
        }
        m._catalog_id = "AWSDataCatalog"  # fallback

        catalog = m._get_catalog_for_table("insurance_db", "policy_master")
        assert catalog == "AWSDataCatalog", f"Expected AWSDataCatalog, got {catalog}"
        print("✅ Glue catalog resolved correctly")

        catalog = m._get_catalog_for_table("insurance_db", "s3_table")
        assert catalog == "s3tablescatalog/my-bucket", f"Expected s3tablescatalog/my-bucket, got {catalog}"
        print("✅ S3 Tables catalog resolved correctly")

        # Fallback for unknown table
        catalog = m._get_catalog_for_table("insurance_db", "unknown_table")
        assert catalog == "AWSDataCatalog", f"Expected fallback AWSDataCatalog, got {catalog}"
        print("✅ Fallback catalog used for unknown table")

        # Clean up
        m._table_catalogs = {}

        return True
    except Exception as e:
        print(f"❌ Catalog routing test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_revision_mode_writes_new_version_and_marks_previous_inactive():
    """When revisionMode=True, _write_versioned_completion writes the new version
    record via put_item and marks the previous version inactive via update_item."""
    from unittest.mock import MagicMock, patch, call

    put_calls = []

    def fake_put_item(Item):
        put_calls.append(dict(Item))

    mock_table = MagicMock()
    mock_table.put_item.side_effect = fake_put_item

    with patch('metadata_agent.main.get_boto_session') as mock_session:
        mock_session.return_value.resource.return_value.Table.return_value = mock_table
        from metadata_agent.main import _write_versioned_completion

        config = {
            'id': 'abc', 'version': 'v1', 'status': 'completed',
            'revisionMode': True, 'targetVersion': 'v2',
            'revisionInstructions': [{'comment': 'fix'}],
            'revisionBaseVersion': 'v1',
        }
        _write_versioned_completion('abc', config, 'v2', 'Processed 3/3 tables.')

    # Only one put_item call — the new active version record
    assert len(put_calls) == 1
    new_record = put_calls[0]
    assert new_record['version'] == 'v2'
    assert new_record['status'] == 'completed'
    assert new_record.get('revisionMode') is False
    assert 'revisionInstructions' not in new_record
    assert 'targetVersion' not in new_record
    assert 'currentVersion' not in new_record

    # One update_item call — marks the previous version (v1) as inactive
    assert mock_table.update_item.call_count == 1
    update_kwargs = mock_table.update_item.call_args[1]
    assert update_kwargs['Key'] == {'id': 'abc', 'version': 'v1'}
    assert ':inactive' in update_kwargs['ExpressionAttributeValues']
    assert update_kwargs['ExpressionAttributeValues'][':inactive'] == 'inactive'


def run_all_tests():
    """Run all tests"""
    print("=" * 60)
    print("Metadata Generation Agent - Test Suite")
    print("=" * 60)

    tests = [
        test_1_imports,
        test_2_token_manager,
        test_3_tool_definitions,
        test_4_invoke_signature,
        test_5_update_progress_response_schema,
        test_6_agent_creation,
        test_7_catalog_routing,
        test_revision_mode_writes_new_version_and_marks_previous_inactive,
    ]

    results = []
    for test in tests:
        try:
            result = test()
            results.append(result)
        except Exception as e:
            print(f"❌ Test crashed: {str(e)}")
            results.append(False)

    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"Passed: {passed}/{total}")
    print(f"Failed: {total - passed}/{total}")

    if passed == total:
        print("\n✅ All tests passed!")
        return 0
    else:
        print("\n❌ Some tests failed")
        return 1


if __name__ == "__main__":
    exit_code = run_all_tests()
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# S3 Tables VersionId tests
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch
import json as _json


def _glue_table_response(version_id=None):
    tbl = {
        "Name": "orders",
        "StorageDescriptor": {"Columns": [{"Name": "order_id", "Type": "string"}]},
        "PartitionKeys": [],
    }
    if version_id:
        tbl["VersionId"] = version_id
    return {"Table": tbl}


@patch("metadata_agent.main.get_boto_session")
def test_s3tables_fetches_version_token(mock_session):
    """For s3tablescatalog/ catalogs, versionToken is fetched on retry when Glue
    federation raises FederationSourceException with 'versionToken null'."""
    import metadata_agent.main as m
    glue = MagicMock()
    glue.get_table.return_value = _glue_table_response(version_id=None)
    # First update_table call simulates the federation error; second succeeds.
    glue.update_table.side_effect = [
        Exception("FederationSourceException: versionToken null"),
        None,
    ]
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    s3tables = MagicMock()
    s3tables.get_table.return_value = {"versionToken": "tok-abc"}

    def client_factory(service, **kwargs):
        return {"glue": glue, "sts": sts, "s3tables": s3tables}[service]

    mock_session.return_value.client.side_effect = client_factory
    mock_session.return_value.region_name = "us-east-1"

    result = _json.loads(m.update_glue_table_metadata(
        database_name="ns1",
        table_name="orders",
        table_description="Order records",
        column_descriptions='{"order_id": "Unique order identifier"}',
        catalog_id="s3tablescatalog/my-bucket",
    ))

    assert result["success"] is True
    assert glue.update_table.call_count == 2  # initial attempt + retry with token
    _, update_kwargs = glue.update_table.call_args  # inspect the retry call
    assert update_kwargs.get("VersionId") == "tok-abc"


@patch("metadata_agent.main.get_boto_session")
def test_s3tables_api_failure_is_nonfatal(mock_session):
    """If S3 Tables API fails, update_table() still proceeds without VersionId."""
    import metadata_agent.main as m
    glue = MagicMock()
    glue.get_table.return_value = _glue_table_response(version_id=None)
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    s3tables = MagicMock()
    s3tables.get_table.side_effect = Exception("S3 Tables API error")

    def client_factory(service, **kwargs):
        return {"glue": glue, "sts": sts, "s3tables": s3tables}[service]

    mock_session.return_value.client.side_effect = client_factory
    mock_session.return_value.region_name = "us-east-1"

    result = _json.loads(m.update_glue_table_metadata(
        database_name="ns1",
        table_name="orders",
        table_description="Order records",
        column_descriptions='{"order_id": "Unique order identifier"}',
        catalog_id="s3tablescatalog/my-bucket",
    ))

    assert result["success"] is True
    glue.update_table.assert_called_once()
    _, update_kwargs = glue.update_table.call_args
    assert "VersionId" not in update_kwargs


@patch("metadata_agent.main.get_boto_session")
def test_s3tables_empty_version_token_skipped(mock_session):
    """If versionToken is None, VersionId is not added to update_kwargs."""
    import metadata_agent.main as m
    glue = MagicMock()
    glue.get_table.return_value = _glue_table_response(version_id=None)
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    s3tables = MagicMock()
    s3tables.get_table.return_value = {"versionToken": None}

    def client_factory(service, **kwargs):
        return {"glue": glue, "sts": sts, "s3tables": s3tables}[service]

    mock_session.return_value.client.side_effect = client_factory
    mock_session.return_value.region_name = "us-east-1"

    result = _json.loads(m.update_glue_table_metadata(
        database_name="ns1",
        table_name="orders",
        table_description="Order records",
        column_descriptions='{"order_id": "Unique order identifier"}',
        catalog_id="s3tablescatalog/my-bucket",
    ))

    assert result["success"] is True
    _, update_kwargs = glue.update_table.call_args
    assert "VersionId" not in update_kwargs


@patch("metadata_agent.main.get_boto_session")
def test_zero_column_glue_table_returns_error_not_nameerror(mock_session):
    """A Glue table with an empty StorageDescriptor.Columns (empty table / view)
    after an Athena DESCRIBE failure must return a clean error JSON — NOT raise
    NameError on the unbound `glue_error_val`. Regression for the fall-through
    path where `if columns:` is False and the Glue `except` never ran."""
    import metadata_agent.main as m
    glue = MagicMock()
    # Glue get_table SUCCEEDS but the table has zero columns and no partitions.
    glue.get_table.return_value = {
        "Table": {
            "Name": "empty_view",
            "StorageDescriptor": {"Columns": []},
            "PartitionKeys": [],
        }
    }
    # Athena DESCRIBE fails so we fall through to the Glue branch.
    athena = MagicMock()
    athena.start_query_execution.side_effect = Exception("DESCRIBE failed")

    def client_factory(service, **kwargs):
        return {"glue": glue, "athena": athena}[service]

    mock_session.return_value.client.side_effect = client_factory
    mock_session.return_value.region_name = "us-east-1"

    # Standard (non-s3tables) catalog so the Athena path is attempted first.
    result = _json.loads(m.get_single_table_schema(
        database_name="ns1",
        table_name="empty_view",
        catalog_id="AWSDataCatalog",
    ))

    assert "error" in result
    assert result["table_name"] == "empty_view"


@patch("metadata_agent.main.get_boto_session")
def test_describe_paginates_get_query_results(mock_session):
    """A wide table (>1000 columns) yields >1 page of DESCRIBE rows. The fetch
    must loop NextToken and accumulate ALL pages, else the schema is silently
    truncated. Page 1 carries the header row (index 0, skipped by [1:]); later
    pages do not. Regression for the single-call truncation bug."""
    import metadata_agent.main as m
    glue = MagicMock()
    # Best-effort Glue description lookup must return JSON-serializable data.
    glue.get_table.return_value = {"Table": {"Description": "wide table"}}
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "q-1"}
    athena.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}},
    }

    def _row(name, typ):
        return {"Data": [{"VarCharValue": name}, {"VarCharValue": typ}]}

    # Page 1: header row + one column def; Page 2: one more column def, no token.
    page_1 = {
        "ResultSet": {
            "Rows": [
                _row("col_name", "data_type"),  # DESCRIBE header (page 1 only)
                _row("policy_id", "string"),
            ]
        },
        "NextToken": "tok-2",
    }
    page_2 = {
        "ResultSet": {
            "Rows": [
                _row("premium", "double"),
            ]
        },
    }
    athena.get_query_results.side_effect = [page_1, page_2]

    def client_factory(service, **kwargs):
        return {"glue": glue, "athena": athena}[service]

    mock_session.return_value.client.side_effect = client_factory
    mock_session.return_value.region_name = "us-east-1"

    result = _json.loads(m.get_single_table_schema(
        database_name="ns1",
        table_name="wide_table",
        catalog_id="AWSDataCatalog",
    ))

    # BOTH column defs survive across pages; the page-1 header is skipped once.
    assert result["source"] == "athena_describe"
    assert [c["name"] for c in result["columns"]] == ["policy_id", "premium"]
    assert athena.get_query_results.call_count == 2
    _, second_kwargs = athena.get_query_results.call_args_list[1]
    assert second_kwargs["NextToken"] == "tok-2"


def test_annotation_mode_requires_target_version():
    """Revision mode must fail loud (KeyError) when targetVersion is absent —
    never write a None-keyed record. Mirrors ontology_agent._run_revision_mode."""
    import metadata_agent.main as m
    import pytest as _pytest
    with _pytest.raises(KeyError):
        m._run_annotation_mode(
            job_id="job-1",
            config={},  # no targetVersion
            tables_list=[],
            annotations=[],
            build_started_at="2026-07-02T00:00:00Z",
        )


@patch("metadata_agent.main.get_boto_session")
def test_partition_columns_not_double_counted(mock_session):
    """I2: Athena DESCRIBE repeats partition columns under '# Partition
    Information'. The parse must dedup by name so the metadata agent's column SET
    matches what the delta planner hashes (GlueService columns + partitionKeys,
    each once) — otherwise a partitioned table is permanently schemaChanged."""
    import metadata_agent.main as m

    def _row(*vals):
        return {"Data": [{"VarCharValue": v} for v in vals]}

    describe_rows = [
        _row("col_name", "data_type", "comment"),   # result-set header (skipped)
        _row("id", "string", "pk"),
        _row("amount", "double", ""),
        _row("dt", "date", "partition col"),        # partition col in main list
        _row("", "", ""),                            # blank separator
        _row("# Partition Information", "", ""),      # section header
        _row("# col_name", "data_type", "comment"),  # section's own header
        _row("dt", "date", "partition col"),         # REPEATED partition col
    ]
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "q1"}
    athena.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
    }
    athena.get_query_results.return_value = {"ResultSet": {"Rows": describe_rows}}
    glue = MagicMock()
    glue.get_table.return_value = {"Table": {"Description": ""}}

    def client_factory(service, **kwargs):
        return {"glue": glue, "athena": athena}[service]

    mock_session.return_value.client.side_effect = client_factory
    mock_session.return_value.region_name = "us-east-1"

    result = _json.loads(m.get_single_table_schema(
        database_name="ns1", table_name="orders", catalog_id="AWSDataCatalog",
    ))
    names = [c["name"] for c in result["columns"]]
    assert names == ["id", "amount", "dt"]   # dt exactly once
    assert result["total_columns"] == 3
