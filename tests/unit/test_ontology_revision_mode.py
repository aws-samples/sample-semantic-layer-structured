"""
Unit tests for ontology agent revision mode workflow.

Run locally:
    cd /Users/huthmac/Documents/AWS/00_workspace/semantic-layer
    pytest tests/unit/test_ontology_revision_mode.py -v
"""
import json
import os
import sys
import threading
from unittest.mock import MagicMock, patch

# Add agents directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))


def test_apply_targeted_edits_writes_versioned_nq_key(monkeypatch):
    """apply_targeted_edits reads base N-Quads from S3, applies edits, and writes
    the result as ontology_v3.nq (not .ttl) — a single versioned file."""
    monkeypatch.setenv("ARTIFACTS_BUCKET", "test-bucket")
    base_nquads = "<s> <p> <o> <g> .\n<s2> <p2> <o2> <g> ."
    s3_mock = MagicMock()
    s3_mock.get_object.return_value = {
        "Body": MagicMock(read=lambda: base_nquads.encode("utf-8"))
    }
    with patch("ontology_agent.main.get_boto_session") as mock_session:
        mock_session.return_value.client.return_value = s3_mock
        from ontology_agent.main import apply_targeted_edits

        result = json.loads(
            apply_targeted_edits(
                ontology_id="abc",
                target_version="v3",
                edits=[{"old_triple": "<s2> <p2> <o2> <g> .", "new_triple": "<s2> <p2> <new_o> <g> ."}],
            )
        )
    assert result["success"] is True
    assert "ontology_v3.nq" in result["nquads_s3_path"]
    assert "ontology_v3.ttl" not in str(result)  # no Turtle file
    assert s3_mock.put_object.call_count == 1   # single versioned file written
    assert result["edits_applied"] == 1


def test_persist_nquads_to_neptune_calls_gateway(monkeypatch):
    monkeypatch.setenv("NEPTUNE_GATEWAY_URL", "https://gateway.example.com")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    mcp_mock = MagicMock()
    mcp_mock.__enter__ = MagicMock(return_value=mcp_mock)
    mcp_mock.__exit__ = MagicMock(return_value=False)
    mcp_mock.call_tool_sync.return_value = {
        "content": [
            {
                "text": json.dumps(
                    {"body": json.dumps({"success": True, "message": "ok"})}
                )
            }
        ]
    }
    with patch("ontology_agent.main.MCPClient", return_value=mcp_mock):
        from ontology_agent.main import persist_nquads_to_neptune

        result = json.loads(persist_nquads_to_neptune("<s> <p> <o> <g> ."))
    assert result["success"] is True
    mcp_mock.call_tool_sync.assert_called_once()
    args = mcp_mock.call_tool_sync.call_args[0]
    assert args[1] == "persist-to-neptune___persist_to_neptune"
    assert args[2]["nquad_data"] == "<s> <p> <o> <g> ."


def test_build_revision_prompt_contains_paths():
    from ontology_agent.prompt_builder import build_revision_prompt

    prompt = build_revision_prompt(
        ontology_id="abc",
        target_version="v3",
        base_nquads_s3_path="s3://b/base_v3.nq",
        instructions_s3_path="s3://b/instr.md",
        namespace="http://example.com/ontology/abc",
    )
    assert "s3://b/base_v3.nq" in prompt
    assert "s3://b/instr.md" in prompt
    assert "v3" in prompt
    assert "N-Quads" in prompt  # prompt must reference N-Quads format


def test_run_revision_mode_uploads_context_and_calls_agent(monkeypatch):
    monkeypatch.setenv("ARTIFACTS_BUCKET", "test-bucket")
    monkeypatch.setenv("ONTOLOGY_METADATA_TABLE", "test-table")
    monkeypatch.setenv("NEPTUNE_GATEWAY_URL", "https://gw.example.com")

    s3_mock = MagicMock()
    s3_mock.get_object.return_value = {
        "Body": MagicMock(read=lambda: b"<s> <p> <o> <g> .")
    }
    dynamo_table = MagicMock()
    session_mock = MagicMock()
    session_mock.client.return_value = s3_mock
    session_mock.resource.return_value.Table.return_value = dynamo_table

    mcp_mock = MagicMock()
    mcp_mock.__enter__ = MagicMock(return_value=mcp_mock)
    mcp_mock.__exit__ = MagicMock(return_value=False)

    agent_mock = MagicMock()

    with (
        patch("ontology_agent.main.get_boto_session", return_value=session_mock),
        patch("ontology_agent.main.MCPClient", return_value=mcp_mock),
        patch(
            "ontology_agent.main.create_revision_agent", return_value=agent_mock
        ),
        patch("ontology_agent.main.update_dynamodb_status"),
    ):
        from ontology_agent.main import _run_revision_mode

        config = {
            "id": "abc",
            "version": "v1",
            "name": "test",
            "metadataPath": "s3://test-bucket/ontologies/abc/ontology.nq",
            "targetVersion": "v3",
            "revisionInstructions": [
                {"highlightedText": "PolicyHolder", "comment": "add subclass"}
            ],
        }
        _run_revision_mode("abc", config)

    # Context files uploaded to S3
    put_keys = [c[1]["Key"] for c in s3_mock.put_object.call_args_list]
    assert any("base_v3.nq" in k for k in put_keys)  # N-Quads base, not .ttl
    assert any("instructions_v3.md" in k for k in put_keys)
    # Neptune delete called
    mcp_mock.call_tool_sync.assert_called_once()
    # Revision agent invoked
    agent_mock.assert_called_once()
    # DynamoDB: one put_item for the new version record
    assert dynamo_table.put_item.call_count == 1
    new_record = dynamo_table.put_item.call_args_list[0][1]["Item"]
    assert new_record["version"] == "v3"
    assert "ontology_v3.nq" in new_record["metadataPath"]  # .nq not .ttl

    # one update_item to mark the previous version (v1) as inactive
    assert dynamo_table.update_item.call_count == 1
    update_kwargs = dynamo_table.update_item.call_args[1]
    assert update_kwargs["Key"] == {"id": "abc", "version": "v1"}
    assert update_kwargs["ExpressionAttributeValues"][":inactive"] == "inactive"


def test_invoke_routes_background_work_to_revision(monkeypatch):
    """
    Test that invoke routes to revision mode when revisionMode is True in config.
    Verify by ensuring _run_revision_mode is called in the background thread
    and Phase 1/2 are skipped.
    """
    monkeypatch.setenv("ONTOLOGY_METADATA_TABLE", "tbl")
    monkeypatch.setenv("ARTIFACTS_BUCKET", "b")

    # Setup detailed mock for boto3 session and DynamoDB
    table_mock = MagicMock()
    table_mock.get_item.return_value = {
        "Item": {
            "id": "x",
            "version": "v1",
            "revisionMode": True,
            "revisionInstructions": [],
            "targetVersion": "v2",
            "metadataPath": "s3://b/ontologies/x/ontology.ttl",
        }
    }

    dynamodb_mock = MagicMock()
    dynamodb_mock.Table.return_value = table_mock

    session_mock = MagicMock()
    session_mock.resource.return_value = dynamodb_mock

    revision_called = threading.Event()
    revision_error = None
    phase1_called = threading.Event()

    def fake_revision(oid, cfg):
        nonlocal revision_error
        try:
            revision_called.set()
        except Exception as e:
            revision_error = e

    def mock_create_phase1_agent():
        # This should NOT be called if revision mode is triggered
        phase1_called.set()
        raise AssertionError("Phase 1 should be skipped in revision mode")

    with patch(
        "ontology_agent.main._run_revision_mode", side_effect=fake_revision
    ), patch(
        "ontology_agent.main.update_dynamodb_status"
    ), patch(
        "ontology_agent.main.create_phase1_agent",
        side_effect=mock_create_phase1_agent,
    ):
        import ontology_agent.main as main_module

        # Set the boto session directly instead of patching the function
        main_module.set_boto_session(session_mock)

        # Mock the app methods without patching the app object itself
        main_module.app.add_async_task = MagicMock(return_value="task-1")
        main_module.app.complete_async_task = MagicMock()

        result = main_module.invoke({"id": "x"}, {})

        # invoke returns immediately with processing status
        assert result.get("status") == "processing", f"Expected status 'processing', got {result}"

        # revision runs in background — wait for it (WITHIN the patch context)
        assert revision_called.wait(
            timeout=3
        ), f"Revision mode should be called in background. Error: {revision_error}"

        # Phase 1 should NOT be called
        assert not phase1_called.is_set(), "Phase 1 should be skipped in revision mode"
