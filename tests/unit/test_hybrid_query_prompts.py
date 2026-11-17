"""Smoke + behaviour tests for the hybrid agents' copy-and-own prompt modules.

Covers ``agents.semantic_layer_creation_agent.query_prompts`` (KB prose builders)
and ``agents.semantic_layer_query_agent.query_prompts`` (NL->SQL / execution
templates). These are pure string-template modules with no cloud dependency.
"""
from agents.semantic_layer_creation_agent import query_prompts as creation_prompts
from agents.semantic_layer_query_agent import query_prompts as query_prompts


# --- creation agent KB prompt builders ---------------------------------------

def test_build_kb_prompt_embeds_table_identity_and_schema():
    prompt = creation_prompts.build_kb_prompt(
        database_name="normalized",
        table_name="address",
        catalog_id="AwsDataCatalog",
        schema_json='[{"name": "id", "type": "int"}]',
        sample_json='[{"id": 1}]',
    )
    assert "normalized" in prompt
    assert "address" in prompt
    assert "AwsDataCatalog" in prompt
    assert '"name": "id"' in prompt
    # The document skeleton header is filled in.
    assert "# AwsDataCatalog.normalized.address" in prompt


def test_build_kb_prompt_includes_context_and_layer_tables():
    prompt = creation_prompts.build_kb_prompt(
        database_name="db",
        table_name="t",
        catalog_id="cat",
        schema_json="[]",
        sample_json="[]",
        use_cases_description="claims analytics",
        data_sources_description="policy admin system",
        layer_tables=["t", "lookup", "t"],  # dedup + sort exercised
    )
    assert "DOMAIN CONTEXT: claims analytics" in prompt
    assert "DATA SOURCES CONTEXT: policy admin system" in prompt
    assert "`lookup`" in prompt
    assert "`t`" in prompt
    assert "TABLES IN THIS SEMANTIC LAYER (2)" in prompt


def test_build_kb_prompt_omits_optional_sections_when_absent():
    prompt = creation_prompts.build_kb_prompt(
        database_name="db", table_name="t", catalog_id="cat",
        schema_json="[]", sample_json="[]",
    )
    assert "DOMAIN CONTEXT" not in prompt
    assert "TABLES IN THIS SEMANTIC LAYER" not in prompt


def test_build_kb_document_prepends_header():
    doc = creation_prompts.build_kb_document(
        database_name="db", table_name="t", catalog_id="cat",
        body="## Overview\nA thing.\n",
    )
    assert doc.startswith("# cat.db.t\n")
    assert "A thing." in doc


def test_build_sidecar_attributes_defaults_catalog():
    attrs = creation_prompts.build_sidecar_attributes(
        semantic_layer_id="sl-1", semantic_layer_version="v1",
        database_name="db", table_name="t", catalog_id="",
    )
    assert attrs == {
        "semantic_layer_id": "sl-1",
        "semantic_layer_version": "v1",
        "database_name": "db",
        "table_name": "t",
        "catalog_id": "AWSDataCatalog",
    }


def test_build_sidecar_attributes_keeps_explicit_catalog():
    attrs = creation_prompts.build_sidecar_attributes(
        semantic_layer_id="sl", semantic_layer_version="v2",
        database_name="db", table_name="t", catalog_id="mycat",
    )
    assert attrs["catalog_id"] == "mycat"


# --- query agent prompt templates --------------------------------------------

def test_query_prompt_constants_present_and_formattable():
    assert "Join paths" in query_prompts.JOIN_PATH_GUIDANCE
    assert query_prompts.SQL_GENERATION_GUIDANCE.startswith(
        query_prompts.JOIN_PATH_GUIDANCE
    )
    # The header template is safely formattable in isolation (guidance braces
    # are kept out of it).
    header = query_prompts.SQL_GENERATION_HEADER_TEMPLATE.format(
        slice_text="{}", question="How many?", suffix="",
    )
    assert "How many?" in header
    assert "# Slice (JSON)" in header


def test_execution_prompt_scopes_to_single_query():
    assert "execute_sql_query" in query_prompts.EXECUTION_PROMPT
    assert "at most 2" in query_prompts.EXECUTION_PROMPT
