"""Tests for updated metadata agent prompt builder."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

from metadata_agent.prompt_builder import build_table_prompt, SYSTEM_PROMPT, _build_annotations_section


def test_build_table_prompt_baseline():
    """Original call without new args still works."""
    prompt = build_table_prompt(
        database_name="mydb", table_name="orders",
        catalog_id="AWSDataCatalog", step=1, total_steps=3, job_id="job-123",
    )
    assert "mydb" in prompt
    assert "orders" in prompt


def test_build_table_prompt_with_use_cases():
    prompt = build_table_prompt(
        database_name="mydb", table_name="orders",
        catalog_id="AWSDataCatalog", step=1, total_steps=3, job_id="job-123",
        use_cases_description="Revenue reporting.",
        data_sources_description="E-commerce orders.",
    )
    assert "DOMAIN CONTEXT" in prompt
    assert "Revenue reporting." in prompt
    assert "DATA SOURCES CONTEXT" in prompt
    assert "E-commerce orders." in prompt


def test_build_table_prompt_with_docs():
    docs = [
        {"filename": "glossary.pdf", "path": "s3://b/glossary.pdf", "size": 12345},
        {"filename": "schema.xlsx", "path": "s3://b/schema.xlsx", "size": 8500},
    ]
    prompt = build_table_prompt(
        database_name="mydb", table_name="orders",
        catalog_id="AWSDataCatalog", step=1, total_steps=3, job_id="job-123",
        uploaded_docs=docs,
    )
    assert "REFERENCE DOCUMENTS" in prompt
    assert "glossary.pdf" in prompt
    assert "schema.xlsx" in prompt
    assert "download_document_from_s3" in prompt


def test_build_table_prompt_empty_docs_no_section():
    prompt = build_table_prompt(
        database_name="mydb", table_name="orders",
        catalog_id="AWSDataCatalog", step=1, total_steps=3, job_id="job-123",
        uploaded_docs=[],
    )
    assert "REFERENCE DOCUMENTS" not in prompt


def test_system_prompt_mentions_document_tools():
    assert "download_document_from_s3" in SYSTEM_PROMPT
    assert "search_document" in SYSTEM_PROMPT
    assert "read_document_lines" in SYSTEM_PROMPT


def test_annotations_section_empty_list():
    assert _build_annotations_section([]) == ""


def test_annotations_section_formats_hints():
    hints = [
        {"target": "customer_id", "instruction": "FK to customers table"},
        {"target": "status", "instruction": "Enum: active, inactive, pending"},
    ]
    result = _build_annotations_section(hints)
    assert "customer_id" in result
    assert "FK to customers table" in result
    assert "status" in result
    assert "Enum: active, inactive, pending" in result


def test_build_table_prompt_no_annotations():
    prompt = build_table_prompt(
        database_name="mydb", table_name="orders",
        catalog_id="AWSDataCatalog", step=1, total_steps=3, job_id="job-001",
    )
    assert "ANNOTATION" not in prompt


def test_build_table_prompt_with_annotations():
    hints = [{"target": "order_id", "instruction": "Primary key, never null"}]
    prompt = build_table_prompt(
        database_name="mydb", table_name="orders",
        catalog_id="AWSDataCatalog", step=1, total_steps=3, job_id="job-001",
        annotations=hints,
    )
    assert "order_id" in prompt
    assert "Primary key, never null" in prompt
