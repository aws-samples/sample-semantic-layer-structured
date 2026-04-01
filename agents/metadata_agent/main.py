"""
Metadata Generation Agent
Uses Strands SDK to enrich AWS Glue Data Catalog with AI-generated descriptions
and save metadata documents to S3 for Bedrock Knowledge Base ingestion.

ARCHITECTURE:
- Lambda invokes AgentCore with semantic-layer id
- Agent reads config from DynamoDB
- Each table entry contains catalogId, dataSource, databaseName, tableName
- Agent processes asynchronously in background thread
- Agent samples live data from Athena for context
- Agent generates business descriptions for tables and columns
- Agent writes descriptions back to Glue Data Catalog or S3 Table Metadata
- Agent saves metadata documents to S3 for Bedrock KB ingestion
- Agent updates DynamoDB with progress
"""

import os
import re
import sys
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import boto3
from bedrock_agentcore import BedrockAgentCoreApp
try:
    from opentelemetry import baggage as _otel_baggage
except ImportError:
    _otel_baggage = None  # type: ignore
from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models import BedrockModel
from botocore.config import Config
from boto3.dynamodb.conditions import Key as DKey

from .token_manager import count_tokens
from .prompt_builder import (
    MODEL_ID,
    SYSTEM_PROMPT,
    ANNOTATION_SYSTEM_PROMPT,
    build_table_prompt,
    build_annotation_prompt,
)
# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(threadName)s] %(message)s',
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# AgentCore app
# ---------------------------------------------------------------------------
app = BedrockAgentCoreApp(debug=True)

# ---------------------------------------------------------------------------
# Boto3 session (injectable for notebooks / tests)
# ---------------------------------------------------------------------------
_boto_session: Optional[boto3.Session] = None


def set_boto_session(session: boto3.Session) -> None:
    """Inject a boto3 Session (useful in notebooks and tests)."""
    global _boto_session
    _boto_session = session
    logger.info(f"Boto3 session set with region: {session.region_name}")


def get_boto_session() -> boto3.Session:
    """Return the active boto3 Session, creating a default one if needed."""
    global _boto_session
    if _boto_session is None:
        region = os.environ.get('AWS_REGION')
        if not region:
            temp = boto3.Session()
            region = temp.region_name or 'us-east-1'
        _boto_session = boto3.Session(region_name=region)
        logger.info(f"Created default boto3 session with region: {region}")
    return _boto_session


# ===========================================================================
# TOOL — retrieve_ontology_patterns
# ===========================================================================

@tool
def retrieve_ontology_patterns(
    schema_description: str, max_patterns: int = 5
) -> Dict[str, Any]:
    """
    Retrieve relevant ontology design patterns from Bedrock Knowledge Base.

    Args:
        schema_description: Description of the schema for pattern matching
        max_patterns: Maximum number of patterns to retrieve

    Returns:
        Retrieved ontology patterns with relevance scores
    """
    session = get_boto_session()
    bedrock_agent = session.client("bedrock-agent-runtime")
    kb_id = os.environ.get("KNOWLEDGE_BASE_ID")

    try:
        response = bedrock_agent.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": schema_description},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": max_patterns}
            },
        )

        patterns = []
        for result in response["retrievalResults"]:
            patterns.append(
                {
                    "content": result["content"]["text"],
                    "score": result["score"],
                    "metadata": result.get("metadata", {}),
                }
            )

        logger.info(
            f"Retrieved {len(patterns)} ontology patterns from Knowledge Base '{kb_id}'"
        )
        return json.dumps({"status": "success", "patterns": patterns})
    except Exception as e:
        logger.error(f"Error retrieving ontology patterns from Knowledge Base '{kb_id}': {e}")
        return json.dumps({"status": "error", "error": str(e), "patterns": []})



# ===========================================================================
# TOOL — get_single_table_schema
# ===========================================================================

@tool
def get_single_table_schema(
    database_name: str, table_name: str, catalog_id: str
) -> str:
    """
    Get schema information for a single table via Athena DESCRIBE TABLE.

    Works for all catalog types including S3 Tables (Iceberg) catalogs —
    pass 's3tablescatalog/<bucket>' as catalog_id and Athena routes the query
    correctly using QueryExecutionContext.Catalog.

    Args:
        database_name: Athena/Glue database name
        table_name: Table name within that database
        catalog_id: Catalog identifier for this table. Use the value provided in
                    the table prompt exactly as given.
                    e.g. 's3tablescatalog/<bucket>' for S3 Tables (Iceberg),
                    'AWSDataCatalog' for standard Glue tables.

    Returns:
        JSON string containing table schema details
    """
    import time

    logger.info(
        f"get_single_table_schema called: database='{database_name}', "
        f"table='{table_name}', catalog_id='{catalog_id}'"
    )
    session = get_boto_session()
    athena_error: Optional[Exception] = None

    # S3 Tables (Iceberg) catalogs reject `DESCRIBE "db"."table"` with
    # InvalidRequestException — skip Athena entirely and read from Glue directly.
    is_s3_tables = bool(catalog_id and catalog_id.startswith("s3tablescatalog/"))
    logger.info(
        f"Routing '{database_name}.{table_name}': "
        f"{'S3 Tables (Glue direct)' if is_s3_tables else 'Athena DESCRIBE TABLE'}"
    )

    if not is_s3_tables:
        try:
            athena = session.client("athena")

            bucket = os.environ.get("ARTIFACTS_BUCKET", "")
            output_location = f"s3://{bucket}/athena-results/" if bucket else None
            workgroup = os.environ.get("ATHENA_WORKGROUP", "primary")

            # Use the catalog_id exactly as registered in Athena.
            athena_catalog = catalog_id
            query_context: Dict[str, str] = {"Database": database_name}
            if athena_catalog and athena_catalog not in ("AWSDataCatalog", "AwsDataCatalog"):
                query_context["Catalog"] = athena_catalog

            start_kwargs: Dict[str, Any] = {
                "QueryString": f'DESCRIBE "{database_name}"."{table_name}"',
                "QueryExecutionContext": query_context,
                "WorkGroup": workgroup,
            }
            if output_location:
                start_kwargs["ResultConfiguration"] = {"OutputLocation": output_location}

            resp = athena.start_query_execution(**start_kwargs)
            qid = resp["QueryExecutionId"]
            logger.info(
                f"Athena DESCRIBE TABLE submitted: query_id='{qid}', "
                f"workgroup='{workgroup}', catalog='{athena_catalog or 'default'}'"
            )

            max_wait, waited = 60, 0
            last_state = None
            while waited < max_wait:
                status = athena.get_query_execution(QueryExecutionId=qid)
                state = status["QueryExecution"]["Status"]["State"]
                if state != last_state:
                    logger.info(f"Athena query '{qid}' state: {state} (waited {waited}s)")
                    last_state = state
                if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                    break
                time.sleep(2)  # nosemgrep: arbitrary-sleep - intentional Athena query status polling loop
                waited += 2

            if state != "SUCCEEDED":
                reason = status["QueryExecution"]["Status"].get(
                    "StateChangeReason", "Unknown"
                )
                raise RuntimeError(f"DESCRIBE TABLE query {state}: {reason}")

            results = athena.get_query_results(QueryExecutionId=qid)
            raw_rows = results["ResultSet"]["Rows"]
            logger.info(
                f"Athena query '{qid}' returned {len(raw_rows)} raw rows "
                f"(including header) for '{database_name}.{table_name}'"
            )
            columns = []
            for row in raw_rows[1:]:  # skip result-set header row
                data = row.get("Data", [])
                if len(data) < 2:
                    continue
                col_name = data[0].get("VarCharValue", "").strip()
                col_type = data[1].get("VarCharValue", "").strip()
                col_comment = (
                    data[2].get("VarCharValue", "").strip() if len(data) > 2 else ""
                )
                # DESCRIBE output includes section headers like "# Partition Information"
                if not col_name or col_name.startswith("#") or col_type == "":
                    continue
                columns.append({"name": col_name, "type": col_type, "comment": col_comment})

            # Fetch existing table description from Glue (best-effort).
            # Annotation mode uses this to preserve the current description when
            # the table itself is not an annotation target.
            existing_table_desc = ""
            try:
                glue_desc_kwargs: Dict[str, Any] = {
                    "DatabaseName": database_name, "Name": table_name
                }
                if athena_catalog and athena_catalog not in ("AWSDataCatalog", "AwsDataCatalog"):
                    glue_desc_kwargs["CatalogId"] = athena_catalog
                existing_table_desc = (
                    session.client("glue")
                    .get_table(**glue_desc_kwargs)["Table"]
                    .get("Description", "")
                )
            except Exception as _e:
                logger.debug("Failed to retrieve existing table description from Glue: %s", _e)  # nosec B110

            table_schema = {
                "database_name": database_name,
                "table_name": table_name,
                "table_description": existing_table_desc,
                "columns": columns,
                "total_columns": len(columns),
                "source": "athena_describe",
            }
            json_str = json.dumps(table_schema)
            table_schema["token_estimate"] = count_tokens(json_str)
            if table_schema["token_estimate"] > 10000:
                logger.warning(
                    f"Large table schema for {database_name}.{table_name}: {table_schema['token_estimate']} tokens ({len(columns)} columns)"
                )
            logger.info(
                f"Retrieved '{database_name}.{table_name}' via Athena DESCRIBE TABLE "
                f"with {len(columns)} columns (catalog: '{catalog_id or 'default'}')"
            )
            return json.dumps(table_schema)

        except Exception as e:
            logger.error(
                f"Error retrieving table schema for '{database_name}.{table_name}': {e}"
            )
            athena_error = e

    # Glue fallback: used directly for S3 Tables (Iceberg) catalogs, and as a fallback
    # for DynamoDB-backed tables (StorageDescriptor.Location = "arn:aws:dynamodb:...")
    # which cause Athena DESCRIBE to fail with java.net.URISyntaxException.
    logger.info(
        f"{'Reading' if is_s3_tables else 'Falling back to'} Glue catalog for "
        f"'{database_name}.{table_name}' (catalog_id='{catalog_id}')"
    )
    try:
        glue = session.client("glue")
        get_kwargs: Dict[str, Any] = {"DatabaseName": database_name, "Name": table_name}
        if catalog_id and catalog_id not in ("AWSDataCatalog", "AwsDataCatalog"):
            get_kwargs["CatalogId"] = catalog_id
        tbl = glue.get_table(**get_kwargs)["Table"]
        sd = tbl.get("StorageDescriptor", {})
        table_type = tbl.get("TableType", "UNKNOWN")
        location = sd.get("Location", "")
        logger.info(
            f"Glue table '{database_name}.{table_name}' fetched: "
            f"type='{table_type}', location='{location}'"
        )
        columns = [
            {
                "name": col["Name"],
                "type": col["Type"],
                "comment": col.get("Comment", ""),
            }
            for col in sd.get("Columns", [])
        ]
        partition_keys = tbl.get("PartitionKeys", [])
        if partition_keys:
            logger.info(
                f"Appending {len(partition_keys)} partition key(s) to schema for "
                f"'{database_name}.{table_name}': "
                f"{[pk['Name'] for pk in partition_keys]}"
            )
        for pk in partition_keys:
            columns.append({
                "name": pk["Name"],
                "type": pk["Type"],
                "comment": pk.get("Comment", ""),
            })
        if columns:
            source = "glue_s3tables" if is_s3_tables else "glue_catalog_fallback"
            table_schema = {
                "database_name": database_name,
                "table_name": table_name,
                "table_description": tbl.get("Description", ""),
                "columns": columns,
                "total_columns": len(columns),
                "source": source,
                "location": sd.get("Location", ""),
            }
            if athena_error:
                table_schema["athena_error"] = str(athena_error)
            table_schema["token_estimate"] = count_tokens(json.dumps(table_schema))
            if is_s3_tables:
                logger.info(
                    f"Retrieved '{database_name}.{table_name}' via Glue (S3 Tables) "
                    f"with {len(columns)} columns"
                )
            else:
                logger.info(
                    f"Retrieved '{database_name}.{table_name}' via Glue catalog fallback "
                    f"({len(columns)} columns; Athena error: {athena_error})"
                )
            return json.dumps(table_schema)
    except Exception as glue_error:
        logger.error(
            f"Glue catalog{' ' if is_s3_tables else ' fallback '}failed for "
            f"'{database_name}.{table_name}': {glue_error}"
        )
        glue_error_val = glue_error

    top_error = athena_error or glue_error_val  # type: ignore[possibly-undefined]
    return json.dumps(
        {"error": str(top_error), "database_name": database_name, "table_name": table_name}
    )


# ===========================================================================
# DynamoDB scan helper (used as fallback when Athena can't query DynamoDB tables)
# ===========================================================================

def _try_dynamodb_scan(
    session: boto3.Session, database_name: str, table_name: str, limit: int
) -> 'Optional[str]':
    """
    Attempt a DynamoDB Scan on a Glue-registered DynamoDB table.

    Used when Athena fails with URISyntaxException because the Glue table
    StorageDescriptor.Location is a DynamoDB ARN rather than an S3 path.

    Returns serialised JSON matching sample_table_data's output format,
    or None if the table is not DynamoDB-backed or the scan fails.
    """
    try:
        glue = session.client('glue')
        tbl = glue.get_table(DatabaseName=database_name, Name=table_name)['Table']
        location = tbl.get('StorageDescriptor', {}).get('Location', '')
        if not location.startswith('arn:aws:dynamodb:'):
            return None
        # ARN format: arn:aws:dynamodb:<region>:<account>:table/<TABLE_NAME>
        parts = location.split('/')
        if len(parts) < 2:
            return None
        dynamo_table_name = parts[-1]

        region = os.environ.get('AWS_REGION', 'us-east-1')
        dynamodb = session.client('dynamodb', region_name=region)
        resp = dynamodb.scan(TableName=dynamo_table_name, Limit=limit)
        items = resp.get('Items', [])
        if not items:
            logger.info(f"DynamoDB scan returned no items for {dynamo_table_name}")
            return json.dumps({
                'database_name': database_name, 'table_name': table_name,
                'columns': [], 'rows': [], 'row_count': 0,
                'source': 'dynamodb_scan_fallback',
            })

        # Collect all attribute names as column headers
        all_keys: list = sorted({k for item in items for k in item.keys()})
        rows = []
        for item in items:
            row = []
            for col in all_keys:
                val = item.get(col, {})
                # DynamoDB returns typed dicts: {'S': '...'}, {'N': '...'}, {'BOOL': True}, etc.
                row.append(str(next(iter(val.values()))) if val else '')
            rows.append(row)

        logger.info(f"DynamoDB scan fallback: {len(rows)} rows from {dynamo_table_name}")
        return json.dumps({
            'database_name': database_name,
            'table_name': table_name,
            'columns': all_keys,
            'rows': rows,
            'row_count': len(rows),
            'source': 'dynamodb_scan_fallback',
        })
    except Exception as e:
        logger.warning(f"DynamoDB scan fallback failed for {database_name}.{table_name}: {e}")
        return None


# ===========================================================================
# TOOL — sample_table_data
# ===========================================================================

@tool
def sample_table_data(
    database_name: str, table_name: str, catalog_id: str, sample_size: int = 10
) -> str:
    """
    Execute a sample SELECT query on an Athena table for exploratory data analysis.

    Use this after get_single_table_schema() to inspect actual data values and
    identify patterns that the schema alone cannot reveal, such as:
    - ID format conventions (e.g. "holding#<uuid>", "party#<id>") that confirm FK references
    - Enum-like columns with a small set of distinct values
    - Columns that are consistently null or sparse
    - Columns whose names are ambiguous until real values are seen

    The insights should inform:
    - richer rdfs:comment annotations
    - more accurate FK hints for Phase 2

    Works for all catalog types including S3 Tables (Iceberg) catalogs — pass
    's3tablescatalog/<bucket>' as catalog_id.

    Args:
        database_name: Glue/Athena database name
        table_name: Table name to sample
        catalog_id: Athena catalog identifier for this table. Use the value
                    provided in the table prompt exactly as given.
                    e.g. 's3tablescatalog/<bucket>' for S3 Tables (Iceberg),
                    'AWSDataCatalog' for standard Glue tables.
        sample_size: Number of rows to return (default: 10, max: 50)

    Returns:
        JSON with column names, sample rows, and query metadata
    """
    import time

    session = get_boto_session()
    athena = session.client("athena")

    sample_size = min(sample_size, 50)

    output_location = os.environ.get("ATHENA_OUTPUT_LOCATION")
    if not output_location:
        bucket = os.environ.get("ARTIFACTS_BUCKET")
        if not bucket:
            return json.dumps(
                {
                    "success": False,
                    "error": "Neither ATHENA_OUTPUT_LOCATION nor ARTIFACTS_BUCKET is set",
                }
            )
        output_location = f"s3://{bucket}/athena-results/"

    workgroup = os.environ.get("ATHENA_WORKGROUP", "primary")
    query = f'SELECT * FROM "{database_name}"."{table_name}" LIMIT {sample_size}'  # nosec B608 - table/database names sourced from Glue catalog (trusted AWS service, not user input)

    # Use the catalog_id exactly as registered in Athena (full 's3tablescatalog/<bucket>' name).
    athena_catalog = catalog_id
    query_context: Dict[str, str] = {"Database": database_name}
    if athena_catalog and athena_catalog not in ("AWSDataCatalog", "AwsDataCatalog"):
        query_context["Catalog"] = athena_catalog

    try:
        response = athena.start_query_execution(
            QueryString=query,
            QueryExecutionContext=query_context,
            ResultConfiguration={"OutputLocation": output_location},
            WorkGroup=workgroup,
        )
        query_execution_id = response["QueryExecutionId"]
        logger.info(
            f"Athena sample query started: {query_execution_id} for {database_name}.{table_name}"
        )

        # Poll for completion (max 60 s)
        max_wait = 60
        waited = 0
        state = "RUNNING"
        while waited < max_wait:
            status = athena.get_query_execution(QueryExecutionId=query_execution_id)
            state = status["QueryExecution"]["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(2)  # nosemgrep: arbitrary-sleep - intentional Athena query status polling loop
            waited += 2

        if state != "SUCCEEDED":
            reason = status["QueryExecution"]["Status"].get(
                "StateChangeReason", "Unknown"
            )
            # Fallback: for DynamoDB-backed tables Athena fails with URISyntaxException.
            # Attempt a direct DynamoDB Scan to provide sample rows to the agent.
            dynamo_result = _try_dynamodb_scan(session, database_name, table_name, sample_size)
            if dynamo_result is not None:
                return dynamo_result
            return json.dumps({"success": False, "error": f"Query {state}: {reason}"})

        results = athena.get_query_results(QueryExecutionId=query_execution_id)
        columns = [
            col["Label"]
            for col in results["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
        ]

        rows = []
        for row in results["ResultSet"]["Rows"][1:]:  # skip header row
            row_data = {
                columns[i]: val.get("VarCharValue") for i, val in enumerate(row["Data"])
            }
            rows.append(row_data)

        logger.info(
            f"Sample query returned {len(rows)} rows for {database_name}.{table_name}"
        )
        return json.dumps(
            {
                "success": True,
                "database_name": database_name,
                "table_name": table_name,
                "columns": columns,
                "sample_rows": rows,
            }
        )

    except Exception as e:
        logger.error(f"Error sampling {database_name}.{table_name}: {e}")
        return json.dumps({"success": False, "error": str(e)})



# ===========================================================================
# S3 Tables versionToken helper (used by update_glue_table_metadata)
# ===========================================================================

def _fetch_s3tables_version_token(
    session: boto3.Session,
    effective_catalog: str,
    database_name: str,
    table_name: str,
) -> 'Optional[str]':
    """
    Fetch the current versionToken for an S3 Tables (Iceberg) table.

    Used as a fallback retry in update_glue_table_metadata when Glue federation
    raises FederationSourceException with 'versionToken null'.  Should NOT be
    called preemptively — injecting the token before the first attempt causes
    ValidationException on tables that Glue has already versioned internally.
    """
    bucket_name = effective_catalog.split('/', 1)[1] if '/' in effective_catalog else ''
    if not bucket_name:
        logger.warning(f'Cannot fetch versionToken: malformed catalog_id {effective_catalog!r}')
        return None
    try:
        account_id = session.client('sts').get_caller_identity()['Account']
        region = session.region_name or 'us-east-1'
        bucket_arn = f'arn:aws:s3tables:{region}:{account_id}:bucket/{bucket_name}'
        tbl = session.client('s3tables', region_name=region).get_table(
            tableBucketARN=bucket_arn, namespace=database_name, name=table_name
        )
        return tbl.get('versionToken')
    except Exception as e:
        logger.warning(f'Could not fetch S3 Tables versionToken for {table_name}: {e}')
        return None


# ===========================================================================
# S3 Tables / Iceberg column doc writer (used by update_glue_table_metadata)
# ===========================================================================

def _write_iceberg_docs_for_table(
    session: boto3.Session,
    catalog_id: str,
    database_name: str,
    table_name: str,
    table_description: str,
    col_desc: Dict[str, str],
) -> None:
    """
    Write column doc strings and table description directly to an S3 Tables
    (Iceberg) table's metadata using pyiceberg.

    Column doc strings are first-class Iceberg schema fields persisted in the
    Iceberg metadata JSON files in S3 — they are NOT subject to the 255-char
    Glue comment limit and survive independently of the Glue catalog.
    Table descriptions are stored as Iceberg table properties.

    Only called for S3 Tables catalogs (catalog_id starts with 's3tablescatalog/').
    All errors are non-fatal: logged as warnings and suppressed so the Glue
    write result is unaffected.
    """
    try:
        from pyiceberg.catalog import load_catalog  # type: ignore
    except ImportError:
        logger.warning("[Iceberg] pyiceberg not installed — skipping S3 Tables metadata update")
        return

    bucket = catalog_id.split("/", 1)[1]
    region = session.region_name or os.environ.get("AWS_REGION", "us-east-1")

    try:
        account_id = session.client("sts").get_caller_identity()["Account"]
    except Exception as sts_err:
        logger.warning(f"[Iceberg] Could not resolve AWS account ID: {sts_err} — skipping")
        return

    warehouse_arn = f"arn:aws:s3tables:{region}:{account_id}:bucket/{bucket}"
    try:
        catalog = load_catalog(
            "s3tables",
            **{
                "type": "rest",
                "uri": f"https://s3tables.{region}.amazonaws.com/iceberg",
                "warehouse": warehouse_arn,
                "rest.sigv4-enabled": "true",
                "rest.signing-region": region,
                "rest.signing-name": "s3tables",
            },
        )
    except Exception as cat_err:
        logger.warning(
            f"[Iceberg] Failed to initialise S3Tables catalog for {bucket}: {cat_err}"
        )
        return

    try:
        iceberg_table = catalog.load_table((database_name, table_name))
    except Exception as load_err:
        logger.warning(
            f"[Iceberg] Could not load table {database_name}.{table_name}: {load_err}"
        )
        return

    # Column doc strings — no 255-char limit, stored in Iceberg schema metadata
    if col_desc:
        try:
            # Build case-insensitive name map: Glue always returns lowercase names
            # but the Iceberg schema may use mixed case (e.g. PascalCase from DynamoDB).
            iceberg_fields_lower: Dict[str, str] = {
                f.name.lower(): f.name for f in iceberg_table.schema().fields
            }
            written = 0
            with iceberg_table.update_schema() as schema_update:
                for col_name, doc in col_desc.items():
                    canonical = iceberg_fields_lower.get(col_name.lower(), col_name)
                    try:
                        schema_update.update_column(canonical, doc=doc)
                        written += 1
                    except Exception as col_err:
                        logger.warning(
                            f"[Iceberg] Skipping column {col_name} ({database_name}.{table_name}): {col_err}"
                        )
            logger.info(
                f"[Iceberg] Wrote {written}/{len(col_desc)} column doc(s) for {database_name}.{table_name}"
            )
        except Exception as schema_err:
            logger.warning(
                f"[Iceberg] Schema update failed for {database_name}.{table_name}: {schema_err}"
            )

    # Table description — stored as an Iceberg table property
    if table_description:
        try:
            with iceberg_table.transaction() as txn:
                txn.set_properties({"description": table_description})
            logger.info(f"[Iceberg] Wrote table description for {database_name}.{table_name}")
        except Exception as prop_err:
            logger.warning(
                f"[Iceberg] Property update failed for {database_name}.{table_name}: {prop_err}"
            )


# ===========================================================================
# TOOL — update_glue_table_metadata
# ===========================================================================

@tool
def update_glue_table_metadata(
    database_name: str,
    table_name: str,
    table_description: str,
    column_descriptions: str,
    catalog_id: str = "",
) -> str:
    """
    Write AI-generated descriptions back to the AWS Glue Data Catalog for a table.

    Uses a read-then-write pattern: the existing table definition is fetched first
    so that all StorageDescriptor fields are preserved — only the Description and
    per-column Comment fields are updated.

    Args:
        database_name: Glue database name.
        table_name: Table to update.
        table_description: Business description for the table (max ~2000 chars).
        column_descriptions: JSON object mapping column name to description string,
                             e.g. '{"col_a": "Customer identifier", "col_b": "..."}'.
                             Column Comments are capped at 255 characters by Glue.
        catalog_id: Glue catalog ID. Leave empty for standard tables (auto-resolved).
                    Pass 's3tablescatalog/<bucket>' for S3 Tables (Iceberg).

    Returns:
        JSON string with success status and count of columns updated.
    """
    session = get_boto_session()
    glue = session.client('glue')


    resolved_catalog = catalog_id 
    effective_catalog = (
        resolved_catalog if resolved_catalog and resolved_catalog != 'AWSDataCatalog' else None
    )

    try:
        col_desc: Dict[str, str] = json.loads(column_descriptions) if isinstance(column_descriptions, str) else column_descriptions
    except json.JSONDecodeError as e:
        return json.dumps({'success': False, 'error': f'column_descriptions is not valid JSON: {e}'})

    try:
        get_kwargs: Dict[str, Any] = {'DatabaseName': database_name, 'Name': table_name}
        if effective_catalog:
            get_kwargs['CatalogId'] = effective_catalog
        table_input = glue.get_table(**get_kwargs)['Table']

        # Remove read-only fields that Glue returns in get_table() but rejects in update_table()
        # (includes S3 Tables / Iceberg federation fields)
        # ViewDefinition is stripped entirely for MV tables — its Representations[*].IsStale
        # field is not accepted by UpdateTable and cannot be selectively removed via the API.
        for field in (
            'CatalogId', 'DatabaseName', 'CreateTime', 'UpdateTime', 'CreatedBy',
            'IsRegisteredWithLakeFormation', 'VersionId', 'IsMultiDialectView',
            'Status', 'FederatedTable', 'IsMaterializedView', 'ViewDefinition',
        ):
            table_input.pop(field, None)

        # S3 Tables returns Owner="" which fails boto3 validation (min length 1)
        if not table_input.get('Owner'):
            table_input.pop('Owner', None)

        table_input['Description'] = table_description[:2048]

        cols_changed = 0
        for col in table_input.get('StorageDescriptor', {}).get('Columns', []):
            if col['Name'] in col_desc:
                col['Comment'] = col_desc[col['Name']][:255]
                cols_changed += 1

        for pk in table_input.get('PartitionKeys', []):
            if pk['Name'] in col_desc:
                pk['Comment'] = col_desc[pk['Name']][:255]
                cols_changed += 1

        update_kwargs: Dict[str, Any] = {
            'DatabaseName': database_name,
            'TableInput': table_input,
        }
        if effective_catalog:
            update_kwargs['CatalogId'] = effective_catalog

        # Attempt the update without VersionId first.
        # If Glue federation raises FederationSourceException with 'versionToken null'
        # (which can happen on freshly-registered S3 Tables when the federation layer
        # hasn't yet resolved the token internally), fetch the current S3 Tables
        # versionToken and retry once.
        #
        # We do NOT inject the token preemptively: after the first successful Glue
        # federation write the table gains an internal Glue VersionId (integer).
        # Passing an S3 Tables UUID token as that VersionId on any subsequent call
        # causes ValidationException: Unsupported Federation Resource.
        try:
            glue.update_table(**update_kwargs)
        except Exception as first_err:
            err_str = str(first_err)
            is_s3_tables = bool(effective_catalog and effective_catalog.startswith('s3tablescatalog/'))
            if is_s3_tables and 'versionToken' in err_str and 'null' in err_str:
                version_token = _fetch_s3tables_version_token(
                    session, effective_catalog, database_name, table_name
                )
                if version_token:
                    update_kwargs['VersionId'] = version_token
                    logger.info(
                        f'Retrying update_table with versionToken for {table_name}'
                    )
                    glue.update_table(**update_kwargs)
                else:
                    raise
            else:
                raise

        logger.info(f"Updated Glue table {database_name}.{table_name} — {cols_changed} columns")

        # For S3 Tables (Iceberg), also write doc strings directly into the Iceberg
        # schema metadata. Glue federation only updates Glue column Comments (255-char
        # limit); the Iceberg schema doc fields must be written separately via pyiceberg
        # so they are persisted in S3 and visible to Iceberg-native query engines.
        if effective_catalog and effective_catalog.startswith('s3tablescatalog/'):
            _write_iceberg_docs_for_table(
                session, effective_catalog, database_name, table_name,
                table_description, col_desc,
            )

        return json.dumps({
            'success': True,
            'database_name': database_name,
            'table_name': table_name,
            'columns_updated': cols_changed,
        })

    except Exception as e:
        err_str = str(e)
        # After _write_iceberg_docs_for_table updates the Iceberg schema via pyiceberg,
        # Glue federation's GetTable can start failing with ValidationException for
        # S3 Tables catalogs.  In that case, fall back to writing Iceberg docs directly
        # using the parameters already available — Glue column Comments are skipped but
        # the Iceberg schema (no 255-char limit, stored in S3) is still updated.
        is_s3_tables = bool(
            effective_catalog and effective_catalog.startswith('s3tablescatalog/')
        )
        if is_s3_tables and 'ValidationException' in err_str and 'Unsupported Federation Resource' in err_str:
            logger.warning(
                f"Glue federation unavailable for {database_name}.{table_name} "
                f"(ValidationException after Iceberg schema update) — writing Iceberg docs only"
            )
            try:
                _write_iceberg_docs_for_table(
                    session, effective_catalog, database_name, table_name,
                    table_description, col_desc,
                )
            except Exception as ice_err:
                logger.error(f"Iceberg fallback also failed for {database_name}.{table_name}: {ice_err}")
            return json.dumps({
                'success': True,
                'database_name': database_name,
                'table_name': table_name,
                'columns_updated': 0,
                'method': 'iceberg_only',
                'message': 'Glue federation unavailable; Iceberg metadata updated directly',
            })
        logger.error(f"update_glue_table_metadata error for {database_name}.{table_name}: {e}")
        return json.dumps({'success': False, 'error': str(e),
                           'database_name': database_name, 'table_name': table_name})



# ===========================================================================
# TOOL — save_metadata_document_to_s3
# ===========================================================================

@tool
def save_metadata_document_to_s3(
    database_name: str,
    table_name: str,
    catalog_id: str,
    metadata_content: str,
) -> str:
    """
    Save an enriched metadata document to S3 for Bedrock Knowledge Base ingestion.

    Writes two objects:
      s3://{ARTIFACTS_BUCKET}/metadata/{database_name}/{table_name}.md
      s3://{ARTIFACTS_BUCKET}/metadata/{database_name}/{table_name}.md.metadata.json

    The companion .metadata.json file makes database_name, catalog_id, and
    table_name available as Bedrock KB chunk metadata attributes so the query
    agent can reliably extract them during retrieval.

    Args:
        database_name: Database the table belongs to.
        table_name: Table name (used in the S3 key).
        catalog_id: Catalog identifier for this table (e.g. 'AWSDataCatalog' or
                    's3tablescatalog/<bucket>'). Stored as a KB metadata attribute.
        metadata_content: Markdown document describing the table, its purpose,
                          and each column — produced by the agent.

    Returns:
        JSON string with success status and s3_path.
    """
    session = get_boto_session()
    s3 = session.client('s3')
    bucket = os.environ.get('ARTIFACTS_BUCKET')

    if not bucket:
        return json.dumps({'success': False, 'error': 'ARTIFACTS_BUCKET env var not set'})

    key = f'metadata/{catalog_id}/{database_name}/{table_name}.md'

    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=metadata_content.encode('utf-8'),
            ContentType='text/markdown',
        )

        # Write companion metadata file so Bedrock KB surfaces these as chunk
        # metadata attributes (database_name, catalog_id, table_name).
        kb_metadata = {
            'metadataAttributes': {
                'database_name': database_name,
                'table_name': table_name,
                'catalog_id': catalog_id or 'AWSDataCatalog',
            }
        }
        s3.put_object(
            Bucket=bucket,
            Key=f'{key}.metadata.json',
            Body=json.dumps(kb_metadata).encode('utf-8'),
            ContentType='application/json',
        )

        s3_path = f's3://{bucket}/{key}'
        logger.info(f"Saved metadata document: {s3_path} (catalog_id={catalog_id})")
        return json.dumps({'success': True, 's3_path': s3_path, 'bucket': bucket, 'key': key})

    except Exception as e:
        logger.error(f"save_metadata_document_to_s3 error for {database_name}.{table_name}: {e}")
        return json.dumps({'success': False, 'error': str(e)})


# ===========================================================================
# Tool: update_progress
# ===========================================================================

def _version_num(v: str) -> int:
    """Parse the integer suffix from a version string like 'v1', 'v10'."""
    m = re.search(r'\d+', v or 'v0')
    return int(m.group()) if m else 0


def _resolve_active_version(table, job_id: str) -> str:
    """
    Return the version sort-key for the currently-active DynamoDB record.

    Queries all version records and returns the one with the highest numeric
    suffix (e.g. 'v10' > 'v9'). This is always correct — the lexicographic
    Limit=1 approach broke for v10+.

    Falls back to 'v1' if no record exists yet (first invocation of the
    initial build before DynamoDB is written).
    """
    try:
        resp = table.query(
            KeyConditionExpression=DKey('id').eq(job_id),
            ProjectionExpression='version',
        )
        items = resp.get('Items', [])
        if items:
            return max(items, key=lambda i: _version_num(i['version']))['version']
    except Exception as e:
        logger.warning(f"_resolve_active_version fallback to v1: {e}")
    return 'v1'


@tool
def update_progress(
    job_id: str,
    tables_processed: int,
    total_tables: int,
    current_table: str,
) -> str:
    """
    Record enrichment progress in DynamoDB so callers can poll for status.

    Args:
        job_id: Unique job identifier (from invocation payload).
        tables_processed: Number of tables fully enriched so far.
        total_tables: Total tables in the job.
        current_table: Name of the table just completed.

    Returns:
        JSON string with success status and progressPercent.
    """
    table_name = os.environ.get('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
    percent = int((tables_processed / total_tables) * 100) if total_tables > 0 else 0

    try:
        session = get_boto_session()
        dynamodb = session.resource('dynamodb')
        table = dynamodb.Table(table_name)

        active_version = _resolve_active_version(table, job_id)
        table.update_item(
            Key={'id': job_id, 'version': active_version},
            UpdateExpression=(
                'SET #status = :status, tablesProcessed = :processed, '
                'totalTables = :total, currentTable = :current, '
                'progressPercent = :percent, updatedAt = :updated'
            ),
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={
                ':status': 'processing',
                ':processed': tables_processed,
                ':total': total_tables,
                ':current': current_table,
                ':percent': percent,
                ':updated': datetime.now(timezone.utc).isoformat(),
            },
        )

        logger.info(f"Progress: {tables_processed}/{total_tables} ({percent}%) — {current_table}")
        return json.dumps({'success': True, 'progressPercent': percent,
                           'tablesProcessed': tables_processed, 'totalTables': total_tables})

    except Exception as e:
        logger.warning(f"update_progress non-fatal error: {e}")
        return json.dumps({'success': False, 'error': str(e)})


# ===========================================================================
# TOOL — download_document_from_s3
# ===========================================================================

@tool
def download_document_from_s3(s3_path: str) -> str:
    """
    Download a document from S3 to local filesystem for analysis.

    Use this tool to download uploaded reference documents (data dictionaries,
    glossaries, etc.) to local storage. After downloading, use search_document
    and read_document_lines tools to explore the content incrementally without
    loading the entire document into context.

    Args:
        s3_path: S3 path in format 's3://bucket/key' or just 'bucket/key'

    Returns:
        JSON string containing local file path and metadata
    """
    session = get_boto_session()
    s3 = session.client("s3")

    try:
        # Parse S3 path
        if s3_path.startswith("s3://"):
            s3_path = s3_path[5:]

        parts = s3_path.split("/", 1)
        if len(parts) != 2:
            return json.dumps(
                {
                    "error": f"Invalid S3 path format: {s3_path}. Expected: s3://bucket/key or bucket/key",
                    "local_path": None,
                }
            )

        bucket, key = parts
        filename = key.split("/")[-1]

        logger.info(f"Downloading document from S3: {bucket}/{key}")

        # Create temp directory for downloaded documents
        import tempfile

        temp_dir = tempfile.gettempdir()
        local_path = os.path.join(temp_dir, "metadata_docs", filename)

        # Ensure directory exists
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        # Download file
        s3.download_file(bucket, key, local_path)

        # Get file info
        file_size = os.path.getsize(local_path)

        # Try to detect if it's text or binary
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                f.read(1024)  # Try reading first 1KB as text
            content_type = "text"
        except UnicodeDecodeError:
            content_type = "binary"

        result = {
            "success": True,
            "s3_path": f"s3://{bucket}/{key}",
            "local_path": local_path,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": file_size,
            "instructions": "Use search_document() to search for terms, or read_document_lines() to read specific sections",
        }

        logger.info(
            f"Successfully downloaded: {filename} ({file_size} bytes) to {local_path}"
        )
        return json.dumps(result)

    except Exception as e:
        logger.error(f"Error downloading document from S3: {str(e)}")
        return json.dumps({"success": False, "error": str(e), "local_path": None})


# ===========================================================================
# TOOL — search_document
# ===========================================================================

@tool
def search_document(file_path: str, search_term: str, context_lines: int = 3) -> str:
    """
    Search for a term in a downloaded document and return matching lines with context.

    Use this to find relevant sections in reference documents without loading
    the entire content. Returns up to 10 matches with surrounding context.

    Args:
        file_path: Local file path from download_document_from_s3
        search_term: Term to search for (case-insensitive)
        context_lines: Number of lines before/after each match to include (default: 3)

    Returns:
        JSON string with search results showing matches and context
    """
    try:
        if not os.path.exists(file_path):
            return json.dumps(
                {
                    "success": False,
                    "error": f"File not found: {file_path}",
                    "matches": [],
                }
            )

        matches = []
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        search_term_lower = search_term.lower()

        for i, line in enumerate(lines):
            if search_term_lower in line.lower():
                # Get context lines
                start_idx = max(0, i - context_lines)
                end_idx = min(len(lines), i + context_lines + 1)

                context = "".join(lines[start_idx:end_idx])

                matches.append(
                    {
                        "line_number": i + 1,
                        "matched_line": line.strip(),
                        "context": context,
                    }
                )

                # Limit to 10 matches to avoid context overflow
                if len(matches) >= 10:
                    break

        result = {
            "success": True,
            "file_path": file_path,
            "search_term": search_term,
            "total_matches": len(matches),
            "matches": matches,
            "truncated": len(matches) >= 10,
        }

        logger.info(
            f"Search for '{search_term}' found {len(matches)} matches in {file_path}"
        )
        return json.dumps(result)

    except Exception as e:
        logger.error(f"Error searching document: {str(e)}")
        return json.dumps({"success": False, "error": str(e), "matches": []})


# ===========================================================================
# TOOL — read_document_lines
# ===========================================================================

@tool
def read_document_lines(
    file_path: str, start_line: int = 1, num_lines: int = 50
) -> str:
    """
    Read specific lines from a downloaded document.

    Use this to read sections of reference documents without loading the entire
    content into context. Useful for reading document sections after finding
    relevant areas with search_document.

    Args:
        file_path: Local file path from download_document_from_s3
        start_line: Line number to start reading from (1-indexed)
        num_lines: Number of lines to read (default: 50, max: 200)

    Returns:
        JSON string with the requested lines
    """
    try:
        if not os.path.exists(file_path):
            return json.dumps(
                {
                    "success": False,
                    "error": f"File not found: {file_path}",
                    "content": None,
                }
            )

        # Limit max lines to prevent context overflow
        num_lines = min(num_lines, 200)

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        total_lines = len(lines)
        start_idx = start_line - 1  # Convert to 0-indexed
        end_idx = start_idx + num_lines

        if start_idx < 0 or start_idx >= total_lines:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Invalid start_line: {start_line}. File has {total_lines} lines.",
                    "content": None,
                }
            )

        selected_lines = lines[start_idx:end_idx]
        content = "".join(selected_lines)

        result = {
            "success": True,
            "file_path": file_path,
            "start_line": start_line,
            "end_line": start_idx + len(selected_lines),
            "total_lines": total_lines,
            "content": content,
            "lines": selected_lines,
        }

        logger.info(f"Read lines {start_line}-{result['end_line']} from {file_path}")
        return json.dumps(result)

    except Exception as e:
        logger.error(f"Error reading document lines: {str(e)}")
        return json.dumps({"success": False, "error": str(e), "content": None})


# ===========================================================================
# System prompt
# ===========================================================================

# ===========================================================================
# Agent factory
# ===========================================================================

def create_metadata_agent(system_prompt: str = SYSTEM_PROMPT) -> Agent:
    """Create and configure the Metadata Generation Agent."""
    boto_config = Config(
        read_timeout=900,
        connect_timeout=60,
        retries={'max_attempts': 3, 'mode': 'adaptive'},
    )

    model = BedrockModel(
        model_id=MODEL_ID,
        temperature=0.2,
        max_tokens=16000,
        boto_session=get_boto_session(),
        boto_client_config=boto_config,
    )

    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[
            retrieve_ontology_patterns,
            get_single_table_schema,
            sample_table_data,
            update_glue_table_metadata,
            save_metadata_document_to_s3,
            update_progress,
            download_document_from_s3,
            search_document,
            read_document_lines,
        ],
        conversation_manager=SlidingWindowConversationManager(window_size=20),
    )


# ===========================================================================
# DynamoDB status helper
# ===========================================================================

def _write_versioned_completion(
    job_id: str, config: dict, target_version: str, summary: str,
    build_started_at: Optional[str] = None,
) -> None:
    """
    Write immutable history record + update v1 current-pointer.
    Called at the end of _run_annotation_mode when revisionMode=True.

    Mirrors ontology_agent._run_revision_mode() steps 6 & 7.

    Args:
        build_started_at: Timestamp captured at invocation time; preserved in
            both records so it isn't lost when put_item replaces the v1 item
            (update_item wrote it earlier but the in-memory config snapshot
            predates that write).
    """
    now = datetime.now(timezone.utc).isoformat()
    table_name = os.environ.get('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
    session = get_boto_session()
    table = session.resource('dynamodb').Table(table_name)

    # Step 6: write new version record (SK = target_version) as the active record
    history_item = {
        **config,
        'version': target_version,
        'status': 'completed',
        'revisionMode': False,
        'completedAt': now,
        'summary': summary,
    }
    if build_started_at:
        history_item['buildStartedAt'] = build_started_at
    history_item.pop('revisionInstructions', None)
    history_item.pop('targetVersion', None)
    history_item.pop('revisionBaseVersion', None)
    table.put_item(Item=history_item)
    logger.info(f'[Revision] Wrote new active record for version {target_version}')

    # Step 7: mark the previous version inactive — target_version is now the highest
    #         and becomes the active record automatically via _resolve_active_version.
    prev_version = config['version']
    table.update_item(
        Key={'id': job_id, 'version': prev_version},
        UpdateExpression='SET #status = :inactive, updatedAt = :now',
        ExpressionAttributeNames={'#status': 'status'},
        ExpressionAttributeValues={':inactive': 'inactive', ':now': now},
    )
    logger.info(f'[Revision] Marked version {prev_version} as inactive; {target_version} is now active')


def _run_annotation_mode(
    job_id: str,
    config: dict,
    tables_list: list,
    annotations: list,
    build_started_at: str,
) -> None:
    """
    Dedicated code path for annotation/revision runs (revisionMode=True).

    Mirrors ontology_agent._run_revision_mode(): completely separate from the
    normal enrichment path so the two cannot interfere.  Uses
    ANNOTATION_SYSTEM_PROMPT for every table and writes a versioned history
    record on completion.
    """
    target_version = config.get('targetVersion')
    valid_tables = [t for t in tables_list if t.get('database') and t.get('table')]
    total = len(valid_tables)
    failures: list = []
    processed = 0

    for idx, t in enumerate(valid_tables, 1):
        db = t['database']
        tbl = t['table']
        cat = t.get('catalogId', 'AWSDataCatalog')
        logger.info(f'[Revision] [Table {idx}/{total}] {db}.{tbl} (catalog: {cat})')
        try:
            agent = create_metadata_agent(system_prompt=ANNOTATION_SYSTEM_PROMPT)
            table_prompt = build_annotation_prompt(
                database_name=db,
                table_name=tbl,
                catalog_id=cat,
                step=idx,
                total_steps=total,
                job_id=job_id,
                annotations=annotations,
            )
            agent(table_prompt)
            processed += 1
        except Exception as table_err:
            logger.warning(
                f'[Revision] [Table {idx}/{total}] Failed {db}.{tbl}: {table_err} — skipping',
                exc_info=True,
            )
            failures.append(f'{db}.{tbl}')

    summary = f'Processed {processed}/{total} tables.'
    if failures:
        summary += f' Skipped: {", ".join(failures)}'

    logger.info(f'[Revision] Annotation completed for job={job_id}: {summary}')
    _write_versioned_completion(
        job_id, config, target_version, summary, build_started_at=build_started_at
    )
    _trigger_kb_ingestion()


def _update_dynamodb_status(job_id: str, status: str, version: str = None, **kwargs) -> None:
    """Write status to DynamoDB.

    Args:
        job_id: Job / config identifier.
        status: New status value.
        version: Explicit version sort-key to target. When omitted, resolves
            the active version via _resolve_active_version (highest numeric version).
    """
    table_name = os.environ.get('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
    try:
        session = get_boto_session()
        dynamodb = session.resource('dynamodb')
        table = dynamodb.Table(table_name)

        if version is None:
            version = _resolve_active_version(table, job_id)

        data = {'status': status, 'updatedAt': datetime.now(timezone.utc).isoformat(), **kwargs}
        update_expr = 'SET ' + ', '.join(f'#{k} = :{k}' for k in data)
        table.update_item(
            Key={'id': job_id, 'version': version},
            UpdateExpression=update_expr,
            ExpressionAttributeNames={f'#{k}': k for k in data},
            ExpressionAttributeValues={f':{k}': v for k, v in data.items()},
        )
        logger.info(f"DynamoDB status updated: {job_id} (version={version}) → {status}")
    except Exception as e:
        logger.error(f"_update_dynamodb_status failed: {e}")


def _trigger_kb_ingestion() -> None:
    """Fire-and-forget: start Bedrock KB ingestion after enrichment completes."""
    kb_id = os.environ.get('SEMANTIC_RAG_KB_ID')
    ds_id = os.environ.get('SEMANTIC_RAG_DATA_SOURCE_ID')
    if not kb_id or not ds_id:
        logger.warning("KB ingestion skipped: SEMANTIC_RAG_KB_ID or SEMANTIC_RAG_DATA_SOURCE_ID not set")
        return
    try:
        session = get_boto_session()
        client = session.client('bedrock-agent', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
        resp = client.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
        logger.info(f"KB ingestion started: {resp['ingestionJob']['ingestionJobId']}")
    except Exception as e:
        logger.error(f"KB ingestion trigger failed (non-fatal): {e}")


# ===========================================================================
# AgentCore entrypoint
# ===========================================================================

@app.entrypoint
def invoke(payload, context):
    """
    Main entrypoint for metadata generation agent.

    Receives id, reads config from DynamoDB, builds prompts,
    starts background processing, and returns immediately.

    Args:
        payload: Contains id
        context: Request context

    Returns:
        Immediate response with status 'processing'
    """
    id = payload.get("id")
    session_id = context.session_id if hasattr(context, "session_id") else str(uuid.uuid4())
    if _otel_baggage:
        _otel_baggage.set_baggage("session.id", session_id)

    if not id:
        return {"error": "id required in payload"}

    logger.info(f"[Entrypoint] Starting metadata enrichment for: {id}")

    # Read configuration from DynamoDB
    session = get_boto_session()
    dynamodb = session.resource("dynamodb")
    table_name = os.environ.get(
        "ONTOLOGY_METADATA_TABLE", "semantic-layer-metadata"
    )
    table = dynamodb.Table(table_name)

    active_version = _resolve_active_version(table, id)
    response = table.get_item(
        Key={"id": id, "version": active_version},
        ConsistentRead=True,
    )

    if "Item" not in response:
        return {"error": f"Metadata configuration not found: {id}"}

    config = response["Item"]

    # Extract context fields from config
    use_cases_description = config.get("useCasesDescription", "")
    data_sources_description = config.get("dataSourcesDescription", "")
    uploaded_docs = config.get("uploadedDocuments", [])
    annotations = config.get("revisionInstructions") or []

    # Update status to 'processing'; capture timestamp so the background thread
    # can include it in versioned records (put_item would otherwise lose it).
    build_started_at = datetime.now(timezone.utc).isoformat()
    _update_dynamodb_status(
        id,
        "processing",
        version=active_version,
        buildStartedAt=build_started_at,
    )

    # Parse tables list from config — all four identifiers are passed per-table through prompts
    tables_list = [
        {
            "database": ds["databaseName"],
            "table": ds["tableName"],
            "catalogId": ds.get("catalogId", "AWSDataCatalog"),
            "dataSource": ds.get("dataSource", "AwsDataCatalog"),
            "tableId": ds.get(
                "tableId"
            ),  # S3 Tables / Iceberg physical table identifier
        }
        for ds in config.get("dataSources", [])
        if ds.get("databaseName") and ds.get("tableName")
    ]
    total_tables = len(tables_list)
    logger.info(f"[Entrypoint] {total_tables} tables to process")

    # Start async task tracking
    task_id = app.add_async_task("metadata_enrichment", {"id": id})


    def background_work():
        try:
            # Revision path: completely separate from normal enrichment, matching
            # the ontology agent's pattern of gating on revisionMode (not on
            # whether annotations happen to be non-empty).
            if config.get('revisionMode'):
                logger.info(f'[Revision] Revision mode for {id}')
                _run_annotation_mode(id, config, tables_list, annotations, build_started_at)
                return

            # Normal enrichment path — always uses SYSTEM_PROMPT.
            valid_tables = [t for t in tables_list if t.get('database') and t.get('table')]
            total = len(valid_tables)
            failures: list = []
            processed = 0

            for idx, t in enumerate(valid_tables, 1):
                db = t['database']
                tbl = t['table']
                cat = t.get('catalogId', 'AWSDataCatalog')
                logger.info(f"[Table {idx}/{total}] {db}.{tbl} (catalog: {cat})")
                try:
                    agent = create_metadata_agent(system_prompt=SYSTEM_PROMPT)
                    table_prompt = build_table_prompt(
                        database_name=db,
                        table_name=tbl,
                        catalog_id=cat,
                        step=idx,
                        total_steps=total,
                        job_id=id,
                        use_cases_description=use_cases_description,
                        data_sources_description=data_sources_description,
                        uploaded_docs=uploaded_docs,
                    )
                    agent(table_prompt)
                    processed += 1
                except Exception as table_err:
                    logger.warning(
                        f"[Table {idx}/{total}] Failed {db}.{tbl}: {table_err} — skipping",
                        exc_info=True,
                    )
                    failures.append(f"{db}.{tbl}")

            if failures:
                logger.warning(f"[Background] Skipped {len(failures)} table(s): {failures}")

            summary = f"Processed {processed}/{total} tables."
            if failures:
                summary += f" Skipped: {', '.join(failures)}"

            logger.info(f"[Background] Enrichment completed for job={id}: {summary}")
            _update_dynamodb_status(
                id, 'completed',
                completedAt=datetime.now(timezone.utc).isoformat(),
                summary=summary,
            )
            _trigger_kb_ingestion()
        except Exception as e:
            logger.error(f"[Background] Enrichment failed for job={id}: {e}", exc_info=True)
            _update_dynamodb_status(
                id, 'failed',
                failedAt=datetime.now(timezone.utc).isoformat(),
                error=str(e),
            )
        finally:
            app.complete_async_task(task_id)
            sys.stdout.flush()
            sys.stderr.flush()

    threading.Thread(target=background_work, name='metadata-enrichment', daemon=True).start()

    return {
        'status': 'processing',
        'message': 'Metadata enrichment started in background',
        'jobId': id,
        'tableCount': len(tables_list),
        'task_id': task_id,
    }


# ===========================================================================
# Local run
# ===========================================================================

if __name__ == '__main__':
    logger.info("Starting Metadata Generation Agent")
    logger.info(f"  Region:           {os.environ.get('AWS_REGION', 'not set')}")
    logger.info(f"  Knowledge Base:   {os.environ.get('KNOWLEDGE_BASE_ID', 'not set')}")
    logger.info(f"  Artifacts Bucket: {os.environ.get('ARTIFACTS_BUCKET', 'not set')}")
    logger.info(f"  Metadata Table:   {os.environ.get('ONTOLOGY_METADATA_TABLE', 'not set')}")
    app.run()
