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
