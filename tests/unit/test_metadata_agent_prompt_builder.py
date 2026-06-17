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
        semantic_layer_version="v1",
    )
    assert "mydb" in prompt
    assert "orders" in prompt


def test_build_table_prompt_with_use_cases():
    prompt = build_table_prompt(
        database_name="mydb", table_name="orders",
        catalog_id="AWSDataCatalog", step=1, total_steps=3, job_id="job-123",
        semantic_layer_version="v1",
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
        semantic_layer_version="v1",
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
        semantic_layer_version="v1",
        uploaded_docs=[],
    )
    assert "REFERENCE DOCUMENTS" not in prompt


def test_system_prompt_mentions_document_tools():
    assert "download_document_from_s3" in SYSTEM_PROMPT
    assert "search_document" in SYSTEM_PROMPT
    assert "read_document_lines" in SYSTEM_PROMPT


def test_system_prompt_mandates_full_column_inventory():
    """The doc must list EVERY column so the query agent's slice is complete
    (a missing column is one the agent can hallucinate around)."""
    assert "Column inventory" in SYSTEM_PROMPT
    assert "EVERY column" in SYSTEM_PROMPT


def test_system_prompt_teaches_prefix_join_transform():
    """Reference-table joins must encode key-format transforms (CONCAT) rather
    than a bare equality that silently matches zero rows."""
    assert "CONCAT" in SYSTEM_PROMPT
    assert "PARTY#" in SYSTEM_PROMPT


def test_system_prompt_empty_table_names_real_source():
    """An empty/audit-only table doc must steer to the table that carries the data."""
    assert "EMPTY/AUDIT-ONLY" in SYSTEM_PROMPT


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
        semantic_layer_version="v1",
    )
    assert "ANNOTATION" not in prompt


def test_build_table_prompt_with_annotations():
    hints = [{"target": "order_id", "instruction": "Primary key, never null"}]
    prompt = build_table_prompt(
        database_name="mydb", table_name="orders",
        catalog_id="AWSDataCatalog", step=1, total_steps=3, job_id="job-001",
        semantic_layer_version="v1",
        annotations=hints,
    )
    assert "order_id" in prompt
    assert "Primary key, never null" in prompt


def test_system_prompt_forbids_out_of_layer_references():
    """The agent must not invent a redirect/join to a table outside the layer
    (the participant/payout degrade)."""
    assert "TABLES IN THIS SEMANTIC LAYER" in SYSTEM_PROMPT
    assert "Cross-references must stay inside this layer" in SYSTEM_PROMPT


def test_build_table_prompt_with_layer_tables():
    prompt = build_table_prompt(
        database_name="normalized", table_name="rider_participant",
        catalog_id="AWSDataCatalog", step=1, total_steps=3, job_id="job-123",
        semantic_layer_version="v1",
        layer_tables=["rider", "rider_participant", "party"],
    )
    assert "TABLES IN THIS SEMANTIC LAYER (3)" in prompt
    assert "`rider`" in prompt and "`party`" in prompt
    # The list is the allowlist the agent must restrict cross-references to.
    assert "Reference ONLY these tables" in prompt


def test_build_table_prompt_without_layer_tables_omits_section():
    prompt = build_table_prompt(
        database_name="normalized", table_name="rider_participant",
        catalog_id="AWSDataCatalog", step=1, total_steps=3, job_id="job-123",
        semantic_layer_version="v1",
    )
    assert "TABLES IN THIS SEMANTIC LAYER" not in prompt


def test_build_table_prompt_includes_layer_id_and_version():
    """Agent must see the semantic-layer id and version so it can pass them
    to save_metadata_document_to_s3."""
    prompt = build_table_prompt(
        database_name="mydb", table_name="orders",
        catalog_id="AWSDataCatalog", step=1, total_steps=3, job_id="layer-abc",
        semantic_layer_version="v7",
    )
    assert "layer-abc" in prompt
    assert "v7" in prompt
    assert "save_metadata_document_to_s3" in prompt
