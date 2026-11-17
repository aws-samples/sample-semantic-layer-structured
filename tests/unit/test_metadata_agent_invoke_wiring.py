"""Tests that invoke() passes config context fields to build_table_prompt."""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

from unittest.mock import MagicMock
import metadata_agent.main as agent_module


def _make_dynamo_mock(config: dict):
    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": config}
    mock_table.update_item.return_value = {}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table
    mock_session = MagicMock()
    mock_session.resource.return_value = mock_resource
    return mock_session


def _run_invoke_sync(monkeypatch, config: dict) -> dict:
    """Patch threading.Thread to run background_work synchronously."""
    captured = {}

    def fake_build_table_prompt(**kwargs):
        captured.update(kwargs)
        return "TABLE 1 OF 1\n"

    def fake_build_annotation_prompt(**kwargs):
        captured.update(kwargs)
        return "TABLE 1 OF 1\n"

    monkeypatch.setattr(agent_module, "build_table_prompt", fake_build_table_prompt)
    monkeypatch.setattr(agent_module, "build_annotation_prompt", fake_build_annotation_prompt)
    monkeypatch.setattr(agent_module, "get_boto_session",
                        lambda: _make_dynamo_mock(config))
    monkeypatch.setattr(agent_module, "create_metadata_agent",
                        lambda **kwargs: MagicMock())
    monkeypatch.setattr(agent_module, "_update_dynamodb_status", MagicMock())
    monkeypatch.setattr(agent_module, "_write_versioned_completion", MagicMock())
    monkeypatch.setattr(agent_module, "_trigger_kb_ingestion", MagicMock())
    # These wiring tests assert prompt/inventory wiring, not fingerprinting. The
    # per-table success path calls _record_table_fingerprint → get_single_table_schema,
    # which against a mock Athena client would poll ~60s per table before fail-soft.
    # Stub it so the wiring assertions run fast and in isolation.
    monkeypatch.setattr(agent_module, "_record_table_fingerprint", MagicMock())
    monkeypatch.setattr(agent_module, "_trigger_eval", MagicMock())
    monkeypatch.setattr(agent_module.app, "add_async_task",
                        MagicMock(return_value="task-1"))
    monkeypatch.setattr(agent_module.app, "complete_async_task", MagicMock())

    original_thread = threading.Thread

    def sync_thread(target=None, args=(), kwargs=None, **_ignored):
        # Mirror threading.Thread's real contract: invoke() now starts the worker
        # as Thread(target=ctx.run, args=(background_work,)) to propagate the OTel
        # context into the thread, so the stub must forward args/kwargs to target.
        t = MagicMock()
        t.start = lambda: target(*args, **(kwargs or {}))
        return t

    monkeypatch.setattr(threading, "Thread", sync_thread)
    agent_module.invoke({"id": "test-job-1"}, {})
    monkeypatch.setattr(threading, "Thread", original_thread)
    return captured


def test_invoke_passes_use_cases_description(monkeypatch):
    config = {
        "id": "test-job-1", "version": "v1",
        "useCasesDescription": "Revenue reporting",
        "dataSourcesDescription": "E-commerce orders",
        "dataSources": [
            {"databaseName": "mydb", "tableName": "orders",
             "catalogId": "AWSDataCatalog"}
        ],
    }
    captured = _run_invoke_sync(monkeypatch, config)
    assert captured.get("use_cases_description") == "Revenue reporting"
    assert captured.get("data_sources_description") == "E-commerce orders"


def test_invoke_passes_uploaded_docs(monkeypatch):
    docs = [{"filename": "g.pdf", "path": "s3://b/g.pdf", "size": 1000}]
    config = {
        "id": "test-job-1", "version": "v1",
        "uploadedDocuments": docs,
        "dataSources": [
            {"databaseName": "mydb", "tableName": "orders",
             "catalogId": "AWSDataCatalog"}
        ],
    }
    captured = _run_invoke_sync(monkeypatch, config)
    assert captured.get("uploaded_docs") == docs


def test_invoke_defaults_when_fields_absent(monkeypatch):
    config = {
        "id": "test-job-1", "version": "v1",
        "dataSources": [
            {"databaseName": "mydb", "tableName": "orders",
             "catalogId": "AWSDataCatalog"}
        ],
    }
    captured = _run_invoke_sync(monkeypatch, config)
    assert captured.get("use_cases_description", "") == ""
    assert captured.get("data_sources_description", "") == ""
    assert captured.get("uploaded_docs", []) == []


def test_delta_build_layer_tables_is_full_inventory_not_filtered_subset(monkeypatch):
    """I4: on a delta build (deltaTargetTables scopes the loop to one table), the
    layer_tables inventory threaded into the prompt — and into the doc validator —
    must be the FULL selected set, so a rebuilt table's valid references to
    unchanged siblings are NOT stripped as out-of-layer."""
    config = {
        "id": "test-job-1", "version": "v1",
        "dataSources": [
            {"databaseName": "db", "tableName": "orders", "catalogId": "AWSDataCatalog"},
            {"databaseName": "db", "tableName": "customers", "catalogId": "AWSDataCatalog"},
            {"databaseName": "db", "tableName": "items", "catalogId": "AWSDataCatalog"},
        ],
        # Delta scope: rebuild only `orders` this run.
        "deltaTargetTables": ["db.orders"],
    }
    captured = _run_invoke_sync(monkeypatch, config)
    # Only `orders` is (re)built...
    assert captured.get("table_name") == "orders"
    # ...but the inventory is the FULL set, including the unchanged siblings.
    assert set(captured.get("layer_tables") or []) == {"orders", "customers", "items"}


def test_invoke_passes_enrichment_annotations_from_dynamo(monkeypatch):
    """revisionInstructions + revisionMode=True reach build_annotation_prompt(annotations=...).

    The service layer always stamps both fields together; the agent gates on
    revisionMode (matching the ontology agent pattern), not on bool(annotations).
    """
    annotations = [{"target": "order_id", "instruction": "Primary key"}]
    config = {
        "id": "test-job-1", "version": "v1",
        "dataSources": [
            {"databaseName": "mydb", "tableName": "orders",
             "catalogId": "AWSDataCatalog"}
        ],
        "revisionInstructions": annotations,
        "revisionMode": True,
        "targetVersion": "v2",
    }
    captured = _run_invoke_sync(monkeypatch, config)
    assert captured.get("annotations") == annotations
