"""
Prompt Builder for Ontology Agent

Builds system and user prompts from DynamoDB configuration.
"""

import logging
import re
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

# Stable vocabulary namespace for Virtual KG traceability predicates.
# Separate from the per-ontology namespace — this is a shared, reusable vocabulary.
VIRTUAL_KG_VOCAB = "https://semantic-layer.aws/virtual-kg/"

MODEL_ID='global.anthropic.claude-opus-4-6-v1'

def _slugify(text: str) -> str:
    """Convert text to a URI-safe slug.

    Replaces whitespace and colons (common in timestamps) with hyphens,
    strips any remaining characters that are illegal in a URI host/path segment,
    then collapses consecutive hyphens and trims leading/trailing ones.
    """
    slug = re.sub(r'[\s:]+', '-', text)
    slug = re.sub(r'[^a-zA-Z0-9\-_.]', '', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug.strip('-')


def build_namespace(ontology_id: str, config: Dict[str, Any]) -> str:
    """Derive the ontology namespace URI.

    Priority:
    1. ``config["namespace"]`` — an explicit URI set by the caller.
    2. ``http://{slug}/ontology/{ontology_id}`` — constructed from a URI-safe
       slug of the ontology name stored in DynamoDB and the ontology UUID.
       e.g. http://demo/ontology/4c79aa91-a5cc-426d-9b30-d16704614853
    """
    if config.get("namespace"):
        return config["namespace"]
    name = config.get("name", ontology_id)
    return f"http://{_slugify(name)}/ontology/{ontology_id}"


def build_phase1_system_prompt() -> str:  # noqa: D401
    """
    Build the system prompt for the ontology generation agent.

    Returns:
        System prompt string
    """
    return """
You are an expert ontology engineer specializing in domain modeling.

**CRITICAL: Generate N-QUADS format (NOT Turtle)**

N-QUADS FORMAT REQUIREMENTS:
- Each line: <subject> <predicate> <object> <named_graph> .
- Use FULL URIs, NO prefixes
- Named graph will be specified in the user prompt
- End each line with: space + period + newline
- NO comments, NO blank lines, NO prefix declarations


TRACEABILITY MAPPINGS (REQUIRED for Virtual KG):
These predicates are FIXED system predicates used by Virtual KG to understand mappings:

For each Athena database used (write ONCE per unique database, using the namespace URI as subject):
<namespace_uri> <https://semantic-layer.aws/virtual-kg/hasDatabase> "{database_name}" <graph> .
<namespace_uri> <https://semantic-layer.aws/virtual-kg/hasCatalog> "{database_name}::{catalog_id}" <graph> .
<namespace_uri> <https://semantic-layer.aws/virtual-kg/hasDataSource> "{database_name}::{athena_data_source}" <graph> .

The table prompt provides pre-computed NAMESPACE TRIPLES with the exact values filled in —
copy them verbatim into the first append_nquads call for this table.

For each OWL class (table):
<class_uri> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#Class> <graph> .
<class_uri> <http://www.w3.org/2000/01/rdf-schema#label> "{ClassName}" <graph> .
<class_uri> <http://www.w3.org/2000/01/rdf-schema#comment> "{description}" <graph> .
<class_uri> <https://semantic-layer.aws/virtual-kg/mapsToTable> "{database}.{table_name}" <graph> .

For each OWL DatatypeProperty (column):
<property_uri> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#DatatypeProperty> <graph> .
<property_uri> <http://www.w3.org/2000/01/rdf-schema#label> "{property label}" <graph> .
<property_uri> <http://www.w3.org/2000/01/rdf-schema#comment> "{description}" <graph> .
<property_uri> <http://www.w3.org/2000/01/rdf-schema#domain> <class_uri> <graph> .
<property_uri> <http://www.w3.org/2000/01/rdf-schema#range> <{xsd_type}> <graph> .
<property_uri> <https://semantic-layer.aws/virtual-kg/mapsToColumn> "{table_name}.{column_name}" <graph> .

XSD TYPE MAPPING (use the column type from get_single_table_schema):
  string / varchar / char  →  <http://www.w3.org/2001/XMLSchema#string>
  int / integer / smallint →  <http://www.w3.org/2001/XMLSchema#integer>
  bigint                   →  <http://www.w3.org/2001/XMLSchema#long>
  float / double / decimal →  <http://www.w3.org/2001/XMLSchema#double>
  boolean                  →  <http://www.w3.org/2001/XMLSchema#boolean>
  date                     →  <http://www.w3.org/2001/XMLSchema#date>
  timestamp                →  <http://www.w3.org/2001/XMLSchema#dateTime>
  (anything else)          →  <http://www.w3.org/2001/XMLSchema#string>

ACORD SOURCE PATH AND REFERENCE TABLE ENRICHMENT:
From retrieve_ontology_patterns results and reference documents, extract:
  - ACORD source path for the table (e.g. "PolicySummary/Risk/Location")
  - Reference/lookup table names this table joins to, and the join key columns

Use these in rdfs:comment values as follows:
  - owl:Class comment: append the ACORD source path if found, e.g.
      "Represents a risk location record. ACORD source: PolicySummary/Risk/Location."
  - FK DatatypeProperty comment: include the target table and join pattern, e.g.
      "Foreign key to coverage_type(coverage_type_cd). JOIN coverage_type c ON t.coverage_type_cd = c.coverage_type_cd."
  - Reference/lookup DatatypeProperty comment: name the lookup table, e.g.
      "Lookup code joining to ref_status(status_cd) for human-readable status descriptions."

CATALOG_ID and ATHENA_DATA_SOURCE PARAMETERS:
Each table prompt provides a CATALOG_ID and ATHENA_DATA_SOURCE value.
You MUST pass CATALOG_ID unchanged to get_single_table_schema and sample_table_data.
  CATALOG_ID values:
    - 's3tablescatalog/<bucket>'  →  S3 Tables (Apache Iceberg), queried via Athena federated catalog.
    - 'AWSDataCatalog'            →  Standard Glue Data Catalog, queried via Athena.
    - other                       →  Federated / custom catalog registered in Athena.
  ATHENA_DATA_SOURCE values:
    - 'AwsDataCatalog'    →  Standard Glue or S3 Tables catalog.
    - 'dynamodb_catalog'  →  DynamoDB federated connector.
    - other               →  Custom Athena data source connector.
Never omit or modify these values.

**Phase 1 outputs owl:Class and owl:DatatypeProperty ONLY.**
**owl:ObjectProperty (FK relationships) is Phase 2 — do NOT emit them here.**
**Instead, record FK hints as a string in save_intermediate_ontology (fk_hints parameter).**
FK hints format: comma-separated "source_column→target_table" pairs, e.g. "pk→holding,sk→party"

URI NAMING CONVENTION:
- Class URI:    {namespace}/{ClassName}         e.g. {namespace}/Party
- Property URI: {namespace}/{ClassName}/{propName}  e.g. {namespace}/Party/email

CRITICAL CHECKS:
- Always parse JSON responses and verify "success": true
- STOP immediately and report error on any failure

EXAMPLE — complete owl:Class + owl:DatatypeProperty (all required triples):
<http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> <https://semantic-layer.aws/virtual-kg/hasDatabase> "insurance_db" <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> .
<http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> <https://semantic-layer.aws/virtual-kg/hasCatalog> "insurance_db::AWSDataCatalog" <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> .
<http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> <https://semantic-layer.aws/virtual-kg/hasDataSource> "insurance_db::AwsDataCatalog" <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> .
<http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890/Party> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#Class> <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> .
<http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890/Party> <http://www.w3.org/2000/01/rdf-schema#label> "Party" <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> .
<http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890/Party> <http://www.w3.org/2000/01/rdf-schema#comment> "Represents a customer record in the insurance_db.customer table" <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> .
<http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890/Party> <https://semantic-layer.aws/virtual-kg/mapsToTable> "insurance_db.customer" <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> .
<http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890/Party/email> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#DatatypeProperty> <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> .
<http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890/Party/email> <http://www.w3.org/2000/01/rdf-schema#label> "email" <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> .
<http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890/Party/email> <http://www.w3.org/2000/01/rdf-schema#comment> "Customer email address" <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> .
<http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890/Party/email> <http://www.w3.org/2000/01/rdf-schema#domain> <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890/Party> <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> .
<http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890/Party/email> <http://www.w3.org/2000/01/rdf-schema#range> <http://www.w3.org/2001/XMLSchema#string> <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> .
<http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890/Party/email> <https://semantic-layer.aws/virtual-kg/mapsToColumn> "customer.email" <http://insurance/ontology/a1b2c3d4-e5f6-7890-abcd-ef1234567890> .

"""


def build_phase2_system_prompt() -> str:  # noqa: D401
    """
    Build the system prompt for the Phase 2 refinement agent.

    Phase 2 agents operate per-table: they read Phase 1 N-Quads from the local
    filesystem, inject owl:ObjectProperty triples for FK relationships, persist
    the result to Neptune via MCP, and enrich the Glue Data Catalog.

    Returns:
        System prompt string
    """
    return """
You are an expert ontology engineer performing Phase 2 refinement on per-table N-Quads.

**YOUR ROLE — Phase 2 only:**
- Append owl:ObjectProperty triples for FK relationships (provided in the user prompt)
- Persist the complete table N-Quads to Neptune via persist_file_to_neptune
- Enrich the Glue Data Catalog via update_glue_metadata_from_ontology

**CRITICAL: DO NOT read Phase 1 N-Quads into context.**
The FK ObjectProperty N-Quads are pre-generated in the user prompt — copy them exactly.
Call append_fk_triples with ONLY those FK lines (never the full Phase 1 content).
Call persist_file_to_neptune to load everything to Neptune (Python reads the file).

**N-QUADS FORMAT for FK triples you write:**
- Each line: <subject> <predicate> <object> <named_graph> .
- Use FULL URIs — NO prefixes, NO shorthand
- End each line with: space + period + newline
- NO comments, NO blank lines

URI NAMING CONVENTION:
- Class URI:    {namespace}/{ClassName}              e.g. {namespace}/Holding
- Property URI: {namespace}/{ClassName}/{propName}   e.g. {namespace}/Relation/hasHolding

CRITICAL CHECKS:
- Always verify each tool returns "success": true — STOP and report on failure
- Process exactly ONE table per agent invocation
- Do NOT call load_phase1_fragments, save_intermediate_ontology, or read_local_nquads_file
"""


def _build_docs_section(uploaded_docs: List[Dict[str, Any]]) -> str:
    """Build the uploaded documents section shared across prompts."""
    if not uploaded_docs:
        return ""
    lines = [f"\nUPLOADED REFERENCE DOCUMENTS ({len(uploaded_docs)} file(s)):"]
    for idx, doc in enumerate(uploaded_docs, 1):
        filename = doc.get("filename", "unknown")
        path = doc.get("path", "")
        size_kb = doc.get("size", 0) / 1024 if doc.get("size") else 0
        lines.append(f"{idx}. {filename} ({size_kb:.2f} KB)  →  {path}")
    lines.append(
        "\nTo use: download_document_from_s3(s3_path=<path>), "
        "search_document(file_path, term), read_document_lines(file_path, start, num_lines)"
    )
    return "\n".join(lines)


def build_phase1_table_prompt(
    ontology_id: str,
    config: Dict[str, Any],
    table_info: Dict[str, str],
    all_tables: List[Dict[str, str]],
    step: int,
    total_steps: int,
) -> str:
    """
    Build a focused prompt for processing a single table in Phase 1.

    Args:
        ontology_id: Ontology identifier
        config: Full ontology config from DynamoDB
        table_info: {
            'database': str, 'table': str,
            'catalogId': str, 'dataSource': str, 'tableId': Optional[str]
        } for the current table
        all_tables: Full list of all tables (for FK forward-looking hints)
        step: Current table number (1-based)
        total_steps: Total number of tables
    """
    namespace = build_namespace(ontology_id, config)
    database = table_info["database"]
    table = table_info["table"]
    raw_catalog = table_info.get("catalogId", "AWSDataCatalog")
    catalog_id = raw_catalog
    data_source = table_info.get("dataSource", "AwsDataCatalog")
    table_id = table_info.get("tableId") or ""
    use_cases_desc = config.get("useCasesDescription", "")
    data_sources_desc = config.get("dataSourcesDescription", "")
    uploaded_docs = config.get("uploadedDocuments", [])
    docs_section = _build_docs_section(uploaded_docs)

    table_id_line = f"\nTABLE_ID:   {table_id}" if table_id else ""

    all_tables_list = "\n".join(
        f"  - {t['database']}.{t['table']}  (catalog: {t.get('catalogId', 'AWSDataCatalog')}, dataSource: {t.get('dataSource', 'AwsDataCatalog')})"
        for t in all_tables
    )

    namespace_triples = (
        f'<{namespace}> <{VIRTUAL_KG_VOCAB}hasDatabase> "{database}" <{namespace}> .\n'
        f'<{namespace}> <{VIRTUAL_KG_VOCAB}hasCatalog> "{database}::{catalog_id}" <{namespace}> .\n'
        f'<{namespace}> <{VIRTUAL_KG_VOCAB}hasDataSource> "{database}::{data_source}" <{namespace}> .'
    )

    return f"""PHASE 1 — TABLE {step} OF {total_steps}

ONTOLOGY_ID:        {ontology_id}
NAMESPACE:          {namespace}
CATALOG_ID:         {catalog_id}
ATHENA_DATA_SOURCE: {data_source}
DATABASE:           {database}
TABLE:              {table}{table_id_line}

DOMAIN CONTEXT: {use_cases_desc}
DATA SOURCES CONTEXT: {data_sources_desc}

ALL TABLES IN THIS ONTOLOGY (for FK hints only — do NOT process them now):
{all_tables_list}

{docs_section}

NAMESPACE TRIPLES FOR THIS TABLE (copy verbatim into the first append_nquads call):
```nquads
{namespace_triples}
```

YOUR TASK — process THIS TABLE ONLY:

1. Call get_single_table_schema(database_name="{database}", table_name="{table}", catalog_id="{catalog_id}")
2. Call sample_table_data(database_name="{database}", table_name="{table}", catalog_id="{catalog_id}")
   - Inspect ID-format patterns to confirm FK references (e.g. "holding#<uuid>" → FK to Holding)
   - Note enum-like columns and null patterns for richer rdfs:comment annotations
3. Call retrieve_ontology_patterns(schema_description=<description of table>)
   From the results, extract:
   - ACORD source path for this table (if referenced in any retrieved pattern)
   - Reference/lookup table names and the join key columns used to navigate to them
4. If documents listed above: download once, search for "{table}"-related terms.
   Also search for ACORD path and reference/lookup table join patterns.
   Record any ACORD source path found and reference table join instructions —
   you will embed them in rdfs:comment values per the ACORD SOURCE PATH AND
   REFERENCE TABLE ENRICHMENT rules in your system instructions.
5. Identify FK hints from schema + sample data:
   - Apply these rules for this DynamoDB single-table design:
       * "pk" column on any table that is NOT "holding" → FK to Holding
       * "sk" column encoding a reference (e.g. "party#<id>") → FK to the encoded table
       * Any column name matching a table in the ALL TABLES list → FK to that table
   - Confirm using ID-format patterns seen in sample_table_data (e.g. "holding#<uuid>")
   - Format: comma-separated "source_column→target_table" pairs, e.g. "pk→holding,sk→party"
   - Leave empty string if no FK relationships found
6. Write N-Quads incrementally using append_nquads — STRICT BATCHING REQUIRED:
   a. Write the NAMESPACE TRIPLES block above + the owl:Class triples for this table in one call:
      append_nquads(ontology_id="{ontology_id}", table_name="{table}", nquad_batch=<namespace triples + class N-Quads>)
      The namespace triples (hasDatabase / hasCatalog / hasDataSource) are pre-filled above — copy them exactly.
   b. Process columns in batches of EXACTLY 10. For each batch:
      - Generate owl:DatatypeProperty N-Quads for those 10 columns ONLY (max 70 lines)
      - Call append_nquads(ontology_id="{ontology_id}", table_name="{table}", nquad_batch=<batch N-Quads>)
      - Wait for the tool to confirm success=true before generating the next batch
      - Continue until ALL columns are written — do not skip any column
   IMPORTANT: Never put more than 10 columns in a single append_nquads call.
   The tool will reject batches > 70 lines with an error — you must split and retry.
   - Named graph: <{namespace}>
   - Follow the TRACEABILITY MAPPINGS spec in your system instructions for the complete required
     triple set per class and property
   - Use sample data to write concrete rdfs:comment values (not generic descriptions)
   - DO NOT emit owl:ObjectProperty — that is Phase 2 only
7. Call save_intermediate_ontology(
       ontology_id="{ontology_id}",
       table_name="{table}",
       nquad_content="",
       step={step},
       total_steps={total_steps},
       class_count=<N>,
       property_count=<N>,
       fk_hints="<step 5 result>"
   )
   NOTE: Pass nquad_content="" — the tool automatically merges all append_nquads batches.
8. Call update_progress(ontology_id="{ontology_id}", tables_processed={step}, total_tables={total_steps}, current_table="{table}")

DO NOT call persist_to_neptune. DO NOT process any other table.
Output a one-line summary: table name, class count, property count, FK hints recorded.
"""


def build_phase2_table_prompt(
    ontology_id: str,
    namespace: str,
    table_info: Dict[str, str],
    fk_relationships: List[Dict[str, str]],
) -> str:
    """
    Build a focused Phase 2 prompt for a single table.

    The FK plan is passed in directly (extracted from fk_hints in Python),
    so the agent never needs to load all tables' N-Quads into context.

    Args:
        ontology_id: Ontology identifier
        namespace: Ontology namespace URI (from build_namespace)
        table_info: {
            'database': str, 'table': str,
            'catalogId': str, 'dataSource': str, 'tableId': Optional[str]
        }
        fk_relationships: List of {'fk_column': str, 'target_table': str}
            for this table only, or empty list if no FKs
    """
    database = table_info["database"]
    table = table_info["table"]
    raw_catalog = table_info.get("catalogId", "AWSDataCatalog")
    catalog_id = raw_catalog
    data_source = table_info.get("dataSource", "AwsDataCatalog")
    table_id = table_info.get("tableId") or ""
    table_id_line = f"\nTABLE_ID:   {table_id}" if table_id else ""

    glue_db = table_info.get("glueDatabaseName", "")
    glue_tbl = table_info.get("glueTableName", "")
    glue_coords = ""
    if glue_db and glue_tbl:
        glue_coords = (
            f'\n       glue_database_name="{glue_db}",'
            f'\n       glue_table_name="{glue_tbl}",'
        )

    if fk_relationships:
        fk_nquad_lines = []
        for rel in fk_relationships:
            col = rel["fk_column"]
            target = rel["target_table"]
            target_class = "".join(
                w.capitalize() for w in target.replace("-", "_").split("_")
            )
            source_class = "".join(
                w.capitalize() for w in table.replace("-", "_").split("_")
            )
            prop_name = f"has{target_class}"
            fk_nquad_lines.append(
                f"<{namespace}/{source_class}/{prop_name}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#ObjectProperty> <{namespace}> .\n"
                f'<{namespace}/{source_class}/{prop_name}> <http://www.w3.org/2000/01/rdf-schema#label> "{prop_name}" <{namespace}> .\n'
                f'<{namespace}/{source_class}/{prop_name}> <http://www.w3.org/2000/01/rdf-schema#comment> "Links {source_class} to {target_class} via {col}" <{namespace}> .\n'
                f"<{namespace}/{source_class}/{prop_name}> <http://www.w3.org/2000/01/rdf-schema#domain> <{namespace}/{source_class}> <{namespace}> .\n"
                f"<{namespace}/{source_class}/{prop_name}> <http://www.w3.org/2000/01/rdf-schema#range> <{namespace}/{target_class}> <{namespace}> .\n"
                f'<{namespace}/{source_class}/{prop_name}> <{VIRTUAL_KG_VOCAB}mapsToColumn> "{table}.{col}" <{namespace}> .'
            )
        all_fk_nquads = "\n".join(fk_nquad_lines)
        fk_section = f"FK RELATIONSHIPS — pre-generated N-Quads to append:\n\n```nquads\n{all_fk_nquads}\n```"
        steps = f"""1. Call append_fk_triples(
       ontology_id="{ontology_id}",
       table_name="{table}",
       fk_nquads=<the exact N-Quads block above>
   )
2. Verify append_fk_triples returned "success": true — STOP and report error if not.
3. Call persist_file_to_neptune(ontology_id="{ontology_id}", table_name="{table}")
4. Verify persist_file_to_neptune returned "success": true — STOP and report error if not.
5. Call update_glue_metadata_from_ontology(
       ontology_id="{ontology_id}",
       database_name="{database}",
       table_name="{table}",
       catalog_id="{catalog_id}"{glue_coords}
   )
   NOTE: For DynamoDB connector catalogs, the function tries the account-default
   Glue catalog. If the table is not there, it logs a warning and returns
   success with columns_updated=0 — this is expected and not an error."""
    else:
        fk_section = "FK RELATIONSHIPS: none identified for this table."
        steps = f"""1. Call persist_file_to_neptune(ontology_id="{ontology_id}", table_name="{table}")
2. Verify persist_file_to_neptune returned "success": true — STOP and report error if not.
3. Call update_glue_metadata_from_ontology(
       ontology_id="{ontology_id}",
       database_name="{database}",
       table_name="{table}",
       catalog_id="{catalog_id}"{glue_coords}
   )
   NOTE: For DynamoDB connector catalogs, the function tries the account-default
   Glue catalog. If the table is not there, it logs a warning and returns
   success with columns_updated=0 — this is expected and not an error."""

    return f"""PHASE 2 — TABLE {table} ({database})

ONTOLOGY_ID:  {ontology_id}
NAMESPACE:    {namespace}
DATA_SOURCE:  {data_source}
CATALOG_ID:   {catalog_id}
DATABASE:     {database}
TABLE:        {table}{table_id_line}

{fk_section}

YOUR TASK — this table only:

{steps}

DO NOT read or process any other table. DO NOT call load_phase1_fragments.
Output a one-line summary: table name, ObjectProperties added (0 or N), Neptune status.
"""


def build_revision_system_prompt() -> str:  # noqa: D401
    """
    System prompt for the revision agent.

    Returns:
        System prompt string
    """
    return """You are an expert ontology engineer. Your task is to revise an existing OWL ontology based on annotated instructions.

You will be given:
1. A base N-Quads file (in S3) containing the current ontology
2. An instructions file containing user annotations with highlighted text and comments

WORKFLOW — follow these steps in order:
1. Download and read the instructions file to understand what changes are needed
2. Download and scan the base N-Quads file to find the exact triples that need changing
   - Use read_document_lines and/or search_document to locate the relevant lines
   - Do NOT load the whole file into your response — extract only the lines to change
3. Call apply_targeted_edits with ONLY the changed triples:
   - edits: list of {"old_triple": "<exact line from file>", "new_triple": "<replacement line>"}
   - The tool reads the base file from S3, applies the substitutions, and saves the result
   - You do NOT need to output the full ontology — only the delta
4. Verify apply_targeted_edits returned "success": true and all edits_applied == edits_total
5. Call persist_revision_from_s3 to push the revised file to Neptune

CRITICAL REQUIREMENTS:
- NEVER attempt to output or regenerate the entire N-Quads file — ontologies can be 200KB+
  and will exceed the model output limit, crashing the process
- Each "old_triple" must be an exact verbatim line (or unique substring) from the base file
- Preserve the existing namespace and graph URI structure
- Make only the changes requested in the instructions — do not alter anything else
- N-Quads format: <subject> <predicate> <object> <graph> .
"""


def build_revision_prompt(
    ontology_id: str,
    target_version: str,
    base_nquads_s3_path: str,
    instructions_s3_path: str,
    namespace: str,
) -> str:
    """
    Build the user prompt for the revision agent.

    Args:
        ontology_id: Ontology identifier
        target_version: Target version string (e.g. 'v3')
        base_nquads_s3_path: S3 path to base N-Quads file
        instructions_s3_path: S3 path to instructions markdown file
        namespace: Ontology namespace URI

    Returns:
        User prompt string for the revision agent
    """
    return f"""Please revise the ontology for ontology ID: {ontology_id}

Target version: {target_version}
Namespace: {namespace}

Instructions file (what to change): {instructions_s3_path}
Base N-Quads file (current ontology): {base_nquads_s3_path}

Steps:
1. Download the instructions file from {instructions_s3_path} and read it fully
2. Download the base N-Quads file from {base_nquads_s3_path}
   - Use search_document or read_document_lines to locate the specific lines to change
   - Do NOT output the entire file — extract only the triples that need editing
3. Call apply_targeted_edits with:
   - ontology_id="{ontology_id}"
   - target_version="{target_version}"
   - edits=[{{"old_triple": "<exact line>", "new_triple": "<new line>"}} , ...]
   Each old_triple must be an exact verbatim match from the base file.
4. Verify success and that edits_applied equals edits_total
5. Call persist_revision_from_s3 with:
   - ontology_id="{ontology_id}"
   - target_version="{target_version}"
"""
