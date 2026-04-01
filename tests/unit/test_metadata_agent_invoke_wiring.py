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
    monkeypatch.setattr(agent_module.app, "add_async_task",
                        MagicMock(return_value="task-1"))
    monkeypatch.setattr(agent_module.app, "complete_async_task", MagicMock())

    original_thread = threading.Thread

    def sync_thread(target=None, **kwargs):
        t = MagicMock()
        t.start = lambda: target()
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
