"""
Unit tests for get_single_table_schema, sample_table_data,
read_local_nquads_file / update_nquads_in_file (exact-match fix),
append_fk_triples, and persist_file_to_neptune.

Focus: verify both functions route ALL catalog types (including
s3tablescatalog/...) through Athena — no Glue fallback, no early-return.

Run locally:
    cd /Users/huthmac/Documents/AWS/00_workspace/semantic-layer
    pytest tests/unit/test_ontology_athena_tools.py -v
"""
import inspect
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, call, patch

import boto3
import pytest

# Add agents directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

# ---------------------------------------------------------------------------
# Helpers to build fake Athena API responses
# ---------------------------------------------------------------------------

def _execution_response(qid: str = "test-qid-001") -> dict:
    return {"QueryExecutionId": qid}


def _status_response(state: str, reason: str = "") -> dict:
    s = {"State": state}
    if reason:
        s["StateChangeReason"] = reason
    return {"QueryExecution": {"Status": s}}


def _describe_results(rows: list[tuple[str, str, str]]) -> dict:
    """
    Build a fake Athena get_query_results response for a DESCRIBE TABLE query.

    Each row is (col_name, col_type, comment).  The real Athena output also
    contains section-header rows like ('# col_name', 'col_type', '') which
    must be filtered out by the code.
    """
    def _row(*values):
        return {"Data": [{"VarCharValue": v} for v in values]}

    athena_rows = [_row(*r) for r in rows]
    return {"ResultSet": {"Rows": athena_rows}}


def _select_results(columns: list[str], data_rows: list[list[str]]) -> dict:
    """Build a fake Athena get_query_results response for a SELECT query."""
    col_info = [{"Label": c} for c in columns]

    def _row(*values):
        return {"Data": [{"VarCharValue": v} for v in values]}

    header = _row(*columns)
    rows = [_row(*r) for r in data_rows]
    return {
        "ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": col_info},
            "Rows": [header] + rows,
        }
    }


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

S3_CATALOG = "s3tablescatalog/semantic-layer-analytics-tables"
GLUE_CATALOG = "AWSDataCatalog"
DB = "semantic_layer_iceberg"
TABLE = "admincode"
ARTIFACTS_BUCKET = "semantic-layer-artifacts-test"
QID = "mock-query-execution-id"


def _make_athena_mock(qid: str = QID, final_state: str = "SUCCEEDED",
                      query_results: dict | None = None) -> MagicMock:
    """Return a MagicMock that mimics the boto3 Athena client."""
    athena = MagicMock()
    athena.start_query_execution.return_value = _execution_response(qid)
    athena.get_query_execution.return_value = _status_response(final_state)
    if query_results is not None:
        athena.get_query_results.return_value = query_results
    return athena


def _inject_session(athena_mock: MagicMock, monkeypatch) -> None:
    """Inject a boto3 session whose 'athena' client is the supplied mock.

    The Glue client is given a separate mock that returns a standard S3 location
    so sample_table_data's DynamoDB-ARN detection does not trigger for standard
    catalog tests.
    """
    session = MagicMock(spec=boto3.Session)
    glue_mock = MagicMock()
    glue_mock.get_table.return_value = {
        'Table': {'StorageDescriptor': {'Location': 's3://bucket/prefix/'}}
    }

    def _client_factory(service_name, **kwargs):
        if service_name == 'glue':
            return glue_mock
        return athena_mock

    session.client.side_effect = _client_factory
    # Import here so the module is fully loaded before patching
    from ontology_agent import main as m
    monkeypatch.setattr(m, "_boto_session", session)


# ---------------------------------------------------------------------------
# get_single_table_schema
# ---------------------------------------------------------------------------

class TestGetSingleTableSchema:

    DESCRIBE_ROWS = [
        # Real DESCRIBE output includes a header row with empty types — filtered
        ("col_name", "data_type", "comment"),
        # Section header — must be filtered (starts with '#')
        ("# Partition Information", "", ""),
        # Actual columns
        ("id", "string", "Primary identifier"),
        ("acord.Code", "string", "ACORD code value"),
        ("Deleted", "boolean", "Soft-delete flag"),
        ("aud.CreatedTimestamp", "timestamp", ""),
    ]

    EXPECTED_COLUMNS = [
        {"name": "id", "type": "string", "comment": "Primary identifier"},
        {"name": "acord.Code", "type": "string", "comment": "ACORD code value"},
        {"name": "Deleted", "type": "boolean", "comment": "Soft-delete flag"},
        {"name": "aud.CreatedTimestamp", "type": "timestamp", "comment": ""},
    ]

    def _run(self, catalog_id: str, monkeypatch) -> tuple[dict, MagicMock]:
        from ontology_agent import main as m
        athena = _make_athena_mock(
            query_results=_describe_results(self.DESCRIBE_ROWS)
        )
        _inject_session(athena, monkeypatch)
        monkeypatch.setenv("ARTIFACTS_BUCKET", ARTIFACTS_BUCKET)

        result = json.loads(m.get_single_table_schema(DB, TABLE, catalog_id))
        return result, athena

    def _run_s3tables(self, monkeypatch) -> tuple[dict, MagicMock, MagicMock]:
        """Run get_single_table_schema with S3_CATALOG using separate Glue/Athena mocks."""
        from ontology_agent import main as m

        glue_mock = MagicMock()
        glue_mock.get_table.return_value = {
            "Table": {
                "StorageDescriptor": {
                    "Columns": [
                        {"Name": "id", "Type": "string", "Comment": "Primary identifier"},
                        {"Name": "acord.Code", "Type": "string", "Comment": "ACORD code value"},
                        {"Name": "Deleted", "Type": "boolean", "Comment": "Soft-delete flag"},
                        {"Name": "aud.CreatedTimestamp", "Type": "timestamp", "Comment": ""},
                    ]
                },
                "TableType": "ICEBERG",
                "PartitionKeys": [],
            }
        }
        athena_mock = MagicMock()

        session = MagicMock(spec=boto3.Session)
        session.client.side_effect = (
            lambda svc, **kw: glue_mock if svc == "glue" else athena_mock
        )
        monkeypatch.setattr(m, "_boto_session", session)
        monkeypatch.setenv("ARTIFACTS_BUCKET", ARTIFACTS_BUCKET)

        result = json.loads(m.get_single_table_schema(DB, TABLE, S3_CATALOG))
        return result, athena_mock, glue_mock

    # --- S3 Tables catalog ---
    # DESCRIBE TABLE is DDL; Athena does not support DDL on LF-managed Iceberg tables
    # (https://docs.aws.amazon.com/athena/latest/ug/querying-iceberg.html).
    # get_single_table_schema skips Athena entirely for s3tablescatalog and reads from Glue.

    def test_s3tables_calls_glue_not_athena(self, monkeypatch):
        """Athena DESCRIBE is skipped; Glue.get_table is called instead."""
        _, athena_mock, glue_mock = self._run_s3tables(monkeypatch)
        glue_mock.get_table.assert_called_once()
        athena_mock.start_query_execution.assert_not_called()

    def test_s3tables_glue_receives_catalog_id(self, monkeypatch):
        """CatalogId=s3tablescatalog/... must be forwarded to glue.get_table."""
        _, _, glue_mock = self._run_s3tables(monkeypatch)
        call_kwargs = glue_mock.get_table.call_args[1]
        assert call_kwargs.get("CatalogId") == S3_CATALOG
        assert call_kwargs.get("DatabaseName") == DB
        assert call_kwargs.get("Name") == TABLE

    def test_s3tables_columns_parsed_correctly(self, monkeypatch):
        result, _, _ = self._run_s3tables(monkeypatch)
        assert result["columns"] == self.EXPECTED_COLUMNS
        assert result["total_columns"] == 4

    def test_s3tables_source_is_glue_s3tables(self, monkeypatch):
        result, _, _ = self._run_s3tables(monkeypatch)
        assert result["source"] == "glue_s3tables"

    def test_s3tables_no_athena_query_submitted(self, monkeypatch):
        """No Athena query should be submitted for s3tablescatalog schema lookups."""
        _, athena_mock, _ = self._run_s3tables(monkeypatch)
        athena_mock.start_query_execution.assert_not_called()

    def test_s3tables_database_and_table_in_result(self, monkeypatch):
        result, _, _ = self._run_s3tables(monkeypatch)
        assert result["database_name"] == DB
        assert result["table_name"] == TABLE

    # --- Standard Glue catalog ---

    def test_glue_catalog_no_catalog_in_context(self, monkeypatch):
        """AWSDataCatalog must NOT appear in QueryExecutionContext.Catalog."""
        _, athena = self._run(GLUE_CATALOG, monkeypatch)
        call_kwargs = athena.start_query_execution.call_args[1]
        ctx = call_kwargs["QueryExecutionContext"]
        assert "Catalog" not in ctx

    def test_glue_catalog_columns_parsed(self, monkeypatch):
        result, _ = self._run(GLUE_CATALOG, monkeypatch)
        assert result["total_columns"] == 4
        assert result["source"] == "athena_describe"

    # --- Error path ---

    def test_athena_failure_returns_error_json(self, monkeypatch):
        from ontology_agent import main as m
        athena = _make_athena_mock(final_state="FAILED")
        athena.get_query_execution.return_value = _status_response(
            "FAILED", "SYNTAX_ERROR: line 1:1"
        )
        _inject_session(athena, monkeypatch)
        monkeypatch.setenv("ARTIFACTS_BUCKET", ARTIFACTS_BUCKET)

        result = json.loads(m.get_single_table_schema(DB, TABLE, S3_CATALOG))
        assert "error" in result
        assert result["table_name"] == TABLE


# ---------------------------------------------------------------------------
# sample_table_data
# ---------------------------------------------------------------------------

class TestSampleTableData:

    COLUMNS = ["id", "acord.Code", "Deleted"]
    DATA_ROWS = [
        ["holding#abc-123", "CODE_A", "false"],
        ["holding#def-456", "CODE_B", "false"],
    ]

    def _run(self, catalog_id: str, monkeypatch, sample_size: int = 5) -> tuple[dict, MagicMock]:
        from ontology_agent import main as m
        athena = _make_athena_mock(
            query_results=_select_results(self.COLUMNS, self.DATA_ROWS)
        )
        _inject_session(athena, monkeypatch)
        monkeypatch.setenv("ARTIFACTS_BUCKET", ARTIFACTS_BUCKET)

        result = json.loads(m.sample_table_data(DB, TABLE, catalog_id, sample_size))
        return result, athena

    # --- S3 Tables catalog — must NOT early-return ---

    def test_s3tables_does_not_skip(self, monkeypatch):
        """sample_table_data must call Athena for S3 Tables — not return early."""
        result, athena = self._run(S3_CATALOG, monkeypatch)
        athena.start_query_execution.assert_called_once()
        assert result["success"] is True

    def test_s3tables_query_context_includes_catalog(self, monkeypatch):
        _, athena = self._run(S3_CATALOG, monkeypatch)
        call_kwargs = athena.start_query_execution.call_args[1]
        ctx = call_kwargs["QueryExecutionContext"]
        assert ctx["Catalog"] == S3_CATALOG
        assert ctx["Database"] == DB

    def test_s3tables_query_is_select_limit(self, monkeypatch):
        _, athena = self._run(S3_CATALOG, monkeypatch, sample_size=5)
        call_kwargs = athena.start_query_execution.call_args[1]
        assert call_kwargs["QueryString"] == f'SELECT * FROM "{DB}"."{TABLE}" LIMIT 5'  # nosec B608 — test assertion comparing against expected SQL string; no user input

    def test_s3tables_sample_size_capped_at_50(self, monkeypatch):
        _, athena = self._run(S3_CATALOG, monkeypatch, sample_size=200)
        call_kwargs = athena.start_query_execution.call_args[1]
        assert "LIMIT 50" in call_kwargs["QueryString"]

    def test_s3tables_rows_returned(self, monkeypatch):
        result, _ = self._run(S3_CATALOG, monkeypatch)
        assert result["columns"] == self.COLUMNS
        assert len(result["sample_rows"]) == 2
        assert result["sample_rows"][0]["id"] == "holding#abc-123"

    # --- Standard catalog ---

    def test_glue_catalog_no_catalog_in_context(self, monkeypatch):
        _, athena = self._run(GLUE_CATALOG, monkeypatch)
        call_kwargs = athena.start_query_execution.call_args[1]
        ctx = call_kwargs["QueryExecutionContext"]
        assert "Catalog" not in ctx

    # --- Error path ---

    def test_athena_failure_returns_error_json(self, monkeypatch):
        from ontology_agent import main as m
        athena = _make_athena_mock(final_state="FAILED")
        athena.get_query_execution.return_value = _status_response(
            "FAILED", "Table not found"
        )
        _inject_session(athena, monkeypatch)
        monkeypatch.setenv("ARTIFACTS_BUCKET", ARTIFACTS_BUCKET)

        result = json.loads(m.sample_table_data(DB, TABLE, S3_CATALOG))
        assert result["success"] is False
        assert "Table not found" in result["error"]

    def test_missing_bucket_env_returns_error(self, monkeypatch):
        from ontology_agent import main as m
        athena = MagicMock()
        _inject_session(athena, monkeypatch)
        monkeypatch.delenv("ARTIFACTS_BUCKET", raising=False)
        monkeypatch.delenv("ATHENA_OUTPUT_LOCATION", raising=False)

        result = json.loads(m.sample_table_data(DB, TABLE, S3_CATALOG))
        assert result["success"] is False
        assert "ARTIFACTS_BUCKET" in result["error"]
        # Must not have called Athena at all
        athena.start_query_execution.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers shared by Phase 2 tool tests
# ---------------------------------------------------------------------------

def _make_phase1_md(table_name: str, nquads: str) -> str:
    """Build a minimal Phase 1 markdown file string."""
    return (
        f"# Ontology Generation\n\n"
        f"**Table:** {table_name}\n"
        f"**FK Hints for Phase 2:** none\n\n"
        f"## Generated N-Quads\n\n"
        f"```nquads\n{nquads}\n```\n"
    )


def _write_phase1_dir(tmp_path, ontology_id: str, tables: dict) -> str:
    """Write fake phase1 markdown files; return local_dir path."""
    import os
    local_dir = os.path.join(str(tmp_path), "ontologies", ontology_id, "phase1")
    os.makedirs(local_dir, exist_ok=True)
    for i, (tname, nquads) in enumerate(tables.items(), 1):
        fname = f"table-{i:02d}-{tname}.md"
        with open(os.path.join(local_dir, fname), "w", encoding="utf-8") as f:
            f.write(_make_phase1_md(tname, nquads))
    return local_dir


ONTOLOGY_ID = "test-ontology-001"
COVERAGE_NQ = "<http://ex/Coverage> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#Class> <http://ex> ."
COVERAGEPRODUCT_NQ = "<http://ex/CoverageProduct> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#Class> <http://ex> ."
FK_NQUADS = "<http://ex/CoverageProduct/hasHolding> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#ObjectProperty> <http://ex> ."


# ---------------------------------------------------------------------------
# Bug 1: exact match in read_local_nquads_file / update_nquads_in_file
# ---------------------------------------------------------------------------

class TestExactTableNameMatch:
    """'coverage' must NOT match 'coverageproduct' — substring match was the bug."""

    def _setup(self, tmp_path, monkeypatch):
        import tempfile
        local_dir = _write_phase1_dir(tmp_path, ONTOLOGY_ID, {
            "coverage": COVERAGE_NQ,
            "coverageproduct": COVERAGEPRODUCT_NQ,
        })
        # Redirect tempfile.gettempdir() to our tmp_path
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        from ontology_agent import main as m
        return m

    def test_read_coverage_gets_coverage_nquads(self, tmp_path, monkeypatch):
        m = self._setup(tmp_path, monkeypatch)
        result = json.loads(m.read_local_nquads_file(ONTOLOGY_ID, "coverage"))
        assert result["success"] is True
        assert "Coverage>" in result["nquad_content"]
        assert "CoverageProduct" not in result["nquad_content"]

    def test_read_coverageproduct_gets_coverageproduct_nquads(self, tmp_path, monkeypatch):
        m = self._setup(tmp_path, monkeypatch)
        result = json.loads(m.read_local_nquads_file(ONTOLOGY_ID, "coverageproduct"))
        assert result["success"] is True
        assert "CoverageProduct" in result["nquad_content"]
        assert result["nquad_content"].count("owl#Class") == 1

    def test_update_coverageproduct_does_not_touch_coverage_file(self, tmp_path, monkeypatch):
        import os
        m = self._setup(tmp_path, monkeypatch)
        m.update_nquads_in_file(ONTOLOGY_ID, "coverageproduct", FK_NQUADS, "add FK")
        # The coverage file must still contain only the Coverage class
        local_dir = os.path.join(str(tmp_path), "ontologies", ONTOLOGY_ID, "phase1")
        with open(os.path.join(local_dir, "table-01-coverage.md"), encoding="utf-8") as f:
            content = f.read()
        assert "Coverage>" in content
        assert "CoverageProduct" not in content

    def test_read_unknown_table_returns_error(self, tmp_path, monkeypatch):
        m = self._setup(tmp_path, monkeypatch)
        result = json.loads(m.read_local_nquads_file(ONTOLOGY_ID, "nonexistent"))
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Bug 2: append_fk_triples
# ---------------------------------------------------------------------------

class TestAppendFkTriples:

    def _setup(self, tmp_path, monkeypatch):
        import tempfile
        _write_phase1_dir(tmp_path, ONTOLOGY_ID, {
            "coverage": COVERAGE_NQ,
            "coverageproduct": COVERAGEPRODUCT_NQ,
        })
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        monkeypatch.delenv("ARTIFACTS_BUCKET", raising=False)  # skip S3 sync
        from ontology_agent import main as m
        return m

    def test_appends_to_correct_file(self, tmp_path, monkeypatch):
        import os
        m = self._setup(tmp_path, monkeypatch)
        result = json.loads(m.append_fk_triples(ONTOLOGY_ID, "coverageproduct", FK_NQUADS))
        assert result["success"] is True
        assert result["fk_triples_added"] == 1
        # Verify the coverage file is untouched
        local_dir = os.path.join(str(tmp_path), "ontologies", ONTOLOGY_ID, "phase1")
        with open(os.path.join(local_dir, "table-01-coverage.md"), encoding="utf-8") as f:
            coverage_content = f.read()
        assert "hasHolding" not in coverage_content

    def test_phase1_content_preserved_after_append(self, tmp_path, monkeypatch):
        import os
        m = self._setup(tmp_path, monkeypatch)
        m.append_fk_triples(ONTOLOGY_ID, "coverageproduct", FK_NQUADS)
        local_dir = os.path.join(str(tmp_path), "ontologies", ONTOLOGY_ID, "phase1")
        with open(os.path.join(local_dir, "table-02-coverageproduct.md"), encoding="utf-8") as f:
            content = f.read()
        # Original class triple still present
        assert "CoverageProduct" in content
        # FK triple was appended
        assert "hasHolding" in content

    def test_no_op_table_with_no_fks(self, tmp_path, monkeypatch):
        m = self._setup(tmp_path, monkeypatch)
        # Appending empty string is valid but unusual — just must not crash
        result = json.loads(m.append_fk_triples(ONTOLOGY_ID, "coverage", ""))
        assert result["success"] is True

    def test_unknown_table_returns_error(self, tmp_path, monkeypatch):
        m = self._setup(tmp_path, monkeypatch)
        result = json.loads(m.append_fk_triples(ONTOLOGY_ID, "nonexistent", FK_NQUADS))
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Bug 2: persist_file_to_neptune
# ---------------------------------------------------------------------------

class TestPersistFileToNeptune:
    """
    persist_file_to_neptune reads local Phase 1 N-Quads and pushes them to
    Neptune via the AgentCore Gateway MCPClient (not a direct Lambda call).
    The function checks NEPTUNE_GATEWAY_URL before making any network call.
    """

    def _make_mcp_response(self, inner: dict) -> dict:
        """Build the AgentCore Gateway response envelope: {statusCode, body}."""
        return {
            "content": [
                {"text": json.dumps({"statusCode": 200, "body": json.dumps(inner)})}
            ]
        }

    def _setup(self, tmp_path, monkeypatch, mcp_inner_response: dict):
        import tempfile
        _write_phase1_dir(tmp_path, ONTOLOGY_ID, {"coverage": COVERAGE_NQ})
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        monkeypatch.setenv("NEPTUNE_GATEWAY_URL", "https://fake-gateway.example.com")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        from ontology_agent import main as m

        mcp_mock = MagicMock()
        mcp_mock.__enter__ = MagicMock(return_value=mcp_mock)
        mcp_mock.__exit__ = MagicMock(return_value=False)
        mcp_mock.call_tool_sync.return_value = self._make_mcp_response(mcp_inner_response)

        monkeypatch.setattr(m, "MCPClient", lambda factory: mcp_mock)
        return m, mcp_mock

    def test_success_calls_gateway_with_nquads(self, tmp_path, monkeypatch):
        m, mcp = self._setup(tmp_path, monkeypatch, {"success": True, "message": "Persisted 1 triples"})
        result = json.loads(m.persist_file_to_neptune(ONTOLOGY_ID, "coverage"))
        assert result["success"] is True
        mcp.call_tool_sync.assert_called_once()
        args = mcp.call_tool_sync.call_args[0]
        assert args[1] == "persist-to-neptune___persist_to_neptune"
        assert COVERAGE_NQ in args[2]["nquad_data"]

    def test_nquads_never_in_llm_output(self, tmp_path, monkeypatch):
        """The N-Quads content is read in Python — the only LLM output is ontology_id + table_name."""
        m, _ = self._setup(tmp_path, monkeypatch, {"success": True, "message": "ok"})
        sig = inspect.signature(m.persist_file_to_neptune)
        assert list(sig.parameters.keys()) == ["ontology_id", "table_name"]

    def test_gateway_failure_returns_error(self, tmp_path, monkeypatch):
        m, _ = self._setup(tmp_path, monkeypatch, {"success": False, "message": "SPARQL error"})
        result = json.loads(m.persist_file_to_neptune(ONTOLOGY_ID, "coverage"))
        assert result["success"] is False
        assert "SPARQL error" in result["error"]

    def test_missing_gateway_url_returns_error(self, tmp_path, monkeypatch):
        import tempfile
        _write_phase1_dir(tmp_path, ONTOLOGY_ID, {"coverage": COVERAGE_NQ})
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        monkeypatch.delenv("NEPTUNE_GATEWAY_URL", raising=False)
        from ontology_agent import main as m
        result = json.loads(m.persist_file_to_neptune(ONTOLOGY_ID, "coverage"))
        assert result["success"] is False
        assert "NEPTUNE_GATEWAY_URL" in result["error"]

    def test_unknown_table_returns_error(self, tmp_path, monkeypatch):
        import tempfile
        _write_phase1_dir(tmp_path, ONTOLOGY_ID, {"coverage": COVERAGE_NQ})
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        monkeypatch.setenv("NEPTUNE_GATEWAY_URL", "https://fake.example.com")
        from ontology_agent import main as m
        result = json.loads(m.persist_file_to_neptune(ONTOLOGY_ID, "nonexistent"))
        assert result["success"] is False


# ===========================================================================
# update_glue_metadata_from_ontology — S3 Tables versionToken retry
# ===========================================================================

def _glue_table_for_ontology():
    """Minimal glue.get_table() response for update_glue_metadata_from_ontology."""
    return {
        "Table": {
            "Name": "orders",
            "StorageDescriptor": {
                "Columns": [{"Name": "order_id", "Type": "string", "Comment": ""}],
                "Location": "s3://bucket--table-s3",
            },
            "PartitionKeys": [],
            "TableType": "customer",
        }
    }


# Minimal N-Quads for "orders" — enough for the parser to pass without errors
_ORDERS_NQ = (
    "<http://ex/Orders> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
    "<http://www.w3.org/2002/07/owl#Class> <http://ex> ."
)


def _setup_phase1(tmp_path, monkeypatch):
    """Write a phase1 markdown file for 'orders' and redirect tempfile.gettempdir()."""
    import tempfile as _tf
    _write_phase1_dir(tmp_path, "oid", {"orders": _ORDERS_NQ})
    monkeypatch.setattr(_tf, "gettempdir", lambda: str(tmp_path))


@patch("ontology_agent.main.get_boto_session")
def test_ontology_s3tables_update_succeeds_without_version_id(mock_session, tmp_path, monkeypatch):
    """First-attempt update (no VersionId) succeeds — versionToken NOT injected."""
    from ontology_agent import main as m
    _setup_phase1(tmp_path, monkeypatch)

    glue = MagicMock()
    glue.get_table.return_value = _glue_table_for_ontology()
    mock_session.return_value.client.return_value = glue
    mock_session.return_value.region_name = "us-east-1"

    result = json.loads(m.update_glue_metadata_from_ontology(
        ontology_id="oid",
        database_name="ns1",
        table_name="orders",
        catalog_id="s3tablescatalog/my-bucket",
    ))

    assert result["success"] is True
    glue.update_table.assert_called_once()
    _, update_kwargs = glue.update_table.call_args
    assert "VersionId" not in update_kwargs


@patch("ontology_agent.main.get_boto_session")
def test_ontology_s3tables_retries_with_version_token_on_federation_error(mock_session, tmp_path, monkeypatch):
    """FederationSourceException 'versionToken null' triggers retry with fetched token."""
    from ontology_agent import main as m
    _setup_phase1(tmp_path, monkeypatch)

    glue = MagicMock()
    glue.get_table.return_value = _glue_table_for_ontology()
    glue.update_table.side_effect = [
        Exception("FederationSourceException: versionToken null"),
        None,
    ]
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    s3tables = MagicMock()
    s3tables.get_table.return_value = {"versionToken": "tok-xyz"}

    def client_factory(service, **kwargs):
        return {"glue": glue, "sts": sts, "s3tables": s3tables}[service]

    mock_session.return_value.client.side_effect = client_factory
    mock_session.return_value.region_name = "us-east-1"

    result = json.loads(m.update_glue_metadata_from_ontology(
        ontology_id="oid",
        database_name="ns1",
        table_name="orders",
        catalog_id="s3tablescatalog/my-bucket",
    ))

    assert result["success"] is True
    assert glue.update_table.call_count == 2
    _, retry_kwargs = glue.update_table.call_args
    assert retry_kwargs.get("VersionId") == "tok-xyz"


@patch("ontology_agent.main.get_boto_session")
def test_ontology_non_federation_error_is_not_retried(mock_session, tmp_path, monkeypatch):
    """Non-versionToken errors propagate immediately without retry."""
    from ontology_agent import main as m
    _setup_phase1(tmp_path, monkeypatch)

    glue = MagicMock()
    glue.get_table.return_value = _glue_table_for_ontology()
    glue.update_table.side_effect = Exception("AccessDeniedException: not authorised")
    mock_session.return_value.client.return_value = glue
    mock_session.return_value.region_name = "us-east-1"

    result = json.loads(m.update_glue_metadata_from_ontology(
        ontology_id="oid",
        database_name="ns1",
        table_name="orders",
        catalog_id="s3tablescatalog/my-bucket",
    ))

    assert result["success"] is False
    assert "AccessDeniedException" in result["error"]
    glue.update_table.assert_called_once()
