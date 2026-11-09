"""Model-facing prompt templates for the Hybrid creation agent's KB builder.

Copied-and-owned (per the design's copy-not-import rule) from the Metadata
Generation Agent's ``prompt_builder.py``. This module is a plain collection of
string templates + a small pure builder — no Bedrock/AWS imports — so the unit
tests can import it without any cloud dependency.

The RICH prose these prompts produce (business purpose, synonyms, reference
tables, sample SQL, sample data, notes) is destined for the Bedrock Knowledge
Base. The thin structural VKG (built separately by ``vkg_builder``) gets none of
this prose — that split is the whole point of the Hybrid design.

Isolation note: standard library only — no ``agents.shared`` / sibling-agent
imports (enforced by ``tests/unit/test_hybrid_isolation.py``).
"""
from typing import Dict, List, Optional

# System prompt for the KB prose composer. Copied in shape from the metadata
# agent's SYSTEM_PROMPT, trimmed to the composition rules that shape the rich KB
# markdown (business purpose, synonyms, reference tables, query patterns, sample
# data, notes). Tool-orchestration lines from the original are dropped — the KB
# builder here supplies the schema/sample context directly in the user prompt.
KB_SYSTEM_PROMPT = """
You are the Hybrid Knowledge Base document composer for structured data assets.
Your job is to write a rich, business-friendly markdown document describing ONE
table so that natural-language search can find the RIGHT table for a question.

You are given the table's schema, sampled rows, and any domain context. Compose:

- A **business purpose**: the business entity a row represents, its grain
  (one row = one X), what the data is used for and by whom.
- **Business concepts & synonyms**: the everyday words a user would ask with
  (participant, role, member, owner, beneficiary, who, policy, contract, …)
  mapped to the columns that actually carry them — even when those words are NOT
  the literal table/column names. This is what makes the table findable.
- **Reference tables**: the lookup/reference tables this table joins to, with the
  join key and SQL join pattern, in BOTH directions (outbound parent FK AND
  inbound child joins), plus any bridge path to relate two entities that do not
  join directly. Reference ONLY tables that exist in this semantic layer.
- **Common query patterns**: 1–3 plain-English or short SQL examples of how the
  table is typically queried; phrase at least one in the user's natural-language
  terms.
- **Sample data**: the first few sampled rows, if available.
- **Notes**: caveats, known data-quality issues, or relationships.

## Description quality rules
- State the business meaning, not just the column name.
- If a column is a foreign key, state which entity it references AND the join
  pattern.
- If numeric: include units and whether it can be negative.
- If the sample reveals a controlled vocabulary, list the key values.
- The ## Columns table MUST list EVERY column in the schema — completeness over
  prose. A column missing here is a column the query agent cannot use.
- Cross-references must stay INSIDE this layer: never invent a plausible table
  that was not materialized here.
"""

# Markdown skeleton the composer fills in. Copied verbatim in structure from the
# metadata agent's "Metadata document format" so the KB ingests the same rich
# section layout the query agent's slice parser expects.
KB_DOCUMENT_FORMAT = """\
# {catalog_id}.{database_name}.{table_name}

## Overview
{{table_description — business entity, grain, business purpose}}

## Business Purpose
{{who uses this data, for what decisions or processes}}

## Business Concepts & Synonyms
{{everyday terms, synonyms, and natural-language questions this table answers —
even when they differ from the literal column/table names — each mapped to the
column(s) that carry them. Omit only if the table is purely technical.}}

## Reference Tables
{{each reference/lookup table this table joins to, with join key and SQL join
pattern; include BOTH outbound (this table's FK -> parent) AND inbound joins,
plus any bridge path. Omit this section entirely if none identified.}}

## Common Query Patterns
{{1–3 plain-English or short SQL examples of how this table is typically queried}}

## Columns
| Column | Type | Description |
|--------|------|-------------|

## Sample Data
{{first 3 rows from the sample, if available}}

## Notes
{{any caveats, known data-quality issues, or relationships}}
"""


def _build_context_section(
    *,
    use_cases_description: str,
    data_sources_description: str,
) -> str:
    """Build the optional domain-context section for the KB prompt.

    Args:
        use_cases_description: Domain use-cases context from the layer config.
        data_sources_description: Data-sources context from the layer config.

    Returns:
        A formatted context block, or ``""`` when neither field is supplied.
    """
    lines: List[str] = []
    if use_cases_description:
        lines.append(f"DOMAIN CONTEXT: {use_cases_description}")
    if data_sources_description:
        lines.append(f"DATA SOURCES CONTEXT: {data_sources_description}")
    return ("\n" + "\n".join(lines)) if lines else ""


def _build_layer_tables_section(layer_tables: List[str]) -> str:
    """Build the in-layer table inventory section for the KB prompt.

    Lists every table that actually exists in this semantic layer so the composer
    restricts its ## Reference Tables and query-pattern examples to real tables —
    never inventing a table that was not materialized here.

    Args:
        layer_tables: The bare table names that exist in this semantic layer.

    Returns:
        A formatted section string, or ``""`` when no inventory was supplied.
    """
    if not layer_tables:
        return ""
    names = ", ".join(f"`{t}`" for t in sorted(set(layer_tables)))
    return (
        f"\nTABLES IN THIS SEMANTIC LAYER ({len(set(layer_tables))}):\n  {names}\n"
        "Reference ONLY these tables in ## Reference Tables and ## Common Query "
        "Patterns. A join or redirect to a table NOT in this list is a "
        "hallucination — omit it instead."
    )


def build_kb_prompt(
    *,
    database_name: str,
    table_name: str,
    catalog_id: str,
    schema_json: str,
    sample_json: str,
    use_cases_description: str = "",
    data_sources_description: str = "",
    layer_tables: Optional[List[str]] = None,
) -> str:
    """Build the per-table user prompt for the KB prose composer.

    Args:
        database_name: Glue/Athena database name.
        table_name: Table name within that database.
        catalog_id: Catalog identifier (used in the document header).
        schema_json: JSON string of the table's columns (name/type/comment).
        sample_json: JSON string of sampled rows for the ## Sample Data section.
        use_cases_description: Domain use-cases context from the layer config.
        data_sources_description: Data-sources context from the layer config.
        layer_tables: Bare table names in this layer; restricts cross-references.

    Returns:
        The fully-assembled user prompt string for the composer.
    """
    context_section = _build_context_section(
        use_cases_description=use_cases_description,
        data_sources_description=data_sources_description,
    )
    layer_tables_section = _build_layer_tables_section(layer_tables or [])
    doc_format = KB_DOCUMENT_FORMAT.format(
        catalog_id=catalog_id,
        database_name=database_name,
        table_name=table_name,
    )

    return f"""Compose the Knowledge Base markdown document for this table.

DATABASE:   {database_name}
TABLE:      {table_name}
CATALOG_ID: {catalog_id}
{context_section}
{layer_tables_section}

# Schema (JSON)
{schema_json}

# Sample rows (JSON)
{sample_json}

Write the document using EXACTLY this structure (fill in every section; omit a
section only where the rules above permit):

{doc_format}
"""


def build_kb_document(*, database_name: str, table_name: str, catalog_id: str,
                      body: str) -> str:
    """Assemble a minimal KB markdown document from a pre-composed body.

    Used when the KB body is supplied directly (e.g. a deterministic build path
    or a test) rather than composed by the model — guarantees the document
    always carries the ``# {catalog}.{database}.{table}`` header the query agent's
    slice parser keys on.

    Args:
        database_name: Glue/Athena database name.
        table_name: Table name within that database.
        catalog_id: Catalog identifier for the header.
        body: The already-composed markdown body (sections below the header).

    Returns:
        The full markdown document (header + body).
    """
    header = f"# {catalog_id}.{database_name}.{table_name}\n"
    return f"{header}\n{body.strip()}\n"


def build_sidecar_attributes(
    *,
    semantic_layer_id: str,
    semantic_layer_version: str,
    database_name: str,
    table_name: str,
    catalog_id: str,
) -> Dict[str, str]:
    """Build the Bedrock KB ``.metadata.json`` sidecar filter attributes.

    Only the five small filter/structural keys the query agent uses are
    persisted (semantic_layer_id/version for the retrieval filter; database/
    table/catalog for the Phase-1 candidate id). Copied in shape from the
    metadata agent's ``save_metadata_document_to_s3`` sidecar so the KB surfaces
    the same chunk-metadata attributes.

    Args:
        semantic_layer_id: DynamoDB partition key scoping the document.
        semantic_layer_version: Version sort-key (e.g. ``'v1'``).
        database_name: Query-time database name.
        table_name: Query-time table name.
        catalog_id: Query-time catalog id.

    Returns:
        The ``metadataAttributes`` dict for the sidecar JSON.
    """
    return {
        "semantic_layer_id": semantic_layer_id,
        "semantic_layer_version": semantic_layer_version,
        "database_name": database_name,
        "table_name": table_name,
        "catalog_id": catalog_id or "AWSDataCatalog",
    }
