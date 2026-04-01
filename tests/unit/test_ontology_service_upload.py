"""
Unit tests for extract_text_from_file in OntologyService.

Tests text extraction from PDF, DOCX, plain-text, and rejection of
unsupported formats — before any S3 or DynamoDB interaction.
"""
import io
import sys
import os
import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api')
)


def _make_minimal_docx(text: str) -> bytes:
    """Build a real DOCX in-memory containing `text`."""
    from docx import Document
    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_extract_txt():
    from services.ontology_service import extract_text_from_file
    result = extract_text_from_file(b"hello world", "schema.txt")
    assert result == "hello world"


def test_extract_markdown():
    from services.ontology_service import extract_text_from_file
    result = extract_text_from_file(b"# Title\nsome content", "notes.md")
    assert "Title" in result


def test_extract_markdown_extension():
    from services.ontology_service import extract_text_from_file
    result = extract_text_from_file(b"glossary", "terms.markdown")
    assert result == "glossary"


def test_extract_docx():
    from services.ontology_service import extract_text_from_file
    content = _make_minimal_docx("policy term definition")
    result = extract_text_from_file(content, "dictionary.docx")
    assert "policy term definition" in result


def test_extract_unsupported_raises():
    from services.ontology_service import extract_text_from_file
    with pytest.raises(ValueError, match="Unsupported file type"):
        extract_text_from_file(b"data", "resume.doc")


def test_extract_unknown_extension_raises():
    from services.ontology_service import extract_text_from_file
    with pytest.raises(ValueError, match="Unsupported file type"):
        extract_text_from_file(b"data", "file.xlsx")


def test_upload_metadata_file_stores_text():
    """upload_metadata_file should store extracted text, not raw bytes."""
    from unittest.mock import MagicMock, patch
    from boto3.dynamodb.conditions import Key as DKey
    from services.ontology_service import OntologyService

    svc = OntologyService.__new__(OntologyService)
    svc.artifacts_bucket = 'test-bucket'
    svc.s3_client = MagicMock()
    svc.table = MagicMock()
    # get_metadata_config uses table.query, not get_item
    svc.table.query.return_value = {'Items': []}

    txt_bytes = b"customer id: unique identifier"
    result = svc.upload_metadata_file(
        file_content=txt_bytes,
        filename="glossary.txt",
        id="abc-123"
    )

    # S3 put_object should have been called with plain text
    call_kwargs = svc.s3_client.put_object.call_args.kwargs
    assert call_kwargs['ContentType'] == 'text/plain'
    assert b"customer id" in call_kwargs['Body']

    # The returned path should point to the .txt key
    assert result['path'].endswith('.txt')
    assert result['filename'] == 'glossary.txt'
