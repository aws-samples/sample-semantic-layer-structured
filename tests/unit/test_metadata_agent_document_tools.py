"""Tests for document tools added to the metadata agent."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

from unittest.mock import MagicMock, patch
import metadata_agent.main as agent_module


def test_download_document_from_s3_success(tmp_path):
    mock_session = MagicMock()
    mock_s3 = MagicMock()
    mock_session.client.return_value = mock_s3

    def fake_download(bucket, key, local_path):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "w", encoding="utf-8") as f:
            f.write("hello world")

    mock_s3.download_file.side_effect = fake_download

    with patch.object(agent_module, 'get_boto_session', return_value=mock_session):
        result = json.loads(
            agent_module.download_document_from_s3("s3://my-bucket/docs/test.txt")
        )

    assert result["success"] is True
    assert result["filename"] == "test.txt"
    assert result["content_type"] == "text"


def test_search_document_finds_matches(tmp_path):
    doc = tmp_path / "glossary.txt"
    doc.write_text("line1\ncustomer id column\nline3\n")

    result = json.loads(agent_module.search_document(str(doc), "customer"))
    assert result["success"] is True
    assert result["total_matches"] == 1
    assert "customer id column" in result["matches"][0]["matched_line"]


def test_read_document_lines_returns_lines(tmp_path):
    doc = tmp_path / "schema.txt"
    doc.write_text("".join(f"line {i}\n" for i in range(1, 101)))

    result = json.loads(
        agent_module.read_document_lines(str(doc), start_line=5, num_lines=3)
    )
    assert result["success"] is True
    assert len(result["lines"]) == 3
    assert result["lines"][0].startswith("line 5")


def test_document_tools_registered_in_agent():
    """All 3 document tools must be registered in create_metadata_agent()."""
    import metadata_agent.main as m
    from strands import Agent

    captured_kwargs = {}

    def fake_agent(**kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    Agent.side_effect = fake_agent
    m.create_metadata_agent()

    tool_names = [
        getattr(t, '__name__', getattr(t, 'name', str(t)))
        for t in captured_kwargs.get('tools', [])
    ]
    assert 'download_document_from_s3' in tool_names
    assert 'search_document' in tool_names
    assert 'read_document_lines' in tool_names


# ---------------------------------------------------------------------------
# save_metadata_document_to_s3 — source-side schema validation wiring
# ---------------------------------------------------------------------------

_DOC_WITH_HALLUCINATION = """# cat.normalized.holding

## Overview
One row per holding.

## Columns
| Column | Type | Description |
|--------|------|-------------|
| holding_id | string | PK. |
| market_value | double | Value. |
| party_id | string | HALLUCINATED — not a real column. |

## Notes
None.
"""


def _capture_s3_put():
    """Patch an S3 client and return ``(session, puts)`` capturing put bodies."""
    mock_session = MagicMock()
    mock_s3 = MagicMock()
    mock_session.client.return_value = mock_s3
    puts = {}

    def fake_put(*, Bucket, Key, Body, **kwargs):
        puts[Key] = Body

    mock_s3.put_object.side_effect = fake_put
    return mock_session, puts


def test_save_metadata_drops_hallucinated_column(monkeypatch):
    """A column present in the doc but absent from the real schema is stripped
    from the markdown written to S3, and a warning metric is emitted."""
    monkeypatch.setenv("ARTIFACTS_BUCKET", "my-bucket")
    mock_session, puts = _capture_s3_put()

    with patch.object(agent_module, 'get_boto_session', return_value=mock_session), \
         patch.object(agent_module, '_fetch_real_columns',
                      return_value={"holding_id", "market_value"}), \
         patch.object(agent_module, '_resolve_reference_target_columns',
                      return_value={}), \
         patch.object(agent_module.cw_metrics, 'emit') as mock_emit:
        result = json.loads(agent_module.save_metadata_document_to_s3(
            database_name="normalized",
            table_name="holding",
            catalog_id="cat",
            metadata_content=_DOC_WITH_HALLUCINATION,
            semantic_layer_id="layer1",
            semantic_layer_version="v1",
        ))

    assert result["success"] is True
    md_key = next(k for k in puts if k.endswith(".md"))
    body = puts[md_key].decode("utf-8")
    assert "party_id" not in body
    assert "market_value" in body
    assert "holding_id" in body
    mock_emit.assert_called_once()
    assert mock_emit.call_args.args[0] == "MetadataDocHallucination"


def test_save_metadata_clean_doc_passes_through(monkeypatch):
    """A doc whose every column is real is written byte-for-byte, no metric."""
    monkeypatch.setenv("ARTIFACTS_BUCKET", "my-bucket")
    mock_session, puts = _capture_s3_put()
    clean_doc = _DOC_WITH_HALLUCINATION.replace(
        "| party_id | string | HALLUCINATED — not a real column. |\n", ""
    )

    with patch.object(agent_module, 'get_boto_session', return_value=mock_session), \
         patch.object(agent_module, '_fetch_real_columns',
                      return_value={"holding_id", "market_value"}), \
         patch.object(agent_module, '_resolve_reference_target_columns',
                      return_value={}), \
         patch.object(agent_module.cw_metrics, 'emit') as mock_emit:
        result = json.loads(agent_module.save_metadata_document_to_s3(
            database_name="normalized",
            table_name="holding",
            catalog_id="cat",
            metadata_content=clean_doc,
            semantic_layer_id="layer1",
            semantic_layer_version="v1",
        ))

    assert result["success"] is True
    md_key = next(k for k in puts if k.endswith(".md"))
    assert puts[md_key].decode("utf-8") == clean_doc
    mock_emit.assert_not_called()


def test_save_metadata_skips_validation_when_schema_unresolved(monkeypatch):
    """When the real schema can't be resolved (empty set), validation is skipped
    and the doc is saved unchanged — an infra failure never blocks the build."""
    monkeypatch.setenv("ARTIFACTS_BUCKET", "my-bucket")
    mock_session, puts = _capture_s3_put()

    with patch.object(agent_module, 'get_boto_session', return_value=mock_session), \
         patch.object(agent_module, '_fetch_real_columns', return_value=set()), \
         patch.object(agent_module.cw_metrics, 'emit') as mock_emit:
        result = json.loads(agent_module.save_metadata_document_to_s3(
            database_name="normalized",
            table_name="holding",
            catalog_id="cat",
            metadata_content=_DOC_WITH_HALLUCINATION,
            semantic_layer_id="layer1",
            semantic_layer_version="v1",
        ))

    assert result["success"] is True
    md_key = next(k for k in puts if k.endswith(".md"))
    assert puts[md_key].decode("utf-8") == _DOC_WITH_HALLUCINATION
    mock_emit.assert_not_called()
