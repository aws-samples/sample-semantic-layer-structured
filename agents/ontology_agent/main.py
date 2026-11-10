"""
Ontology Generation Agent
Uses Strands SDK to generate OWL ontologies from database schemas and enrich AWS Glue Data Catalog with AI-generated descriptions
and save metadata documents to S3 for Bedrock Knowledge Base ingestion.

ARCHITECTURE:
- Lambda invokes AgentCore with semantic-layer id
- Agent reads config from DynamoDB
- Each table entry contains catalogId, dataSource, databaseName, tableName
- Agent builds prompts internally
- Agent processes asynchronously in background thread
- Agent updates DynamoDB with progress
"""

import os
import re
import logging
import threading
import contextvars
from datetime import datetime, timezone
import uuid
from bedrock_agentcore import BedrockAgentCoreApp
try:
    from opentelemetry import baggage as _otel_baggage
    from opentelemetry import context as _otel_context
    from opentelemetry import trace as _otel_trace
except ImportError:
    _otel_baggage = None  # type: ignore
    _otel_context = None  # type: ignore
    _otel_trace = None  # type: ignore
from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.hooks import AfterToolCallEvent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
import boto3
from boto3.dynamodb.conditions import Key as DKey
from botocore.config import Config
import json
from typing import Dict, List, Any, Optional, Tuple
from strands.types.exceptions import MaxTokensReachedException
try:
    from agents.shared.token_manager import count_tokens
    # set_boto_session re-exported for notebook/test credential injection.
    from agents.shared.boto_session import get_boto_session, set_boto_session  # noqa: F401
except ImportError:
    from shared.token_manager import count_tokens  # type: ignore
    from shared.boto_session import get_boto_session, set_boto_session  # type: ignore  # noqa: F401
from .enum_shapes import (
    MAX_ENUM_CARDINALITY,
    extract_enum_candidates,
    build_enum_shape_nquads,
    is_enum,
    is_enum_column,
)
from .prompt_builder import (
    build_namespace,
    build_phase1_system_prompt,
    build_phase2_system_prompt,
    build_phase1_table_prompt,
    build_phase2_table_prompt,
    build_revision_system_prompt,
    build_revision_prompt,
    VIRTUAL_KG_VOCAB,
    MODEL_ID
)
try:
    from shared.fk_plan import build_fk_plan  # type: ignore
except ImportError:  # pragma: no cover - repo-relative path outside container
    from agents.shared.fk_plan import build_fk_plan  # type: ignore

try:
    from shared.delta_fingerprint import (
        schema_hash, make_fingerprint, write_table_fingerprint,
        iceberg_snapshot_id, ddb_count_marker,
        is_count_free_catalog, unmeasurable_data_marker)  # type: ignore
except ImportError:  # pragma: no cover
    from agents.shared.delta_fingerprint import (
        schema_hash, make_fingerprint, write_table_fingerprint,
        iceberg_snapshot_id, ddb_count_marker,
        is_count_free_catalog, unmeasurable_data_marker)  # type: ignore

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Create AgentCore app instance with debug mode for task tracking
app = BedrockAgentCoreApp(debug=True)

# boto3 session management (set_boto_session / get_boto_session) is provided by
# agents.shared.boto_session, imported above.


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



# Cache of Athena catalog id -> catalog Type ('LAMBDA' for a federated connector,
# 'GLUE' for a Glue-backed one). An ontology build calls get_single_table_schema once
# per table over the same one or two catalogs, so this keeps it to one GetDataCatalog
# call per catalog per container rather than one per table.
_CATALOG_TYPE_CACHE: Dict[str, str] = {}


def _athena_catalog_type(*, catalog_id: str) -> str:
    """Look up an Athena data catalog's registered Type.

    Args:
        catalog_id: The catalog name exactly as registered in Athena
            (e.g. 'snowflake_fhir_db', 'dynamodb_catalog').

    Returns:
        The Type Athena reports — 'LAMBDA' for a federated connector catalog,
        'GLUE'/'HIVE' otherwise — or '' when the lookup failed (the catalog is not
        registered under that name, or the role lacks athena:GetDataCatalog). '' is
        not a silent fallback: the caller then routes on the catalog NAME, and a
        wrong route still fails loudly on the next Athena call rather than
        producing a wrong schema.
    """
    if catalog_id in _CATALOG_TYPE_CACHE:
        return _CATALOG_TYPE_CACHE[catalog_id]
    try:
        catalog = get_boto_session().client("athena").get_data_catalog(Name=catalog_id)
        catalog_type = str(catalog["DataCatalog"].get("Type", ""))
    except Exception as exc:  # noqa: BLE001 — an unknown Type must not abort the build
        logger.warning(
            f"Could not read the Athena catalog Type for '{catalog_id}' ({exc}); "
            f"falling back to name-based routing."
        )
        catalog_type = ""
    _CATALOG_TYPE_CACHE[catalog_id] = catalog_type
    return catalog_type


# How long to wait for a schema-introspection query. A federated connector Lambda
# cold-starts on the first query of a build — with a VPC-attached connector that can
# take well over a minute, and the old 60s budget expired mid-RUNNING on exactly one
# table per build. That is the worst possible failure mode here: the table falls
# through to the Glue fallback (which cannot resolve a federated database), the tool
# returns an error, and the agent models that one table from memory while the other
# five are real. Generous because this waits at most once per table and a LIMIT 0
# query scans nothing.
_SCHEMA_QUERY_MAX_WAIT_SECONDS = 300


def _schema_via_athena_select_metadata(
    *, session: Any, database_name: str, table_name: str, catalog_id: str
) -> Dict[str, Any]:
    """Read a table's columns from Athena's `SELECT * ... LIMIT 0` result metadata.

    This is the introspection path for Lambda-backed (federated) Athena catalogs —
    DynamoDB, Snowflake, and every other connector — because their SQL front ends
    reject a quoted schema-qualified DESCRIBE. The LIMIT 0 query scans no rows, and
    Athena still returns the full column list in ResultSetMetadata.ColumnInfo.

    Args:
        session: The boto3 session to create the Athena client from.
        database_name: Schema name within the federated catalog (e.g. 'FHIR_R4').
        table_name: Table name within that schema (e.g. 'PATIENTS').
        catalog_id: Athena catalog name as registered (e.g. 'snowflake_fhir_db').

    Returns:
        A table-schema dict with 'database_name', 'table_name', 'columns'
        (name/type/comment triples), 'total_columns' and 'source'.

    Raises:
        RuntimeError: If the query does not reach SUCCEEDED. Callers must not
            substitute a guessed schema — an ontology built from a guess reports
            success while describing columns that do not exist.
    """
    import time as _time

    athena = session.client("athena")

    output_location = os.environ.get("ATHENA_OUTPUT_LOCATION")
    if not output_location:
        bucket = os.environ.get("ARTIFACTS_BUCKET", "")
        if bucket:
            output_location = f"s3://{bucket}/athena-results/"

    workgroup = os.environ.get("ATHENA_WORKGROUP", "primary")
    start_kwargs: Dict[str, Any] = {
        "QueryString": f'SELECT * FROM "{database_name}"."{table_name}" LIMIT 0',  # nosec B608 - table/database names sourced from the Athena catalog (trusted AWS service, not user input)
        "QueryExecutionContext": {"Database": database_name, "Catalog": catalog_id},
        "WorkGroup": workgroup,
    }
    if output_location:
        start_kwargs["ResultConfiguration"] = {"OutputLocation": output_location}

    resp = athena.start_query_execution(**start_kwargs)
    qid = resp["QueryExecutionId"]
    logger.info(
        f"Athena SELECT LIMIT 0 submitted for federated connector '{catalog_id}': "
        f"query_id='{qid}', workgroup='{workgroup}', catalog='{catalog_id}'"
    )

    waited = 0
    last_state = None
    state = "QUEUED"
    status: Dict[str, Any] = {}
    while waited < _SCHEMA_QUERY_MAX_WAIT_SECONDS:
        status = athena.get_query_execution(QueryExecutionId=qid)
        state = status["QueryExecution"]["Status"]["State"]
        if state != last_state:
            logger.info(f"Athena query '{qid}' state: {state} (waited {waited}s)")
            last_state = state
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        _time.sleep(2)  # nosemgrep: arbitrary-sleep - intentional Athena query status polling loop
        waited += 2

    if state != "SUCCEEDED":
        # Name the two outcomes apart. A still-RUNNING query means the budget ran out,
        # not that the query is bad — reporting it as a failure sent the caller to the
        # Glue fallback and, when that also failed, left the agent to invent the schema.
        if state in ("QUEUED", "RUNNING"):
            raise RuntimeError(
                f"SELECT LIMIT 0 for '{database_name}.{table_name}' was still {state} "
                f"after {_SCHEMA_QUERY_MAX_WAIT_SECONDS}s (query_id='{qid}'); the "
                f"connector for '{catalog_id}' is likely cold-starting."
            )
        reason = status.get("QueryExecution", {}).get("Status", {}).get(
            "StateChangeReason", "Unknown"
        )
        raise RuntimeError(f"SELECT LIMIT 0 query {state}: {reason}")

    results = athena.get_query_results(QueryExecutionId=qid)
    col_info = results["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
    columns = [{"name": c["Name"], "type": c["Type"], "comment": ""} for c in col_info]

    table_schema: Dict[str, Any] = {
        "database_name": database_name,
        "table_name": table_name,
        "columns": columns,
        "total_columns": len(columns),
        "source": "athena_select_metadata",
    }
    table_schema["token_estimate"] = count_tokens(json.dumps(table_schema))
    logger.info(
        f"Retrieved '{database_name}.{table_name}' via Athena SELECT metadata "
        f"with {len(columns)} columns (catalog: '{catalog_id}')"
    )
    return table_schema


# Athena's parse-time rejection of `DESCRIBE "<schema>"."<table>"` on a Lambda-backed
# catalog. This is a deterministic signature of a federated catalog, so it is a
# reliable second chance at correct routing when the catalog Type lookup was denied.
_FEDERATED_DESCRIBE_SIGNATURE = "no viable alternative at input 'DESCRIBE"


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
    # Lambda-backed federated catalogs (any Athena connector: DynamoDB, Snowflake, …)
    # reject `DESCRIBE "db"."table"` — the connector's SQL front end cannot parse a
    # quoted schema-qualified DESCRIBE, and Athena fails it at submit time with
    # `InvalidRequestException: no viable alternative at input 'DESCRIBE "<db>"'`.
    # `SELECT * ... LIMIT 0` works and yields the full column list from
    # ResultSetMetadata.ColumnInfo.
    #
    # The catalog's registered TYPE decides the route, not its name. Testing for
    # 'dynamodb' in the name (as this did originally) silently mis-routes every other
    # connector: a Snowflake catalog took the DESCRIBE path, failed to parse, then fell
    # through to the Glue fallback — where a federated database does not exist at all —
    # so an ontology build over Snowflake never saw a real schema. The name check is
    # kept as a fallback for when GetDataCatalog is unavailable.
    is_federated_connector = False
    if catalog_id and not is_s3_tables and catalog_id not in (
        "AWSDataCatalog",
        "AwsDataCatalog",
    ):
        is_federated_connector = (
            "dynamodb" in catalog_id.lower()
            or _athena_catalog_type(catalog_id=catalog_id) == "LAMBDA"
        )
    if is_s3_tables:
        routing_label = "S3 Tables (Glue direct)"
    elif is_federated_connector:
        routing_label = f"Athena SELECT metadata (federated connector '{catalog_id}')"
    else:
        routing_label = "Athena DESCRIBE TABLE"
    logger.info(f"Routing '{database_name}.{table_name}': {routing_label}")

    # Federated (Lambda-backed) Athena connector: DESCRIBE fails with
    # "InvalidRequestException: no viable alternative at input 'DESCRIBE "default"'"
    # because the connector's SQL engine doesn't support double-quoted schema-qualified DESCRIBE.
    # SELECT * LIMIT 0 works and returns full column schema in ResultSetMetadata.ColumnInfo.
    if is_federated_connector:
        try:
            return json.dumps(
                _schema_via_athena_select_metadata(
                    session=session,
                    database_name=database_name,
                    table_name=table_name,
                    catalog_id=catalog_id,
                )
            )
        except Exception as e:
            logger.error(
                f"Athena SELECT LIMIT 0 failed for federated connector '{catalog_id}' "
                f"'{database_name}.{table_name}': {e}"
            )
            athena_error = e
        # fall through to Glue fallback

    if not is_s3_tables and not is_federated_connector:
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

            waited = 0
            last_state = None
            while waited < _SCHEMA_QUERY_MAX_WAIT_SECONDS:
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
                if state in ("QUEUED", "RUNNING"):
                    raise RuntimeError(
                        f"DESCRIBE TABLE for '{database_name}.{table_name}' was still "
                        f"{state} after {_SCHEMA_QUERY_MAX_WAIT_SECONDS}s "
                        f"(query_id='{qid}')."
                    )
                reason = status["QueryExecution"]["Status"].get(
                    "StateChangeReason", "Unknown"
                )
                raise RuntimeError(f"DESCRIBE TABLE query {state}: {reason}")

            # Athena returns <=1000 rows per page; loop NextToken to fetch the
            # FULL result set. A wide table (Spider2-scale, >1000 columns) yields
            # >1000 DESCRIBE rows, and a single call would silently truncate the
            # schema. Accumulate every page into raw_rows; the downstream [1:]
            # then skips only the page-1 header row, exactly as before.
            raw_rows = []
            next_token = ""
            while True:
                page_kwargs: Dict[str, Any] = {"QueryExecutionId": qid}
                if next_token:
                    page_kwargs["NextToken"] = next_token
                page = athena.get_query_results(**page_kwargs)
                raw_rows.extend(page["ResultSet"]["Rows"])
                next_token = page.get("NextToken", "") or ""
                if not next_token:
                    break
            logger.info(
                f"Athena query '{qid}' returned {len(raw_rows)} raw rows "
                f"(including header) for '{database_name}.{table_name}'"
            )
            columns = []
            _seen_cols = set()
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
                # Athena DESCRIBE repeats partition columns under the
                # "# Partition Information" section (they also appear in the main
                # list). Dedup by name (first occurrence wins) so the column SET
                # matches what the delta planner hashes (GlueService columns +
                # partitionKeys) — otherwise a partitioned table reads as
                # permanently schemaChanged.
                _key = col_name.lower()
                if _key in _seen_cols:
                    continue
                _seen_cols.add(_key)
                columns.append({"name": col_name, "type": col_type, "comment": col_comment})

            table_schema = {
                "database_name": database_name,
                "table_name": table_name,
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

            # Self-correct a mis-route. Athena rejecting the quoted DESCRIBE at parse
            # time means this catalog IS Lambda-backed, whatever the Type lookup said —
            # and that lookup is denied whenever the role lacks athena:GetDataCatalog,
            # in which case routing had fallen back to guessing from the catalog name.
            # Retry on the correct path rather than dropping through to Glue, where a
            # federated database does not exist: that fallback also fails, the tool
            # returns an error, and the agent writes the ontology from the model's
            # domain knowledge while the build still reports success.
            if _FEDERATED_DESCRIBE_SIGNATURE in str(e):
                logger.warning(
                    f"'{catalog_id}' rejected DESCRIBE at parse time, so it is a "
                    f"federated (Lambda-backed) catalog that was mis-routed — retrying "
                    f"'{database_name}.{table_name}' via Athena SELECT metadata. Grant "
                    f"this role athena:GetDataCatalog to route it correctly first time."
                )
                _CATALOG_TYPE_CACHE[catalog_id] = "LAMBDA"
                try:
                    return json.dumps(
                        _schema_via_athena_select_metadata(
                            session=session,
                            database_name=database_name,
                            table_name=table_name,
                            catalog_id=catalog_id,
                        )
                    )
                except Exception as retry_error:
                    logger.error(
                        f"Athena SELECT metadata retry also failed for "
                        f"'{database_name}.{table_name}' on '{catalog_id}': {retry_error}"
                    )
                    athena_error = retry_error

    # Glue catalog: used directly for S3 Tables (Iceberg) catalogs; fallback for all others
    # when Athena DESCRIBE fails (e.g. DynamoDB-backed Glue tables with arn:aws:dynamodb: location).
    glue_error_val: Exception | None = None
    logger.info(
        f"{'Reading' if is_s3_tables else 'Falling back to'} Glue catalog for "
        f"'{database_name}.{table_name}' (catalog_id='{catalog_id}')"
    )
    try:
        glue = session.client("glue")
        get_kwargs: Dict[str, Any] = {"DatabaseName": database_name, "Name": table_name}
        # Pass catalog_id to Glue for both federated connectors (e.g. 'dynamodb_catalog')
        # and non-default catalogs. Glue supports federated catalog IDs as CatalogId.
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


def _resolve_catalog(
    database_name: str, table_name: str, catalog_id: str
) -> tuple[str, str, str]:
    """
    Resolve the effective (database, table, catalog) for an Athena query, auto-detecting
    DynamoDB ARN-backed Glue-crawled tables and re-routing them via the DynamoDB connector.

    For standard Glue catalogs (AWSDataCatalog or empty catalog_id), this looks up the table
    in Glue and inspects its StorageDescriptor location. If the location is a DynamoDB ARN
    (``arn:aws:dynamodb:...``), the query is re-routed to the DynamoDB connector catalog
    (database ``default``, table = the DynamoDB table name, catalog = ``DYNAMODB_CONNECTOR_CATALOG``).
    Any non-Glue / S3 Tables catalog is passed through unchanged.

    Args:
        database_name: Glue/Athena database name as provided in the table prompt.
        table_name: Table name as provided in the table prompt.
        catalog_id: Athena catalog identifier (e.g. 'AWSDataCatalog', 's3tablescatalog/<bucket>').

    Returns:
        Tuple of (effective_database, effective_table, effective_catalog) to use for the query.
    """
    effective_database = database_name
    effective_table = table_name
    effective_catalog = catalog_id

    if not catalog_id or catalog_id in ("AWSDataCatalog", "AwsDataCatalog"):
        try:
            session = get_boto_session()
            glue = session.client("glue")
            glue_resp = glue.get_table(DatabaseName=database_name, Name=table_name)
            location = glue_resp["Table"]["StorageDescriptor"].get("Location", "")
            if location.startswith("arn:aws:dynamodb:"):
                dynamo_table_name = location.split("/")[-1]
                effective_database = "default"
                effective_table = dynamo_table_name
                effective_catalog = os.environ.get(
                    "DYNAMODB_CONNECTOR_CATALOG", "dynamodb_catalog"
                )
                logger.info(
                    f"DynamoDB ARN detected for {database_name}.{table_name} → "
                    f"re-routing via {effective_catalog}/default/{dynamo_table_name}"
                )
        except Exception as glue_err:
            logger.warning(
                f"Glue lookup failed for {database_name}.{table_name}: {glue_err}"
            )

    return effective_database, effective_table, effective_catalog


def _run_athena_query(
    query: str,
    database: str,
    catalog: str,
    *,
    max_wait_seconds: int = _SCHEMA_QUERY_MAX_WAIT_SECONDS,
) -> dict:
    """
    Execute a SQL query on Athena, polling for completion, and parse the result rows.

    Resolves the result output location (``ATHENA_OUTPUT_LOCATION`` env var, falling back to
    ``s3://<ARTIFACTS_BUCKET>/athena-results/``) and workgroup (``ATHENA_WORKGROUP``, default
    'primary'). Routes the query via ``catalog`` when it is not the default Glue catalog.

    On giving up (still QUEUED/RUNNING at the budget), the query is CANCELLED before
    returning. Abandoning it instead is not merely untidy: a federated connector has a
    small fixed Lambda concurrency, and a caller that loops over columns leaves one
    orphaned full-table scan behind per iteration. Those pile up, saturate the
    connector, and the *next* query fails with a misleading `TABLE_NOT_FOUND` — a
    load symptom that reads exactly like a schema error.

    Args:
        query: The SQL query string to execute.
        database: The database name to set in the Athena query execution context.
        catalog: The Athena catalog name; added to the query context only when it is not the
                 default Glue catalog ('AWSDataCatalog'/'AwsDataCatalog').
        max_wait_seconds: Seconds to poll before cancelling and returning failure.
                 Defaults to the schema-introspection budget; callers that run one query
                 per column should pass something much smaller, since the budget is paid
                 once per call and multiplies across the loop.

    Returns:
        dict with keys:
          - success (bool): True only when the query SUCCEEDED.
          - columns (list[str]): result column labels (empty on failure).
          - rows (list[dict]): list of column-label -> VarCharValue dicts (empty on failure).
          - error (str | None): error description on failure, else None.
    """
    import time

    session = get_boto_session()
    athena = session.client("athena")

    output_location = os.environ.get("ATHENA_OUTPUT_LOCATION")
    if not output_location:
        bucket = os.environ.get("ARTIFACTS_BUCKET")
        if not bucket:
            return {
                "success": False,
                "columns": [],
                "rows": [],
                "error": "Neither ATHENA_OUTPUT_LOCATION nor ARTIFACTS_BUCKET is set",
            }
        output_location = f"s3://{bucket}/athena-results/"

    workgroup = os.environ.get("ATHENA_WORKGROUP", "primary")

    # Route the query via the effective catalog when it is not the default Glue catalog
    query_context: Dict[str, str] = {"Database": database}
    if catalog and catalog not in ("AWSDataCatalog", "AwsDataCatalog"):
        query_context["Catalog"] = catalog

    try:
        response = athena.start_query_execution(
            QueryString=query,
            QueryExecutionContext=query_context,
            ResultConfiguration={"OutputLocation": output_location},
            WorkGroup=workgroup,
        )
        query_execution_id = response["QueryExecutionId"]
        logger.info(f"Athena query started: {query_execution_id}")

        # Poll for completion. Budget is the caller's: schema introspection needs room
        # for a federated connector's cold start, while a per-column probe loop must not
        # pay that budget once per column.
        max_wait = max_wait_seconds
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
            if state in ("QUEUED", "RUNNING"):
                # Free the connector's capacity instead of letting the abandoned scan
                # run on for minutes. Best-effort: a cancel failure must not mask the
                # timeout we are already reporting.
                try:
                    athena.stop_query_execution(QueryExecutionId=query_execution_id)
                    logger.warning(
                        f"Cancelled Athena query {query_execution_id} after "
                        f"{max_wait}s in state {state}"
                    )
                except Exception as cancel_err:
                    logger.warning(
                        f"Could not cancel Athena query {query_execution_id}: {cancel_err}"
                    )
                reason = f"still {state} after {max_wait}s (query_id={query_execution_id})"
            else:
                reason = status["QueryExecution"]["Status"].get(
                    "StateChangeReason", "Unknown"
                )
            return {
                "success": False,
                "columns": [],
                "rows": [],
                "error": f"Query {state}: {reason}",
            }

        # Athena returns <=1000 rows per page; loop NextToken to fetch the FULL
        # result set. Only page 1 carries the header row (index 0). Callers
        # currently LIMIT this query, but this is a shared executor of an
        # arbitrary `query` param — a single call would silently truncate any
        # future caller that fetches >1000 rows.
        columns: List[str] = []
        rows = []
        next_token = ""
        first_page = True
        while True:
            page_kwargs: Dict[str, Any] = {"QueryExecutionId": query_execution_id}
            if next_token:
                page_kwargs["NextToken"] = next_token
            page = athena.get_query_results(**page_kwargs)
            result_set = page["ResultSet"]
            if first_page:
                columns = [
                    col["Label"]
                    for col in result_set["ResultSetMetadata"]["ColumnInfo"]
                ]
                data_rows = result_set["Rows"][1:]  # skip page-1 header row
                first_page = False
            else:
                data_rows = result_set["Rows"]
            for row in data_rows:
                row_data = {
                    columns[i]: val.get("VarCharValue")
                    for i, val in enumerate(row["Data"])
                }
                rows.append(row_data)
            next_token = page.get("NextToken", "") or ""
            if not next_token:
                break

        logger.info(f"Athena query returned {len(rows)} rows")
        return {"success": True, "columns": columns, "rows": rows, "error": None}

    except Exception as e:
        logger.error(f"Error running Athena query: {e}")
        return {"success": False, "columns": [], "rows": [], "error": str(e)}


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
    sample_size = min(sample_size, 50)

    eff_db, eff_tbl, eff_cat = _resolve_catalog(database_name, table_name, catalog_id)

    query = f'SELECT * FROM "{eff_db}"."{eff_tbl}" LIMIT {sample_size}'  # nosec B608 - table/database names sourced from Glue catalog (trusted AWS service, not user input)

    res = _run_athena_query(query, eff_db, eff_cat)
    if not res["success"]:
        return json.dumps({"success": False, "error": res["error"]})

    return json.dumps(
        {
            "success": True,
            "database_name": eff_db,
            "table_name": eff_tbl,
            "columns": res["columns"],
            "sample_rows": res["rows"],
        }
    )


def select_distinct_values(database: str, table: str, column: str, catalog: str) -> dict:
    """Probe a column's distinct values with a cardinality cap (build-time enum discovery).

    Runs SELECT <col>, COUNT(*) GROUP BY <col> ORDER BY count DESC LIMIT
    MAX_ENUM_CARDINALITY+1, so a true enum returns within the cap while a
    high-cardinality column trips the truncation flag. Reuses _resolve_catalog +
    _run_athena_query (DynamoDB rerouting + polling).

    Args:
        database: Glue/Athena database name.
        table: table name.
        column: column to probe.
        catalog: Athena catalog id.
    Returns:
        {"values": [str,...], "distinct_count": int, "truncated": bool,
         "sample_rows": int, "error": str|None}. sample_rows is the total rows the
        distinct was computed over (sum of per-value COUNT(*)), a lower bound when
        the probe truncated.
        Fail-soft: on probe failure, values=[] / distinct_count=0 / truncated=False
        / sample_rows=0.
    """
    eff_db, eff_tbl, eff_cat = _resolve_catalog(database, table, catalog)
    limit = MAX_ENUM_CARDINALITY + 1

    # A GROUP BY over a federated (Lambda-backed) catalog is not the cheap scan it is on
    # S3 Tables. Athena pushes down the projection but NOT the aggregation, so the
    # connector's own log shows it running a bare `SELECT "<col>" FROM "<db>"."<tbl>"` and
    # streaming EVERY row back through a Lambda capped at 900s. Measured against Snowflake,
    # a single column's probe exceeded that ceiling and was killed
    # (`Status: timeout`), and the connector's abandoned invocations then congested every
    # later query for ~15 minutes. Multiplied by one probe per enum candidate per table,
    # this is what ran an ontology build past the runtime's limit and got the container
    # reaped mid-refinement.
    #
    # Bounding the SQL does not rescue it: an inner `LIMIT` is applied AFTER the connector
    # returns, so the generated remote SQL is unchanged (verified in the connector log).
    # There is no cheap form of this question over a federated catalog, so decline to ask
    # it and say so loudly. The caller already treats a failed probe as "no enum" and
    # emits no shape, which is the honest outcome — the alternative is not a cheaper
    # answer, it is a dead build. S3 Tables is unaffected: the scan is cheap there, and
    # changing it would silently move existing layers' shapes.
    if _is_federated_connector_catalog(eff_cat):
        message = (
            f"enum probe skipped: '{eff_cat}' is a federated connector catalog, where "
            f"GROUP BY is not pushed down and scanning '{eff_db}.{eff_tbl}' for column "
            f"'{column}' would stream the whole table through the connector Lambda "
            f"(measured: exceeds its 900s ceiling). No enum shape will be emitted for "
            f"this column."
        )
        logger.warning(f"[enum] {message}")
        return {"values": [], "distinct_count": 0, "truncated": False,
                "sample_rows": 0, "error": message}

    query = (
        f'SELECT "{column}" AS v, COUNT(*) AS c '  # nosec B608 - names from Glue catalog
        f'FROM "{eff_db}"."{eff_tbl}" '
        f'GROUP BY "{column}" ORDER BY c DESC LIMIT {limit}'
    )
    res = _run_athena_query(query, eff_db, eff_cat)
    if not res.get("success"):
        return {"values": [], "distinct_count": 0, "truncated": False,
                "sample_rows": 0, "error": res.get("error")}
    values = [r.get("v") for r in res["rows"] if r.get("v") is not None]

    # sample_rows = total rows the distinct was computed over (sum of per-value
    # COUNT(*) `c`), which feeds the distinct/total ratio signal in
    # is_enum_column. Coerce each `c` to int, guarding non-numeric/NULL as 0. If
    # the probe truncated (overflow row dropped), this is a lower bound — which
    # only makes the ratio gate stricter, so it is safe.
    sample_rows = 0
    for r in res["rows"]:
        try:
            sample_rows += int(r.get("c") or 0)
        except (TypeError, ValueError):
            # Non-numeric count (should not happen for COUNT(*)) -> contribute 0.
            continue

    truncated = len(values) > MAX_ENUM_CARDINALITY
    if truncated:
        values = values[:MAX_ENUM_CARDINALITY]
    return {"values": values, "distinct_count": len(values),
            "truncated": truncated, "sample_rows": sample_rows, "error": None}


# ==============================================================================
# NEPTUNE TOOLS are accessed via AgentCore Gateway
# ==============================================================================


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
        local_path = os.path.join(temp_dir, "ontology_docs", filename)

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
        }

        logger.info(f"Read lines {start_line}-{result['end_line']} from {file_path}")
        return json.dumps(result)

    except Exception as e:
        logger.error(f"Error reading document lines: {str(e)}")
        return json.dumps({"success": False, "error": str(e), "content": None})


def _version_num(v: str) -> int:
    """Parse the integer suffix from a version string like 'v1', 'v10'."""
    m = re.search(r'\d+', v or 'v0')
    return int(m.group()) if m else 0


def _filter_delta_targets(valid_tables: list, delta_targets) -> list:
    """Restrict the per-table phase loops to a delta rebuild set.

    Args:
        valid_tables: List of {database, table, catalogId} dicts from _build_tables_list.
        delta_targets: List of 'database.table' names, or None/empty for a full build.

    Returns:
        Subset whose 'database.table' is in delta_targets, or the full list when
        delta_targets is falsy. Unmatched targets are skipped (deselection between
        plan and build is not a loud failure).
    """
    if not delta_targets:
        return valid_tables
    wanted = set(delta_targets)
    return [t for t in valid_tables
            if f"{t['database']}.{t['table']}" in wanted]


def _data_marker_for_table(db, tbl, cat):
    """Return (marker, kind) via the SAME shared functions the planner uses.

    Args:
        db: Database name.
        tbl: Table name.
        cat: Catalog id.

    Returns:
        (marker_str, marker_kind): Iceberg snapshot for S3 Tables, 'unmeasurable' for
        a federated connector with no aggregation pushdown, else DDB count.
    """
    region = os.environ.get("AWS_REGION", "us-east-1")
    bucket = os.environ.get("ARTIFACTS_BUCKET", "")
    if str(cat).startswith("s3tablescatalog/"):
        return iceberg_snapshot_id(db=db, tbl=tbl, catalog_id=cat, region=region), "iceberg_snapshot"
    # A non-DynamoDB Lambda catalog (e.g. Snowflake) gets no aggregation pushdown, so
    # the COUNT(*) below would become a full remote scan that outlives its own Lambda.
    if is_count_free_catalog(catalog_id=str(cat), region=region):
        return unmeasurable_data_marker(db=db, tbl=tbl, catalog_id=str(cat)), "unmeasurable"
    return ddb_count_marker(db=db, tbl=tbl, catalog_id=cat, region=region,
                            artifacts_bucket=bucket), "ddb_count"


def _record_table_fingerprint(table, layer_id, version, db, tbl, cat):
    """Compute + persist this table's fingerprint. Called ONLY after Phase 2 success.

    Args:
        table: boto3 DynamoDB Table resource for the metadata table.
        layer_id: The layer/ontology id.
        version: The active layer version.
        db: Database name.
        tbl: Table name.
        cat: Catalog id.

    Returns:
        None.
    """
    raw = json.loads(get_single_table_schema(database_name=db, table_name=tbl, catalog_id=cat))
    # get_single_table_schema ALREADY appends partition keys into `columns`, so do
    # NOT merge_columns here — the set is already canonical (matches the planner).
    cols = raw.get("columns", [])
    marker, kind = _data_marker_for_table(db, tbl, cat)
    fp = make_fingerprint(schema_h=schema_hash(cols), data_marker=marker,
                          marker_kind=kind, version=version, catalog_id=cat)
    write_table_fingerprint(table, layer_id=layer_id, version=version,
                            db_table=f"{db}.{tbl}", fp=fp)


def _resolve_active_version(table, job_id: str) -> str:
    """
    Return the latest version sort-key for a given job/ontology ID.

    Queries all version records and returns the one with the highest numeric
    suffix (e.g. 'v10' > 'v9'). This is always correct — the lexicographic
    Limit=1 approach broke for v10+.

    Falls back to 'v1' if no record exists yet (first invocation of initial build).
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
    ontology_id: str, tables_processed: int, total_tables: int, current_table: str
) -> str:
    """
    Update ontology build progress in DynamoDB.

    This function updates the status after each table is processed,
    allowing clients to see real-time progress via polling.

    Args:
        ontology_id: Unique identifier for the ontology
        tables_processed: Number of tables processed so far
        total_tables: Total number of tables to process
        current_table: Name of the table just processed

    Returns:
        str: JSON string with update status. ``success`` is False — and no item is written —
             when no metadata record exists for ``ontology_id``, because an unconditional
             update_item would create one (see the ConditionExpression below).
    """
    try:
        session = get_boto_session()
        dynamodb = session.resource("dynamodb")
        table_name = os.environ.get(
            "ONTOLOGY_METADATA_TABLE", "semantic-layer-metadata"
        )
        table = dynamodb.Table(table_name)

        progress_percent = (
            int((tables_processed / total_tables) * 100) if total_tables > 0 else 0
        )

        # Update DynamoDB with progress.
        #
        # ConditionExpression guards against phantom rows. DynamoDB's update_item CREATES
        # the item when the key is absent, and _resolve_active_version falls back to 'v1'
        # when no record exists at all — so a progress write against a stale or already
        # deleted ontology id used to mint a nameless orphan stub with nothing but a status
        # and a percentage. attribute_exists(id) makes the write conditional on the record
        # being real. ('id' is not a DynamoDB reserved word, so it needs no alias, unlike
        # 'status' above.)
        active_version = _resolve_active_version(table, ontology_id)
        try:
            table.update_item(
                Key={
                    "id": ontology_id,
                    "version": active_version,
                },
                UpdateExpression="SET #status = :status, tablesProcessed = :processed, totalTables = :total, currentTable = :current, progressPercent = :percent, updatedAt = :updated",
                ConditionExpression="attribute_exists(id)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": "processing",
                    ":processed": tables_processed,
                    ":total": total_tables,
                    ":current": current_table,
                    ":percent": progress_percent,
                    ":updated": datetime.now(timezone.utc).isoformat(),
                },
            )
        except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            # Report the miss rather than swallowing it. The caller is an agent loop, so a
            # silent success would have it keep reporting progress into the void for the
            # rest of the build.
            logger.warning(
                f"Skipping progress update for unknown ontology id={ontology_id} "
                f"version={active_version} (no such record — not creating a phantom stub)"
            )
            return json.dumps(
                {
                    "success": False,
                    "error": f"No metadata record for id={ontology_id}",
                }
            )

        logger.info(
            f"✓ Progress updated: {tables_processed}/{total_tables} tables ({progress_percent}%) - Current: {current_table}"
        )

        return json.dumps(
            {
                "success": True,
                "tablesProcessed": tables_processed,
                "totalTables": total_tables,
                "currentTable": current_table,
                "progressPercent": progress_percent,
            }
        )

    except Exception as e:
        error_msg = f"Error updating progress: {str(e)}"
        logger.error(f"✗ {error_msg}")
        return json.dumps({"success": False, "error": error_msg})


def save_ontology_to_s3(
    ontology_content: str, ontology_id: str, filename: str = "ontology.nq"
) -> str:
    """
    Save generated ontology to S3 for backup and documentation.

    Args:
        ontology_content: OWL ontology in N-Quads format
        ontology_id: Unique identifier for the ontology (used in S3 path)
        filename: Name for the ontology file (default: "ontology.nq")

    Returns:
        str: JSON string with S3 location and status
    """
    _ext_content_types = {
        ".nq": "application/n-quads",
        ".ttl": "text/turtle",
        ".owl": "application/rdf+xml",
    }
    content_type = _ext_content_types.get(
        os.path.splitext(filename)[1], "application/octet-stream"
    )

    session = get_boto_session()
    s3 = session.client("s3")
    bucket = os.environ.get("ARTIFACTS_BUCKET")

    if not bucket:
        error_msg = "ARTIFACTS_BUCKET environment variable not set"
        logger.error(error_msg)
        return json.dumps({"success": False, "message": error_msg})

    try:
        # Save to S3 using ontology_id in path to match expected location
        key = f"ontologies/{ontology_id}/{filename}"
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=ontology_content.encode("utf-8"),
            ContentType=content_type,
        )

        s3_location = f"s3://{bucket}/{key}"

        logger.info(f"✓ Successfully saved ontology to S3: {s3_location}")

        return json.dumps(
            {
                "success": True,
                "s3_location": s3_location,
                "bucket": bucket,
                "key": key,
                "message": f"Ontology saved to S3 at {s3_location}",
            }
        )
    except Exception as e:
        error_msg = f"Error saving ontology to S3: {str(e)}"
        logger.error(f"✗ {error_msg}")
        return json.dumps({"success": False, "message": error_msg})


@tool
def save_intermediate_ontology(
    ontology_id: str,
    table_name: str,
    nquad_content: str,
    step: int,
    total_steps: int,
    class_count: int = 0,
    property_count: int = 0,
    fk_hints: str = "",
) -> str:
    """
    Save intermediate ontology N-Quads for a single table to local filesystem and S3.

    Call this after generating N-Quads for each table in Phase 1.
    Saves a markdown file containing the N-Quads and summary stats.

    Args:
        ontology_id: Ontology identifier
        table_name: Name of the table just processed
        nquad_content: Generated N-Quads string for this table
        step: Current table number (1-based)
        total_steps: Total number of tables
        class_count: Number of OWL classes generated
        property_count: Number of OWL properties generated
        fk_hints: Comma-separated list of FK hints noted for Phase 2

    Returns:
        JSON string with local_path and s3_location
    """
    import tempfile

    try:
        session = get_boto_session()
        s3 = session.client("s3")
        bucket = os.environ.get("ARTIFACTS_BUCKET")

        # Build local path
        temp_dir = os.path.join(
            tempfile.gettempdir(), "ontologies", ontology_id, "phase1"
        )
        os.makedirs(temp_dir, exist_ok=True)
        filename = f"table-{step:02d}-{table_name}.md"
        local_path = os.path.join(temp_dir, filename)

        percent = int((step / total_steps) * 100) if total_steps > 0 else 0
        timestamp = datetime.now(timezone.utc).isoformat()

        # If nquad_content is empty, merge from the incremental accumulation file
        # written by append_nquads() during batched processing.
        accum_path = os.path.join(temp_dir, f"{table_name}.nq")
        if not nquad_content and os.path.exists(accum_path):
            with open(accum_path, "r", encoding="utf-8") as af:
                nquad_content = af.read().strip()
            logger.info(
                f"Merged {len(nquad_content)} chars of N-Quads from accumulation file for {table_name}"
            )
            os.remove(accum_path)

        content = f"""# Ontology Generation - Table {step} of {total_steps}

**Ontology ID:** {ontology_id}
**Table:** {table_name}
**Timestamp:** {timestamp}
**Progress:** {step}/{total_steps} ({percent}%)

## Summary Statistics

- **Classes Generated:** {class_count}
- **Properties Generated:** {property_count}
- **FK Hints for Phase 2:** {fk_hints or "none"}

## Generated N-Quads

```nquads
{nquad_content}
```

## Status

- ✓ Schema retrieved from Glue
- ✓ N-Quads generated
- ✓ Saved to S3
"""

        with open(local_path, "w", encoding="utf-8") as f:
            f.write(content)

        result = {
            "success": True,
            "local_path": local_path,
            "table_name": table_name,
            "step": step,
            "total_steps": total_steps,
        }

        if bucket:
            s3_key = f"ontologies/{ontology_id}/phase1/{filename}"
            s3.put_object(
                Bucket=bucket,
                Key=s3_key,
                Body=content.encode("utf-8"),
                ContentType="text/markdown",
            )
            result["s3_location"] = f"s3://{bucket}/{s3_key}"
            logger.info(
                f"Saved intermediate ontology for {table_name} to {result['s3_location']}"
            )
        else:
            logger.warning(
                "ARTIFACTS_BUCKET not set — skipping S3 upload for intermediate ontology"
            )

        return json.dumps(result)

    except Exception as e:
        logger.error(f"Error saving intermediate ontology for {table_name}: {e}")
        return json.dumps({"success": False, "error": str(e)})


@tool
def append_nquads(ontology_id: str, table_name: str, nquad_batch: str) -> str:
    """
    Append a batch of N-Quads to the incremental accumulation file for a table.

    Use this during Phase 1 to write N-Quads in small batches (10 columns per call)
    instead of generating the entire table's N-Quads in a single response.
    This avoids MaxTokensReachedException for wide tables (100+ columns).

    BATCH SIZE LIMIT: each call must contain at most 10 columns (≤ 70 N-Quad lines).
    The tool rejects oversized batches so you can split and retry.

    Workflow:
      1. Call append_nquads() once for the owl:Class triples.
      2. Call append_nquads() repeatedly for each batch of 10 owl:DatatypeProperty columns.
      3. Call save_intermediate_ontology(..., nquad_content="") to finalise —
         the tool will merge all batches automatically.

    Args:
        ontology_id: Ontology identifier (same as used in save_intermediate_ontology)
        table_name:  Table name being processed
        nquad_batch: Raw N-Quad lines for this batch (newline-separated).
                     Must contain ≤ 70 non-blank lines (≈ 10 properties × 6 triples + a few header lines).

    Returns:
        JSON with success status and running byte count written so far
    """
    import tempfile

    # Guard: reject batches that exceed the per-call line limit.
    # This is a backstop; the primary defence is max_tokens=8000 on the model.
    MAX_LINES = 70  # ~10 DatatypeProperty columns × 6 triples + overhead
    non_blank_lines = [ln for ln in nquad_batch.splitlines() if ln.strip()]
    if len(non_blank_lines) > MAX_LINES:
        logger.warning(
            f"[append_nquads] {table_name}: batch too large ({len(non_blank_lines)} lines > {MAX_LINES}). "
            "Rejecting — split into smaller batches of 10 columns."
        )
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"Batch too large: {len(non_blank_lines)} N-Quad lines (max {MAX_LINES}). "
                    "Split into batches of 10 columns and call append_nquads once per batch."
                ),
                "table_name": table_name,
                "lines_received": len(non_blank_lines),
                "max_lines": MAX_LINES,
            }
        )

    try:
        temp_dir = os.path.join(
            tempfile.gettempdir(), "ontologies", ontology_id, "phase1"
        )
        os.makedirs(temp_dir, exist_ok=True)
        accum_path = os.path.join(temp_dir, f"{table_name}.nq")

        with open(accum_path, "a", encoding="utf-8") as f:
            f.write(nquad_batch.strip() + "\n")

        size = os.path.getsize(accum_path)
        logger.info(
            f"[append_nquads] {table_name}: appended {len(nquad_batch)} chars, file now {size} bytes"
        )
        return json.dumps(
            {"success": True, "table_name": table_name, "bytes_written": size}
        )

    except Exception as e:
        logger.error(f"[append_nquads] Error for {table_name}: {e}")
        return json.dumps({"success": False, "error": str(e)})


@tool
def load_phase1_fragments(ontology_id: str) -> str:
    """
    Load Phase 1 fragment summaries for a given ontology.

    Tries local filesystem first, then falls back to S3 (downloading files locally).
    Returns only table_name, fk_hints, and local_path — N-Quads content is
    intentionally excluded to keep the tool result small. Fetch N-Quads on demand
    via read_local_nquads_file().

    Args:
        ontology_id: Ontology identifier

    Returns:
        JSON with list of table summaries (table_name, fk_hints, local_path)
        and total_tables count.
    """
    import tempfile
    import re

    entries = []

    # Try local first
    local_dir = os.path.join(tempfile.gettempdir(), "ontologies", ontology_id, "phase1")

    if os.path.isdir(local_dir):
        files = sorted(f for f in os.listdir(local_dir) if f.endswith(".md"))
        for filename in files:
            path = os.path.join(local_dir, filename)
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            match = re.search(r"\*\*Table:\*\* (.+)", raw)
            table_name = match.group(1).strip() if match else filename
            fk_match = re.search(r"\*\*FK Hints for Phase 2:\*\* (.+)", raw)
            fk_hints_raw = fk_match.group(1).strip() if fk_match else ""
            fk_hints = "" if fk_hints_raw == "none" else fk_hints_raw
            # nquad_content intentionally excluded — fetched on-demand via read_local_nquads_file
            entries.append(
                {"table_name": table_name, "fk_hints": fk_hints, "local_path": path}
            )

        if entries:
            logger.info(
                f"Loaded {len(entries)} Phase 1 fragment summaries from local filesystem"
            )
            return json.dumps(
                {"success": True, "total_tables": len(entries), "tables": entries}
            )

    # Fall back to S3
    try:
        session = get_boto_session()
        s3 = session.client("s3")
        bucket = os.environ.get("ARTIFACTS_BUCKET")
        if not bucket:
            return json.dumps(
                {
                    "success": False,
                    "error": "ARTIFACTS_BUCKET not set and no local files found",
                }
            )

        prefix = f"ontologies/{ontology_id}/phase1/"
        response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        objects = sorted(
            o["Key"] for o in response.get("Contents", []) if o["Key"].endswith(".md")
        )

        os.makedirs(local_dir, exist_ok=True)
        for key in objects:
            obj = s3.get_object(Bucket=bucket, Key=key)
            raw = obj["Body"].read().decode("utf-8")
            filename = key.split("/")[-1]
            local_path = os.path.join(local_dir, filename)
            with open(local_path, "w", encoding="utf-8") as f:
                f.write(raw)
            match = re.search(r"\*\*Table:\*\* (.+)", raw)
            table_name = match.group(1).strip() if match else filename
            fk_match = re.search(r"\*\*FK Hints for Phase 2:\*\* (.+)", raw)
            fk_hints_raw = fk_match.group(1).strip() if fk_match else ""
            fk_hints = "" if fk_hints_raw == "none" else fk_hints_raw
            entries.append(
                {
                    "table_name": table_name,
                    "fk_hints": fk_hints,
                    "local_path": local_path,
                }
            )

        logger.info(f"Loaded {len(entries)} Phase 1 fragments from S3")
        return json.dumps(
            {"success": True, "total_tables": len(entries), "tables": entries}
        )

    except Exception as e:
        logger.error(f"Error loading Phase 1 fragments: {e}")
        return json.dumps({"success": False, "error": str(e)})


@tool
def read_local_nquads_file(ontology_id: str, table_name: str) -> str:
    """
    Read the N-Quads content for a specific table from its Phase 1 markdown file.

    Use during Phase 2 refinement to review individual table N-Quads
    without loading all tables into context at once.

    Args:
        ontology_id: Ontology identifier
        table_name: Table name (as written in the markdown **Table:** field)

    Returns:
        JSON with nquad_content and local_path
    """
    import tempfile
    import re

    local_dir = os.path.join(tempfile.gettempdir(), "ontologies", ontology_id, "phase1")

    if not os.path.isdir(local_dir):
        return json.dumps(
            {
                "success": False,
                "error": f"Phase 1 directory not found: {local_dir}. Run load_phase1_fragments first.",
            }
        )

    # Find matching file by table_name substring
    for filename in sorted(os.listdir(local_dir)):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(local_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        match = re.search(r"\*\*Table:\*\* (.+)", raw)
        found_name = match.group(1).strip() if match else ""
        if found_name == table_name:
            nq_match = re.search(r"```nquads\n(.*?)```", raw, re.DOTALL)
            nquad_content = nq_match.group(1).strip() if nq_match else ""
            return json.dumps(
                {
                    "success": True,
                    "table_name": found_name,
                    "nquad_content": nquad_content,
                    "local_path": path,
                }
            )

    return json.dumps(
        {"success": False, "error": f"No Phase 1 file found for table: {table_name}"}
    )


def _hydrate_missing_fragments_from_s3(ontology_id: str) -> None:
    """Download any Phase-1 fragments that are absent from the local temp dir.

    In a DELTA build only the rebuilt subset of tables has a fresh local fragment;
    unchanged tables' fragments live only in S3 (from the prior build). Because
    load_phase1_fragments is LOCAL-FIRST and returns early once ANY local file
    exists, it would otherwise report only the rebuilt subset — truncating the
    assembled ontology. This pulls the missing (unchanged) fragments down first so
    the local dir holds the COMPLETE set.

    Skips are keyed on the fragment's ``**Table:**`` NAME, not the filename. The
    filename encodes the table's position in the build list (``table-{step:02d}-…``),
    and a delta build filters that list — so a rebuilt table's fresh local fragment
    (e.g. ``table-01-orders.md``) can have a DIFFERENT step number than its prior
    S3 fragment (``table-05-orders.md``). Keying on filename would download the
    stale S3 copy alongside the fresh local one, duplicating that table in assembly.
    Keying on table NAME skips any S3 fragment already represented locally, so the
    fresh content wins and no stale duplicate is pulled in. Fail-soft: an S3 error
    just leaves the local set as-is.

    Args:
        ontology_id: The ontology/layer id (S3 prefix component).

    Returns:
        None.
    """
    import tempfile
    import re

    bucket = os.environ.get("ARTIFACTS_BUCKET")
    if not bucket:
        return
    local_dir = os.path.join(tempfile.gettempdir(), "ontologies", ontology_id, "phase1")
    os.makedirs(local_dir, exist_ok=True)

    def _table_name_of(md_text: str) -> str:
        m = re.search(r"\*\*Table:\*\* (.+)", md_text)
        return m.group(1).strip() if m else ""

    # Table names already present locally (rebuilt this run) — skip these in S3.
    local_tables = set()
    for fn in os.listdir(local_dir):
        if fn.endswith(".md"):
            with open(os.path.join(local_dir, fn), "r", encoding="utf-8", errors="ignore") as f:
                local_tables.add(_table_name_of(f.read()))
    local_tables.discard("")

    try:
        s3 = get_boto_session().client("s3")
        prefix = f"ontologies/{ontology_id}/phase1/"
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                filename = key.split("/")[-1]
                if not filename.endswith(".md"):
                    continue
                text = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode(
                    "utf-8", errors="ignore"
                )
                # Skip if this table already has a (fresh) local fragment, even under
                # a different step-numbered filename.
                if _table_name_of(text) in local_tables:
                    continue
                with open(os.path.join(local_dir, filename), "w", encoding="utf-8") as f:
                    f.write(text)
    except Exception as e:  # noqa: BLE001 — never abort assembly on a hydration miss
        # ERROR, not warning: a hydration failure on a DELTA build means unchanged
        # tables' fragments may be absent, so the consolidated ontology.nq can be
        # SILENTLY TRUNCATED (assembly still succeeds on the rebuilt subset). We keep
        # fail-soft so a transient S3 blip doesn't abort the whole build, but log
        # loudly so the truncation is visible in CloudWatch rather than hidden.
        logger.error(
            "[Assembly] fragment hydration from S3 FAILED for %s: %s — consolidated "
            "ontology may be truncated to the rebuilt subset (unchanged tables' "
            "fragments not hydrated)", ontology_id, e)


def _assemble_consolidated(ontology_id: str) -> str:
    """Concatenate ALL per-table N-Quad fragments into one consolidated string.

    First hydrates any fragments missing from the local temp dir from S3
    (_hydrate_missing_fragments_from_s3), so a DELTA build — where only a subset of
    tables was rebuilt this run — still assembles the COMPLETE ontology (unchanged
    tables' last-built fragments are included). load_phase1_fragments is local-first,
    so without the hydration step it would return only the rebuilt subset.

    Args:
        ontology_id: The ontology/layer id.

    Returns:
        The consolidated N-Quads string ("\n"-joined), or "" if no fragments had content.
    """
    _hydrate_missing_fragments_from_s3(ontology_id)
    fragments = json.loads(load_phase1_fragments(ontology_id))
    parts: List[str] = []
    for entry in fragments.get("tables", []):
        nq = json.loads(read_local_nquads_file(ontology_id, entry["table_name"]))
        if nq.get("success") and nq.get("nquad_content"):
            parts.append(nq["nquad_content"])
        else:
            logger.warning(
                "[Assembly] Empty or missing N-Quads for table '%s': success=%s, error=%s",
                entry["table_name"],
                nq.get("success"),
                nq.get("error", ""),
            )
    return "\n".join(parts)


def _stamp_verified_primary_keys(
    *,
    ontology_id: str,
    nquads: str,
    tables: List[Dict[str, Any]],
) -> Tuple[str, str]:
    """Append verified ``vkg:hasPrimaryKey`` triples to the consolidated N-Quads.

    Runs uniqueness probes against Athena (``COUNT(DISTINCT col) = COUNT(*)`` with
    no NULLs) so each class's row identity is a MEASURED fact rather than a name
    guess. The translator prefers this declared key over every name heuristic and
    only emits a ``uniqueConstraint`` when it is present, so stamping it is what
    stops Ontop self-joining a table on a value column.

    Args:
        ontology_id: The ontology/layer id (logging context only).
        nquads: The consolidated ontology N-Quads text.
        tables: The build's table dicts, used to resolve each database's catalog
            so a federated (S3 Tables) database is probed through the right one.

    Returns:
        ``(stamped_nquads, delta_nquads)``. The first is the input with verified
        key triples appended (for S3); the second is ONLY the newly added lines,
        which the caller must also push to Neptune — the query path reads the
        ontology from NEPTUNE, not from the S3 file, so a key that lands only in
        S3 never reaches the translator and changes nothing. ``delta_nquads`` is
        empty when nothing was stamped. On any failure the input is returned
        unchanged with an empty delta: an unstamped class keeps the pre-existing
        heuristic behaviour rather than blocking the build.
    """
    from .key_stamp import stamp_primary_keys

    # database -> catalog, from the build's own table list. Probing a federated
    # database through the default catalog fails to resolve the table at all.
    catalogs: Dict[str, str] = {}
    for entry in tables:
        database = entry.get("database")
        catalog = entry.get("catalogId")
        if database and catalog and database not in catalogs:
            catalogs[database] = catalog

    def _probe_factory(database: str, catalog: Optional[str]) -> Any:
        """Build a RunProbe over ``_run_athena_query`` for one database."""
        effective_catalog = catalog or "AWSDataCatalog"

        # A uniqueness probe is `COUNT(*)` plus `COUNT(DISTINCT col)`/`COUNT(col)` for
        # EVERY column, in one query per table. That design is what makes probing every
        # column affordable on S3 Tables — one scan answers the whole table. Over a
        # federated (Lambda-backed) catalog it is the opposite: Athena does not push
        # aggregation into the connector, so the connector runs a bare
        # `SELECT "<col>" FROM …` and streams the entire table through a Lambda capped at
        # 900s. Measured against Snowflake, these probes ran 600-900s each and were killed
        # at the ceiling; because the build probes tables concurrently, their abandoned
        # invocations then congested the connector for ~15 minutes and the build died in
        # refinement. Worse, the widest column here is typically a JSON document blob
        # (FHIR `RESOURCE`), which is both the most expensive to stream AND unusable as
        # row identity even though it would pass the uniqueness test.
        #
        # So decline the probe on a federated catalog. Declining is a supported outcome,
        # not a silent one: key_probe treats no rows as "key unknown", stamps nothing, and
        # the translator falls back to its name heuristic while logging that row identity
        # is a guess. That is strictly better than a stamped key we cannot afford to
        # verify — an unverified `primaryKey` would additionally license Ontop to drop
        # de-duplication on a column that may repeat.
        #
        # This is deliberately conservative: cost scales with rows x column width, so on a
        # SMALL federated table the probe is affordable (measured 7-14s over a 10,969-row
        # Snowflake table). Gating on catalog type rather than measured size costs us a
        # verified key on those tables, but the only way to learn a table's size here is a
        # `COUNT(*)` — itself an unpushed aggregation, i.e. the very query we cannot
        # afford on the large tables. Refine this by reading row counts from catalog
        # statistics, not by probing for them.
        if _is_federated_connector_catalog(effective_catalog):
            logger.warning(
                "[KeyStamp] SKIPPING uniqueness probes for database '%s': catalog '%s' is "
                "a federated connector, where a COUNT(DISTINCT) probe streams the whole "
                "table through the connector Lambda and exceeds its 900s ceiling. No "
                "primary key will be declared for these classes, so the translator will "
                "fall back to its name heuristic for row identity — verify the chosen "
                "subject column if query results look self-joined.",
                database, effective_catalog,
            )

            def _decline(query: str) -> List[Dict[str, Any]]:
                """Return no rows, so the caller records 'key unknown' for this table."""
                return []

            return _decline

        def _run(query: str) -> List[Dict[str, Any]]:
            """Execute one probe query, returning rows (empty on failure).

            Returning empty on failure is what makes the probe non-committal: the
            caller treats "no answer" as "key unknown" and leaves the class
            unstamped, never as evidence of uniqueness.
            """
            result = _run_athena_query(query, database, effective_catalog)
            if not result.get("success"):
                logger.warning(
                    "[KeyStamp] probe query failed on %s: %s",
                    database, result.get("error"),
                )
                return []
            return result.get("rows", [])

        return _run

    try:
        stamped, keys_by_class = stamp_primary_keys(
            nquads=nquads,
            probe_factory=_probe_factory,
            catalog_for=catalogs.get,
        )
    except Exception as exc:  # noqa: BLE001 — never block assembly on key probing
        logger.warning(
            "[KeyStamp] key stamping failed for %s (%s) — saving the ontology "
            "without declared keys; the translator will fall back to its name "
            "heuristic for row identity",
            ontology_id, exc,
        )
        return nquads, ""

    logger.info(
        "[KeyStamp] stamped verified primary keys on %d class(es) for %s",
        len(keys_by_class), ontology_id,
    )
    # The delta is exactly the suffix stamp_primary_keys appended. Recomputing it
    # from keys_by_class would risk drifting from the emitted literal shape, so it
    # is sliced off the returned text instead.
    delta = stamped[len(nquads):] if stamped != nquads else ""
    return stamped, delta


def _clear_prior_neptune_graphs(*, ontology_id: str) -> str:
    """Drop every named graph a previous build of this layer left in Neptune.

    A full rebuild must start from an empty graph. It does not, by default: the
    graph URI is derived from the layer's *name* (see ``build_namespace``), and
    callers timestamp that name per build, so each rebuild writes into a NEW graph
    and ``tool_persist_to_neptune`` (a SPARQL ``INSERT DATA``) never touches the
    old one. On 2026-08-09 every rebuilt BIRD layer had three graphs in Neptune and
    resolution was serving the oldest, so the 08-06 rebuild — and any fix it
    carried — was invisible at query time.

    Args:
        ontology_id: The layer id whose prior graphs should be dropped.

    Returns:
        A human-readable detail string for the log (never raises: a rebuild is
        still worth attempting if the cleanup could not run, and the newest-graph
        ordering in ``_resolve_graph_uris`` keeps resolution correct meanwhile).
    """
    gateway_url = os.environ.get("NEPTUNE_GATEWAY_URL", "")
    if not gateway_url:
        return "NEPTUNE_GATEWAY_URL not configured — nothing cleared"

    region = os.environ.get("AWS_REGION", "us-east-1")
    try:
        with MCPClient(
            lambda: aws_iam_streamablehttp_client(
                endpoint=gateway_url,
                aws_region=region,
                aws_service="bedrock-agentcore",
            )
        ) as client:
            raw = client.call_tool_sync(
                str(uuid.uuid4()),
                "delete-graph___delete_graph",
                {"ontology_id": ontology_id},
            )
        text = raw["content"][0].get("text", "") if raw.get("content") else ""
        return text or "delete_graph returned no body"
    except Exception as exc:  # noqa: BLE001 — cleanup failure must not block a rebuild
        return f"could not clear prior graphs ({exc})"


def _probe_layer_is_queryable(*, ontology_id: str) -> Tuple[bool, str]:
    """Check a finished layer can actually answer a SPARQL query, via the gateway.

    A layer can reach ``refinement_complete`` with a perfectly well-formed
    ontology that Ontop cannot build a reformulator for at all. That happened to
    ``bird_card_games``: every SPARQL query failed with "Failed to build Ontop
    reformulator" (an unquoted reserved-word table sent Ontop down its live-DB
    "black box view" path), yet the layer was stamped ``completed`` and served
    52 benchmark rows a hard zero with no error visible anywhere upstream.

    The probe deliberately goes through the SAME gateway tools a real query uses
    — ``get_ontology_from_neptune`` then ``translate_sparql_to_sql`` — because
    that is the only path that exercises Neptune's stored ontology AND the Java
    reformulator together. Translating the local N-Quads instead would pass while
    the deployed path fails, which is exactly the gap that let card_games ship.
    Only the TRANSLATE step is probed, not Athena execution: a translation
    failure is a broken layer, whereas an Athena failure may be a transient
    permission or capacity problem that should not fail a build.

    Args:
        ontology_id: The layer id to probe.

    Returns:
        ``(is_queryable, detail)``. ``detail`` carries the reason on failure and
        the probed class IRI on success. When the probe cannot RUN at all
        (no gateway URL configured, no class found), it returns ``True`` with a
        detail explaining why — an inconclusive probe must not fail a build it
        has no evidence against.
    """
    gateway_url = os.environ.get("NEPTUNE_GATEWAY_URL", "")
    if not gateway_url:
        return True, "NEPTUNE_GATEWAY_URL not configured — probe skipped"

    import uuid as _uuid

    region = os.environ.get("AWS_REGION", "us-east-1")
    try:
        mcp_client = MCPClient(
            lambda: aws_iam_streamablehttp_client(
                endpoint=gateway_url,
                aws_region=region,
                aws_service="bedrock-agentcore",
            )
        )
        with mcp_client:

            def _call(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
                """Call one gateway tool and unwrap its JSON body."""
                raw = mcp_client.call_tool_sync(
                    str(_uuid.uuid4()), tool_name, arguments
                )
                text = ""
                if raw.get("content"):
                    text = raw["content"][0].get("text", "")
                envelope = json.loads(text) if text else {}
                # The Gateway wraps a Lambda response as {statusCode, body}; the
                # Java translate Lambda returns its map directly.
                body = envelope.get("body", "")
                return json.loads(body) if body else envelope

            ontology_json = _call(
                "get-ontology-from-neptune___get_ontology_from_neptune",
                {"ontology_id": ontology_id},
            )
            if ontology_json.get("error"):
                return False, f"get_ontology_from_neptune failed: {ontology_json['error']}"
            classes = ontology_json.get("classes") or {}
            if not classes:
                # Nothing to query. Assembly already failed the build when it
                # produced no N-Quads, so treat this as inconclusive rather than
                # duplicating that failure with a worse message.
                return True, "ontology in Neptune declares no classes — probe skipped"

            # A mapped class, so the translation actually reaches a relation.
            # Sorted for determinism: the same layer must probe the same class on
            # every rebuild, or a flaky pass/fail looks like a code change.
            mappings = ontology_json.get("mappings") or {}
            mapped = sorted(
                iri for iri in classes
                if isinstance(mappings.get(iri), dict) and mappings[iri].get("table")
            )
            if not mapped:
                return True, "no class carries a table mapping — probe skipped"
            class_iri = mapped[0]

            payload = _call(
                "translate-sparql-to-sql___translate_sparql_to_sql",
                {
                    "sparql": f"SELECT ?s WHERE {{ ?s a <{class_iri}> }} LIMIT 1",
                    "ontologyJson": ontology_json,
                    "ontologyId": ontology_id,
                },
            )
    except Exception as exc:  # noqa: BLE001 — an unreachable gateway proves nothing
        return True, f"probe could not run ({exc}) — treated as inconclusive"

    if payload.get("error"):
        return False, f"translate_sparql_to_sql failed for <{class_iri}>: {payload['error']}"
    if not payload.get("sql"):
        return False, (
            f"translate_sparql_to_sql returned no SQL for <{class_iri}> — the query "
            f"reformulated to nothing, so every query against this layer returns "
            f"zero rows"
        )
    return True, f"probed <{class_iri}>"


@tool
def update_nquads_in_file(
    ontology_id: str, table_name: str, updated_nquads: str, reason: str
) -> str:
    """
    Replace the N-Quads block in a Phase 1 markdown file with refined content.

    Use during Phase 2 to update individual table ontologies based on
    cross-table analysis (add FK relationships, fix annotations, etc.).

    Args:
        ontology_id: Ontology identifier
        table_name: Table name matching the file to update
        updated_nquads: Complete replacement N-Quads string
        reason: Explanation of what was changed and why (audit trail)

    Returns:
        JSON with success status and updated file path
    """
    import tempfile
    import re

    local_dir = os.path.join(tempfile.gettempdir(), "ontologies", ontology_id, "phase1")

    if not os.path.isdir(local_dir):
        return json.dumps(
            {"success": False, "error": f"Phase 1 directory not found: {local_dir}"}
        )

    for filename in sorted(os.listdir(local_dir)):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(local_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        match = re.search(r"\*\*Table:\*\* (.+)", raw)
        found_name = match.group(1).strip() if match else ""
        if found_name != table_name:
            continue

        # Replace N-Quads block
        timestamp = datetime.now(timezone.utc).isoformat()
        updated_raw = re.sub(
            r"```nquads\n.*?```",
            f"```nquads\n{updated_nquads}\n```",
            raw,
            flags=re.DOTALL,
        )
        # Append refinement note
        updated_raw += (
            f"\n## Phase 2 Refinement ({timestamp})\n\n**Reason:** {reason}\n"
        )

        with open(path, "w", encoding="utf-8") as f:
            f.write(updated_raw)

        # Also update S3
        try:
            session = get_boto_session()
            s3 = session.client("s3")
            bucket = os.environ.get("ARTIFACTS_BUCKET")
            if bucket:
                s3_key = f"ontologies/{ontology_id}/phase1/{filename}"
                s3.put_object(
                    Bucket=bucket,
                    Key=s3_key,
                    Body=updated_raw.encode("utf-8"),
                    ContentType="text/markdown",
                )
        except Exception as s3_err:
            logger.warning(f"S3 update failed for {filename}: {s3_err}")

        logger.info(f"Updated N-Quads for {found_name}: {reason}")
        return json.dumps(
            {
                "success": True,
                "table_name": found_name,
                "local_path": path,
                "reason": reason,
            }
        )

    return json.dumps(
        {"success": False, "error": f"No Phase 1 file found for table: {table_name}"}
    )


@tool
def append_fk_triples(ontology_id: str, table_name: str, fk_nquads: str) -> str:
    """
    Append FK ObjectProperty N-Quads to a Phase 1 file without replacing its content.

    Use in Phase 2 to add owl:ObjectProperty triples for foreign-key relationships.
    Pass ONLY the new FK lines — the existing Phase 1 content (class + datatype
    properties) is preserved. This avoids the max_tokens overflow that occurs when
    the agent tries to output the full combined content.

    Args:
        ontology_id: Ontology identifier
        table_name: Exact table name (must match the **Table:** field in the file)
        fk_nquads: The FK ObjectProperty N-Quad lines to append (typically 6 lines
                   per FK relationship)

    Returns:
        JSON with success, local_path, and fk_triples_added count
    """
    import tempfile
    import re

    local_dir = os.path.join(tempfile.gettempdir(), "ontologies", ontology_id, "phase1")
    if not os.path.isdir(local_dir):
        return json.dumps(
            {"success": False, "error": f"Phase 1 directory not found: {local_dir}"}
        )

    for filename in sorted(os.listdir(local_dir)):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(local_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        match = re.search(r"\*\*Table:\*\* (.+)", raw)
        found_name = match.group(1).strip() if match else ""
        if found_name != table_name:
            continue

        # Append FK triples inside the existing nquads block
        new_lines = fk_nquads.strip()
        updated_raw = re.sub(
            r"(```nquads\n)(.*?)(```)",
            lambda m: m.group(1)
            + m.group(2).rstrip("\n")
            + "\n"
            + new_lines
            + "\n"
            + m.group(3),
            raw,
            flags=re.DOTALL,
        )
        timestamp = datetime.now(timezone.utc).isoformat()
        fk_count = len([line for line in new_lines.splitlines() if line.strip()])
        updated_raw += f"\n## Phase 2 FK Additions ({timestamp})\n\n**FK triples appended:** {fk_count}\n"

        with open(path, "w", encoding="utf-8") as f:
            f.write(updated_raw)

        try:
            session = get_boto_session()
            s3 = session.client("s3")
            bucket = os.environ.get("ARTIFACTS_BUCKET")
            if bucket:
                s3_key = f"ontologies/{ontology_id}/phase1/{filename}"
                s3.put_object(
                    Bucket=bucket,
                    Key=s3_key,
                    Body=updated_raw.encode("utf-8"),
                    ContentType="text/markdown",
                )
        except Exception as s3_err:
            logger.warning(f"S3 sync failed for {filename}: {s3_err}")

        logger.info(f"Appended {fk_count} FK triples to {filename}")
        return json.dumps(
            {
                "success": True,
                "table_name": table_name,
                "local_path": path,
                "fk_triples_added": fk_count,
            }
        )

    return json.dumps(
        {"success": False, "error": f"No Phase 1 file found for table: {table_name}"}
    )


def _read_phase1_nquads(ontology_id: str, table_name: str) -> str:
    """
    Return the current N-Quads block text for a table's Phase 1 file.

    Locates the per-table Phase 1 markdown file the same way
    ``persist_file_to_neptune`` does — globbing the temp directory
    ``ontologies/{ontology_id}/phase1`` and matching the ``**Table:** <name>``
    line — then extracts the content of the ```nquads`` fenced block.

    Args:
        ontology_id: Ontology identifier (used in the temp S3-mirror path).
        table_name: Exact table name; must match the ``**Table:**`` field.

    Returns:
        The N-Quads block text (stripped), or "" if no matching file / block
        is found. Never raises for a missing directory or file.
    """
    import tempfile
    import re

    local_dir = os.path.join(
        tempfile.gettempdir(), "ontologies", ontology_id, "phase1"
    )
    if not os.path.isdir(local_dir):
        return ""

    for filename in sorted(os.listdir(local_dir)):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(local_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        match = re.search(r"\*\*Table:\*\* (.+)", raw)
        found_name = match.group(1).strip() if match else ""
        if found_name != table_name:
            continue
        nq_match = re.search(r"```nquads\n(.*?)```", raw, re.DOTALL)
        return nq_match.group(1).strip() if nq_match else ""

    return ""


def append_enum_shape_nquads(ontology_id: str, table_name: str, nquads: str) -> str:
    """
    Append SHACL enum-shape N-Quads into a table's Phase 1 ```nquads`` block.

    Thin wrapper that delegates to ``append_fk_triples`` — which already performs
    the exact file-locate + fenced-block append + S3 sync we need. Defined as its
    own function so the ``enum_shape_builder`` call site reads clearly and so tests
    can patch it independently of the FK path.

    Args:
        ontology_id: Ontology identifier.
        table_name: Exact table name; must match the ``**Table:**`` field.
        nquads: The SHACL shape N-Quad lines to append.

    Returns:
        The JSON string returned by ``append_fk_triples``.
    """
    return append_fk_triples(ontology_id, table_name, nquads)


@tool
def enum_shape_builder(
    ontology_id: str, table_name: str, database: str, catalog: str
) -> str:
    """
    Probe a table's string columns, gate true enums, and append sh:in SHACL shapes.

    For each datatype property in the table's Phase 1 N-Quads that maps to a column
    (via ``vkg:mapsToColumn``), this tool probes the column's distinct values with a
    cardinality cap, applies the enum gate (``is_enum``), and — for columns that pass
    — builds a SHACL ``sh:in`` shape constraining the property's allowed values. All
    accepted shapes are appended to the table's Phase 1 file in a single write.

    ``database`` and ``catalog`` are required because the distinct-value probe runs an
    Athena query, which needs the table's database and catalog (the N-Quads only carry
    the column/table name). The Phase 1 build loop has these in scope per table.

    This tool NEVER raises: per-column failures are logged as WARNINGs and skipped;
    any outer failure is returned as ``{"success": False, "error": ...}``.

    Args:
        ontology_id: Ontology identifier (locates the Phase 1 file).
        table_name: Exact table name; must match the ``**Table:**`` field.
        database: Glue/Athena database name for the distinct-value probe.
        catalog: Athena catalog id for the distinct-value probe.

    Returns:
        JSON string with: ``success`` (bool), ``columns_probed`` (int — number of enum
        candidates considered), ``enums_found`` (int — candidates that passed the gate
        and produced a non-empty shape), and ``shapes_written`` (int — shapes appended).
    """
    try:
        nquads = _read_phase1_nquads(ontology_id, table_name)
        if not nquads:
            return json.dumps(
                {
                    "success": True,
                    "columns_probed": 0,
                    "enums_found": 0,
                    "shapes_written": 0,
                }
            )

        candidates = extract_enum_candidates(nquads)
        shapes: List[str] = []

        for candidate in candidates:
            # Per-column isolation: a single probe/build failure must not abort
            # the whole table's enum discovery.
            try:
                # Probe the PHYSICAL table (`table_name`), not `candidate["table"]`.
                # All candidates were parsed from this table's Phase-1 N-Quads, so
                # they belong to `table_name`. `candidate["table"]` is the prefix the
                # model wrote in the `vkg:mapsToColumn "table.column"` literal, which
                # routinely differs from the real Glue/Athena table name (e.g. friendly
                # "party" vs physical "semantic_layer_parties"). Probing the model's
                # token made Athena fail and the fail-soft path silently dropped EVERY
                # enum shape for the table. The column half is trusted (real schema).
                probe = select_distinct_values(
                    database, table_name, candidate["column"], catalog
                )
                # Two independent gates:
                #  - passed_value_gate (is_enum): the VALUE SET looks enum-like
                #    (small, short, untruncated).
                #  - passed_col_gate (is_enum_column): the COLUMN is categorical
                #    by name + distinct/total ratio (rejects name/id/free-text
                #    columns that happen to have few sampled values).
                # Both must pass before we emit a sh:in shape.
                passed_value_gate = is_enum(
                    probe["values"], truncated=probe["truncated"]
                )
                passed_col_gate = is_enum_column(
                    candidate["column"],
                    probe["values"],
                    distinct_count=probe["distinct_count"],
                    sample_rows=probe.get("sample_rows", 0),
                )
                if passed_value_gate and passed_col_gate:
                    shape = build_enum_shape_nquads(
                        class_iri=candidate["class_iri"],
                        prop_iri=candidate["prop_iri"],
                        values=probe["values"],
                        graph=candidate["graph"],
                    )
                    # build_enum_shape_nquads returns "" to signal "skip" (e.g. a
                    # value contains a quote, or values is empty) — don't count it.
                    if shape:
                        shapes.append(shape)
                else:
                    # Explain why this candidate was rejected so build logs are useful.
                    # The precision gate is checked FIRST: a value set can pass
                    # is_enum yet still be a name/near-unique column, and that is
                    # the most informative reason to surface. After that, surface
                    # the probe ERROR explicitly — an Athena failure (e.g.
                    # CATALOG_NOT_FOUND on a federated catalog) otherwise looks
                    # identical to a genuinely empty column, which masks infra/IAM
                    # gaps behind a benign-looking "no distinct values" message.
                    if not passed_col_gate:
                        reason = "not categorical (name/ratio)"
                    elif probe.get("error"):
                        reason = f"probe error: {probe['error']}"
                    elif probe["truncated"]:
                        reason = "high cardinality (probe truncated)"
                    elif not probe["values"]:
                        reason = "empty / no distinct values"
                    else:
                        reason = f"not enum-like ({probe['distinct_count']} distinct)"
                    logger.warning(
                        f"[enum] Skipping {candidate['table']}.{candidate['column']}: {reason}"
                    )
            except Exception as col_err:
                logger.warning(
                    f"[enum] Failed probing {candidate['table']}.{candidate['column']}: {col_err}"
                )
                continue

        shapes_written = 0
        if shapes:
            append_enum_shape_nquads(ontology_id, table_name, "\n".join(shapes))
            shapes_written = len(shapes)

        return json.dumps(
            {
                "success": True,
                "columns_probed": len(candidates),
                "enums_found": len(shapes),
                "shapes_written": shapes_written,
            }
        )
    except Exception as e:
        logger.error(f"[enum] enum_shape_builder failed for {table_name}: {e}")
        return json.dumps({"success": False, "error": str(e)})


@tool
def persist_file_to_neptune(ontology_id: str, table_name: str) -> str:
    """
    Persist the complete N-Quads for a table (Phase 1 + FK additions) to Neptune.

    Reads the local phase1 file content in Python and calls the Neptune tools
    Lambda directly via boto3 — the N-Quads are never output as LLM tokens,
    which avoids max_tokens overflow for wide tables (100-270 KB of N-Quads).

    Args:
        ontology_id: Ontology identifier
        table_name: Exact table name (must match the **Table:** field in the file)

    Returns:
        JSON with success status and Neptune message
    """
    import tempfile
    import re

    local_dir = os.path.join(tempfile.gettempdir(), "ontologies", ontology_id, "phase1")
    if not os.path.isdir(local_dir):
        return json.dumps(
            {"success": False, "error": f"Phase 1 directory not found: {local_dir}"}
        )

    nquad_content = None
    for filename in sorted(os.listdir(local_dir)):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(local_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        match = re.search(r"\*\*Table:\*\* (.+)", raw)
        found_name = match.group(1).strip() if match else ""
        if found_name != table_name:
            continue
        nq_match = re.search(r"```nquads\n(.*?)```", raw, re.DOTALL)
        nquad_content = nq_match.group(1).strip() if nq_match else ""
        break

    if nquad_content is None:
        return json.dumps(
            {
                "success": False,
                "error": f"No Phase 1 file found for table: {table_name}",
            }
        )
    if not nquad_content:
        return json.dumps(
            {"success": False, "error": f"Empty N-Quads for table: {table_name}"}
        )

    gateway_url = os.environ.get("NEPTUNE_GATEWAY_URL")
    if not gateway_url:
        return json.dumps(
            {"success": False, "error": "NEPTUNE_GATEWAY_URL not configured"}
        )

    try:
        import uuid

        region = os.environ.get("AWS_REGION", "us-east-1")
        mcp_client = MCPClient(
            lambda: aws_iam_streamablehttp_client(
                endpoint=gateway_url,
                aws_region=region,
                aws_service="bedrock-agentcore",
            )
        )
        with mcp_client:
            mcp_result = mcp_client.call_tool_sync(
                str(uuid.uuid4()),
                "persist-to-neptune___persist_to_neptune",
                {"nquad_data": nquad_content},
            )

        content_text = ""
        if mcp_result.get("content"):
            content_text = mcp_result["content"][0].get("text", "")
        # AgentCore Gateway wraps Lambda response as {"statusCode": 200, "body": "{...}"}
        # Unwrap the body field if present, otherwise parse directly
        envelope = json.loads(content_text) if content_text else {}
        body_str = envelope.get("body", "")
        data = json.loads(body_str) if body_str else envelope

        if data.get("success"):
            logger.info(f"Persisted {table_name} to Neptune: {data.get('message', '')}")
            return json.dumps(
                {
                    "success": True,
                    "table_name": table_name,
                    "message": data.get("message", ""),
                }
            )
        else:
            logger.error(
                f"Neptune persist failed for {table_name}: {data.get('message', '')}"
            )
            return json.dumps(
                {
                    "success": False,
                    "error": data.get("message", "Unknown error from Neptune Gateway"),
                }
            )
    except Exception as e:
        logger.error(f"Error calling Neptune Gateway for {table_name}: {e}")
        return json.dumps({"success": False, "error": str(e)})


@tool
def apply_targeted_edits(
    ontology_id: str,
    target_version: str,
    edits: List[Dict[str, str]],
) -> str:
    """
    Apply targeted triple-level edits to the base N-Quads file and save the result.

    Reads the base N-Quads from S3, applies each edit (old_triple → new_triple
    string substitution), then writes the revised file back to S3.  The agent
    only needs to supply the changed triples — never the full file content.

    Args:
        ontology_id: The ontology identifier
        target_version: The version string, e.g. 'v2'
        edits: List of {"old_triple": str, "new_triple": str} dicts.
               Each old_triple must be an exact N-Quads line (or substring)
               present in the base file.

    Returns:
        JSON string with success status, s3_path, and edit counts
    """
    bucket = os.environ.get("ARTIFACTS_BUCKET")
    if not bucket:
        return json.dumps({"success": False, "error": "ARTIFACTS_BUCKET not configured"})

    base_key = f"ontologies/{ontology_id}/revision/base_{target_version}.nq"
    out_key = f"ontologies/{ontology_id}/ontology_{target_version}.nq"

    try:
        session = get_boto_session()
        s3 = session.client("s3")

        obj = s3.get_object(Bucket=bucket, Key=base_key)
        content = obj["Body"].read().decode("utf-8")
        original_len = len(content)

        applied, not_found = 0, []
        for edit in edits:
            old = edit.get("old_triple", "")
            new = edit.get("new_triple", "")
            if old and old in content:
                content = content.replace(old, new, 1)
                applied += 1
            else:
                not_found.append(old[:120])

        s3.put_object(Bucket=bucket, Key=out_key, Body=content.encode("utf-8"))
        s3_path = f"s3://{bucket}/{out_key}"
        logger.info(
            f"[Revision] Applied {applied}/{len(edits)} edits; "
            f"{len(not_found)} not found. Saved to {s3_path} "
            f"({original_len} → {len(content)} chars)"
        )
        result = {
            "success": True,
            "nquads_s3_path": s3_path,
            "edits_applied": applied,
            "edits_total": len(edits),
        }
        if not_found:
            result["not_found"] = not_found
        return json.dumps(result)
    except Exception as e:
        logger.error(f"[Revision] apply_targeted_edits failed: {e}")
        return json.dumps({"success": False, "error": str(e)})


@tool
def persist_revision_from_s3(ontology_id: str, target_version: str) -> str:
    """
    Persist the revised ontology from S3 to Neptune.

    Reads the versioned N-Quads file that was produced by apply_targeted_edits
    from S3 and pushes it to the Neptune graph.  The agent passes only the
    ontology ID and version — no large content string required.

    Args:
        ontology_id: The ontology identifier
        target_version: The version string, e.g. 'v2'

    Returns:
        JSON string with success status and triple count
    """
    bucket = os.environ.get("ARTIFACTS_BUCKET")
    gateway_url = os.environ.get("NEPTUNE_GATEWAY_URL", "")
    if not bucket or not gateway_url:
        return json.dumps({"success": False, "error": "ARTIFACTS_BUCKET or NEPTUNE_GATEWAY_URL not configured"})

    key = f"ontologies/{ontology_id}/ontology_{target_version}.nq"
    try:
        import uuid as _uuid
        session = get_boto_session()
        s3 = session.client("s3")
        obj = s3.get_object(Bucket=bucket, Key=key)
        nquads_content = obj["Body"].read().decode("utf-8")
        logger.info(f"[Revision] Loaded {len(nquads_content)} chars from s3://{bucket}/{key}")

        region = os.environ.get("AWS_REGION", "us-east-1")
        mcp_client = MCPClient(
            lambda: aws_iam_streamablehttp_client(
                endpoint=gateway_url,
                aws_region=region,
                aws_service="bedrock-agentcore",
            )
        )
        with mcp_client:
            mcp_result = mcp_client.call_tool_sync(
                str(_uuid.uuid4()),
                "persist-to-neptune___persist_to_neptune",
                {"nquad_data": nquads_content},
            )

        content_text = ""
        if mcp_result.get("content"):
            content_text = mcp_result["content"][0].get("text", "")
        envelope = json.loads(content_text) if content_text else {}
        body_str = envelope.get("body", "")
        result = json.loads(body_str) if body_str else envelope

        success = result.get("success", False)
        logger.info(f"[Revision] Neptune persist result: {result}")
        return json.dumps({"success": success, "message": result.get("message", "")})
    except Exception as e:
        logger.error(f"[Revision] persist_revision_from_s3 failed: {e}")
        return json.dumps({"success": False, "error": str(e)})


@tool
def persist_nquads_to_neptune(nquads_content: str) -> str:
    """
    Push N-Quads content directly to Neptune via the MCPClient gateway.

    Args:
        nquads_content: The full N-Quads content as a string

    Returns:
        JSON string with success status
    """
    gateway_url = os.environ.get("NEPTUNE_GATEWAY_URL", "")
    if not gateway_url:
        error_msg = "NEPTUNE_GATEWAY_URL environment variable not set"
        logger.error(error_msg)
        return json.dumps({"success": False, "error": error_msg})

    try:
        import uuid

        region = os.environ.get("AWS_REGION", "us-east-1")
        mcp_client = MCPClient(
            lambda: aws_iam_streamablehttp_client(
                endpoint=gateway_url,
                aws_region=region,
                aws_service="bedrock-agentcore",
            )
        )
        with mcp_client:
            mcp_result = mcp_client.call_tool_sync(
                str(uuid.uuid4()),
                "persist-to-neptune___persist_to_neptune",
                {"nquad_data": nquads_content},
            )

        content_text = ""
        if mcp_result.get("content"):
            content_text = mcp_result["content"][0].get("text", "")
        # AgentCore Gateway wraps Lambda response as {"statusCode": 200, "body": "{...}"}
        # Unwrap the body field if present, otherwise parse directly
        envelope = json.loads(content_text) if content_text else {}
        body_str = envelope.get("body", "")
        result = json.loads(body_str) if body_str else envelope

        success = result.get("success", False)
        logger.info(f"[Revision] Neptune persist result: {result}")
        return json.dumps({"success": success, "message": result.get("message", "")})
    except Exception as e:
        logger.error(f"[Revision] Failed to persist N-Quads to Neptune: {e}")
        return json.dumps({"success": False, "error": str(e)})


def _is_federated_connector_catalog(catalog_id: str) -> bool:
    """Return True if catalog_id is an Athena Lambda connector name, not a Glue catalog ID.

    Athena connector catalogs (e.g. 'dynamodb_catalog', 'snowflake_fhir_db') are
    Lambda-backed and are NOT 12-digit account IDs. Passing one as CatalogId to the
    Glue API fails, and the tables behind it have no Glue entry to enrich at all —
    the metadata lives in the remote engine, reachable only through the connector.

    Args:
        catalog_id: The Athena catalog name from the layer's data-source config.

    Returns:
        True when the catalog is a federated connector, so callers skip Glue.
    """
    if not catalog_id or catalog_id in ("AWSDataCatalog", "AwsDataCatalog"):
        return False
    if catalog_id.isdigit():  # a 12-digit account id IS a real Glue catalog id
        return False
    # Same routing rule as get_single_table_schema: trust the registered catalog
    # Type, fall back to the connector-name heuristic when it is unavailable.
    return (
        "dynamodb" in catalog_id.lower()
        or _athena_catalog_type(catalog_id=catalog_id) == "LAMBDA"
    )


def _build_tables_list(data_sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Build the normalised tables list consumed by prompt builder and build loops.

    Each entry's coordinate is *persisted*: prompt_builder bakes it into the ontology graph as
    ``vkg:hasDatabase`` / ``vkg:hasCatalog`` / ``vkg:hasDataSource``, and every later query on
    the layer resolves through those triples. So a coordinate that is merely DESCRIBE-able is
    not good enough here — it has to be the one a SELECT can run against.

    That distinction matters for DynamoDB. The admin data-source picker denylists the
    connector catalog (``lambda/rest-api/services/glue_service.py``) because the connector's
    tables are already surfaced through the Glue database ``<project>_dynamodb`` with curated
    descriptions, so a user selecting a DynamoDB table hands us ``catalogId='AWSDataCatalog'``
    for a table Athena can read only via the connector — its Glue ``StorageDescriptor.Location``
    is a DynamoDB ARN, which Athena cannot parse as a URI. ``_resolve_catalog`` detects exactly
    that and re-routes to ``<connector>/default/<ARN tail>``; a changed catalog is the signal
    that it fired, since it returns the triple unchanged for everything else.

    Args:
        data_sources: Raw wizard ``dataSources`` entries. Requires ``databaseName`` and
                      ``tableName``; ``catalogId``, ``dataSource``, ``tableId``,
                      ``glueDatabaseName`` and ``glueTableName`` are optional.

    Returns:
        One normalised dict per usable data source, with keys ``database``, ``table``,
        ``catalogId``, ``dataSource``, ``tableId``, ``glueDatabaseName``, ``glueTableName``.
        Sources missing a database or table name are skipped.
    """
    result = []
    for ds in data_sources:
        if not (ds.get("databaseName") and ds.get("tableName")):
            continue
        database = ds["databaseName"]
        table = ds["tableName"]
        catalog = ds.get("catalogId", "AWSDataCatalog")
        glue_database = ds.get("glueDatabaseName", "")
        glue_table = ds.get("glueTableName", "")
        data_source = ds.get("dataSource", "AwsDataCatalog")

        # NOTE: this is the only network I/O in this function — one Glue get_table per selected
        # table, and only for tables presented under a standard Glue catalog (_resolve_catalog
        # returns immediately for connector and S3 Tables catalogs). _build_tables_list runs
        # once per build (see the revision and build entrypoints below), so that cost is fine
        # here; it must NOT be moved inside a per-column loop.
        eff_db, eff_table, eff_cat = _resolve_catalog(
            database_name=database, table_name=table, catalog_id=catalog
        )
        if eff_cat != catalog:
            # A re-route happened. Keep the ORIGINAL Glue coordinate so Glue-catalog work
            # (update_glue_metadata_from_ontology's column comments, Iceberg metadata) can
            # still find the real Glue table, and treat the connector as the data source —
            # the same rule glue_service._data_source_for_catalog applies in the REST API.
            if not glue_database:
                glue_database = database
            if not glue_table:
                glue_table = table
            data_source = eff_cat
            database, table, catalog = eff_db, eff_table, eff_cat
        # When eff_cat == catalog nothing is rewritten. That covers the ordinary case AND a
        # swallowed Glue failure: _resolve_catalog logs a warning and returns the triple
        # unchanged rather than raising. At query time that soft failure is deliberate; here it
        # can persist a wrong coordinate, which is worse — but raising would fail an entire
        # build on a transient Glue blip, and "unchanged" is exactly right for the
        # overwhelming majority of tables, which are not DynamoDB-backed. Do not "tidy" this
        # into a raise without reading docs/plans/2026-08-17-ddb-authoring-coordinate-design.md.

        entry: Dict[str, Any] = {
            "database": database,
            "table": table,
            "catalogId": catalog,
            "dataSource": data_source,
            "tableId": ds.get("tableId", ""),
            "glueDatabaseName": glue_database,
            "glueTableName": glue_table,
        }
        result.append(entry)
    return result


def _fetch_s3tables_version_token(
    session: boto3.Session,
    effective_catalog: str,
    database_name: str,
    table_name: str,
) -> "Optional[str]":
    """
    Fetch the current versionToken for an S3 Tables (Iceberg) table.

    Used as a fallback retry in update_glue_metadata_from_ontology when Glue
    federation raises FederationSourceException with 'versionToken null'.
    Should NOT be called preemptively — injecting the token before the first
    attempt causes ValidationException on tables Glue has already versioned
    internally (after the first successful write Glue assigns its own integer
    VersionId; subsequent calls with a UUID S3 Tables token fail validation).
    """
    bucket_name = effective_catalog.split("/", 1)[1] if "/" in effective_catalog else ""
    if not bucket_name:
        logger.warning(
            f"Cannot fetch versionToken: malformed catalog_id {effective_catalog!r}"
        )
        return None
    try:
        account_id = session.client("sts").get_caller_identity()["Account"]
        region = session.region_name or "us-east-1"
        bucket_arn = f"arn:aws:s3tables:{region}:{account_id}:bucket/{bucket_name}"
        tbl = session.client("s3tables", region_name=region).get_table(
            tableBucketARN=bucket_arn, namespace=database_name, name=table_name
        )
        return tbl.get("versionToken")
    except Exception as e:
        logger.warning(
            f"Could not fetch S3 Tables versionToken for {table_name}: {e}"
        )
        return None


@tool
def update_glue_metadata_from_ontology(
    ontology_id: str,
    database_name: str,
    table_name: str,
    catalog_id: str = "",
    glue_database_name: str = "",
    glue_table_name: str = "",
) -> str:
    """
    Enrich AWS Glue Data Catalog table and column descriptions from the final ontology N-Quads.

    Reads the markdown file in the phase1/ directory for this table. Because
    update_nquads_in_file() writes Phase 2 ObjectProperty additions back to the
    same file in-place, this function always sees the fully-refined N-Quads
    (Phase 1 classes/properties + Phase 2 FK relationships) when called after
    update_nquads_in_file().

    Extracts rdfs:comment values from OWL classes and properties, detects PKs
    (heuristic: single _id column) and FKs (owl:ObjectProperty with mapsToColumn),
    then calls glue.update_table() to write column Comments and the table Description.

    Args:
        ontology_id: Ontology identifier (used to locate the markdown file)
        database_name: Glue database name (e.g. "semantic_layer_dynamodb")
        table_name: Glue table name (e.g. "semantic_layer_parties")
        catalog_id: Glue catalog ID or Athena connector name. Use the CATALOG_ID from
                    the table prompt exactly as given.
                    'AWSDataCatalog' or '' → standard Glue catalog (default).
                    's3tablescatalog/<bucket>' → S3 Tables (Iceberg).
                    'dynamodb_catalog' or any name containing 'dynamodb' →
                    Athena DynamoDB connector. The function strips the connector
                    name and tries the account-default Glue catalog. If the table
                    is not registered in Glue, a warning is logged and the function
                    returns success with columns_updated=0.
        glue_database_name: When the Athena catalog uses connector coords that differ from
                            the Glue Data Catalog (e.g. DynamoDB connector: database="default"),
                            pass the actual Glue database name here (e.g. "semantic_layer_dynamodb").
        glue_table_name: Actual Glue table name when it differs from the Athena connector name
                         (e.g. "semantic_layer_admin_codes" instead of "semantic-layer-admin-codes").
                         Both fields must be set together.

    Returns:
        JSON with success, columns_updated, primary_keys, foreign_keys counts
    """
    import tempfile
    import re

    try:
        # Load N-Quads for this table
        local_dir = os.path.join(
            tempfile.gettempdir(), "ontologies", ontology_id, "phase1"
        )
        nquad_content = ""
        if os.path.isdir(local_dir):
            for filename in os.listdir(local_dir):
                if not filename.endswith(".md"):
                    continue
                path = os.path.join(local_dir, filename)
                with open(path, "r", encoding="utf-8") as f:
                    raw = f.read()
                m = re.search(r"\*\*Table:\*\* (.+)", raw)
                if m and (table_name in m.group(1) or m.group(1).strip() in table_name):
                    nq_match = re.search(r"```nquads\n(.*?)```", raw, re.DOTALL)
                    if nq_match:
                        nquad_content = nq_match.group(1)
                    break

        if not nquad_content:
            return json.dumps(
                {"success": False, "error": f"No N-Quads found for {table_name}"}
            )

        # Parse rdfs:comment per subject URI
        comments: Dict[str, str] = {}
        maps_to_column: Dict[str, str] = {}  # property_uri -> "table.column"
        maps_to_table: Dict[str, str] = {}  # class_uri -> "db.table"
        object_properties: set = set()

        for line in nquad_content.splitlines():
            line = line.strip()
            if not line or not line.endswith("."):
                continue
            parts = line[:-1].strip().split(" ", 3)
            if len(parts) < 3:
                continue
            subj, pred = parts[0], parts[1]
            # Reconstruct full object — rdfs:comment literals contain spaces
            obj = " ".join(parts[2:])

            if pred == "<http://www.w3.org/2000/01/rdf-schema#comment>":
                # Extract literal value (greedy to capture multi-word descriptions)
                lit_match = re.match(r'"(.*)"', obj)
                if lit_match:
                    comments[subj] = lit_match.group(1)
            elif pred == _MAPS_TO_COLUMN_PRED:
                lit_match = re.match(r'"(.*)"', obj)
                if lit_match:
                    maps_to_column[subj] = lit_match.group(1)
            elif pred == _MAPS_TO_TABLE_PRED:
                lit_match = re.match(r'"(.*)"', obj)
                if lit_match:
                    maps_to_table[subj] = lit_match.group(1)
            elif (
                pred == "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
                and "<http://www.w3.org/2002/07/owl#ObjectProperty>" in obj
            ):
                object_properties.add(subj)

        # Build column -> description map
        col_descriptions: Dict[str, str] = {}
        fk_columns: List[Dict[str, str]] = []
        pk_columns: List[str] = []

        for prop_uri, col_ref in maps_to_column.items():
            col_name = col_ref.split(".")[-1]
            desc = comments.get(prop_uri, "")

            # PK heuristic
            if col_name.endswith("_id") and not any(
                other_col.split(".")[-1] == col_name
                for other_uri, other_col in maps_to_column.items()
                if other_uri != prop_uri
            ):
                pk_columns.append(col_name)
                desc = f"{desc} [PK]".strip()

            # FK: ObjectProperty → cross-table reference
            if prop_uri in object_properties:
                target = col_name.replace("_id", "").replace("_key", "")
                fk_columns.append({"column": col_name, "references": target})
                desc = f"{desc} [FK → {target}]".strip()

            if desc:
                col_descriptions[col_name] = desc

        # Find table-level description
        table_desc = ""
        for cls_uri, tbl_ref in maps_to_table.items():
            if table_name in tbl_ref:
                table_desc = comments.get(cls_uri, "")
                break

        # Update Glue table
        session = get_boto_session()
        glue = session.client("glue")

        is_federated_catalog = _is_federated_connector_catalog(catalog_id)

        # Prefer explicit Glue coords (set when Athena connector coords differ from Glue)
        effective_db = glue_database_name if glue_database_name else database_name
        effective_tbl = glue_table_name if glue_table_name else table_name

        if glue_database_name:
            # Explicit Glue coords supplied — always use default catalog
            effective_catalog = None
        elif is_federated_catalog:
            # A connector catalog is not a Glue catalog id; never send it to Glue.
            effective_catalog = None
        elif catalog_id and catalog_id != "AWSDataCatalog":
            effective_catalog = catalog_id
        else:
            effective_catalog = None

        get_kwargs: Dict[str, Any] = {"DatabaseName": effective_db, "Name": effective_tbl}
        if effective_catalog:
            get_kwargs["CatalogId"] = effective_catalog

        try:
            response = glue.get_table(**get_kwargs)
        except glue.exceptions.EntityNotFoundException:
            if is_federated_catalog:
                # Expected for a federated source (Snowflake, DynamoDB, …): the table
                # exists only in the remote engine, so there is no Glue entry to
                # enrich. Skipping is correct — the ontology itself still carries the
                # comments, and failing here would abort an otherwise good build.
                msg = (
                    f"Federated table {table_name} is not registered in the Glue Data Catalog "
                    f"(connector catalog: {catalog_id}). Glue metadata not updated."
                )
                logger.warning(msg)
                return json.dumps({
                    "success": True,
                    "table_name": table_name,
                    "columns_updated": 0,
                    "method": "skipped_not_in_glue",
                    "message": msg,
                })
            raise
        except Exception as get_err:
            err_str = str(get_err)
            # Certain S3 Tables entries raise ValidationException on GetTable with
            # "Unsupported Federation Resource" when the table name or column names are
            # invalid for federation.  Skip gracefully so the rest of the run succeeds.
            if "ValidationException" in err_str and "Unsupported Federation Resource" in err_str:
                msg = (
                    f"Skipping Glue update for {table_name}: GetTable raised "
                    f"ValidationException (Unsupported Federation Resource). "
                    f"catalog={effective_catalog or 'default'}"
                )
                logger.warning(msg)
                return json.dumps({
                    "success": True,
                    "table_name": table_name,
                    "columns_updated": 0,
                    "method": "skipped_federation_error",
                    "message": msg,
                })
            raise

        table_input = response["Table"]

        # Remove read-only fields (including S3 Tables / Iceberg federation fields
        # that Glue returns in get_table() but rejects in update_table())
        for field in [
            "CatalogId",
            "DatabaseName",
            "CreateTime",
            "UpdateTime",
            "CreatedBy",
            "IsRegisteredWithLakeFormation",
            "VersionId",
            "IsMultiDialectView",
            "Status",
            "FederatedTable",       # S3 Tables / federated catalog — read-only
            "IsMaterializedView",   # S3 Tables — read-only
        ]:
            table_input.pop(field, None)

        # S3 Tables returns Owner="" which fails boto3 validation (min length 1).
        # Strip it entirely — Glue treats absent Owner the same as the default.
        if not table_input.get("Owner"):
            table_input.pop("Owner", None)

        if table_desc:
            table_input["Description"] = table_desc

        updated_cols = 0
        for col in table_input.get("StorageDescriptor", {}).get("Columns", []):
            if col["Name"] in col_descriptions:
                col["Comment"] = col_descriptions[col["Name"]]
                updated_cols += 1

        update_kwargs: Dict[str, Any] = {
            "DatabaseName": effective_db,
            "TableInput": table_input,
        }
        if effective_catalog:
            update_kwargs["CatalogId"] = effective_catalog

        # Attempt the update without VersionId first.
        # If Glue federation raises FederationSourceException with 'versionToken null'
        # (can happen on freshly-registered S3 Tables when the federation layer hasn't
        # yet resolved the token internally), fetch the current S3 Tables versionToken
        # and retry once.
        #
        # We do NOT inject the token preemptively: after the first successful Glue
        # federation write the table gains an internal Glue VersionId (integer).
        # Passing an S3 Tables UUID token as that VersionId on any subsequent call
        # causes ValidationException: Unsupported Federation Resource.
        try:
            glue.update_table(**update_kwargs)
        except Exception as first_err:
            err_str = str(first_err)
            is_s3_tables = bool(
                effective_catalog and effective_catalog.startswith("s3tablescatalog/")
            )
            if is_s3_tables and "versionToken" in err_str and "null" in err_str:
                version_token = _fetch_s3tables_version_token(
                    session, effective_catalog, effective_db, effective_tbl
                )
                if version_token:
                    update_kwargs["VersionId"] = version_token
                    logger.info(
                        f"Retrying update_table with versionToken for {table_name}"
                    )
                    glue.update_table(**update_kwargs)
                else:
                    raise
            else:
                raise

        logger.info(
            f"Updated Glue metadata for {effective_db}.{effective_tbl}: {updated_cols} columns"
        )
        return json.dumps(
            {
                "success": True,
                "table_name": table_name,
                "table_description_updated": bool(table_desc),
                "columns_updated": updated_cols,
                "primary_keys": pk_columns,
                "foreign_keys": fk_columns,
                "method": "glue",
                "message": f"Updated {updated_cols} column comments for {table_name}",
            }
        )

    except Exception as e:
        logger.error(f"Error updating Glue metadata for {table_name}: {e}")
        return json.dumps({"success": False, "error": str(e)})


# ==============================================================================
# ICEBERG METADATA UPDATE — Layer 1: persist descriptions in S3 metadata files
# ==============================================================================

_RDFS_COMMENT_PRED = "<http://www.w3.org/2000/01/rdf-schema#comment>"
_MAPS_TO_COLUMN_PRED = f"<{VIRTUAL_KG_VOCAB}mapsToColumn>"
_MAPS_TO_TABLE_PRED = f"<{VIRTUAL_KG_VOCAB}mapsToTable>"


def _load_nquads_for_table(ontology_id: str, table_name: str) -> str:
    """Load N-Quads content for *table_name* from the local phase1 directory."""
    import re
    import tempfile

    local_dir = os.path.join(tempfile.gettempdir(), "ontologies", ontology_id, "phase1")
    if not os.path.isdir(local_dir):
        return ""
    for filename in os.listdir(local_dir):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(local_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        m = re.search(r"\*\*Table:\*\* (.+)", raw)
        if m and (table_name in m.group(1) or m.group(1).strip() in table_name):
            nq_match = re.search(r"```nquads\n(.*?)```", raw, re.DOTALL)
            if nq_match:
                return nq_match.group(1)
    return ""


def _parse_nquad_metadata(nquad_content: str):
    """Parse rdfs:comment, mapsToColumn, and mapsToTable triples from N-Quads text.

    Returns:
        (comments, maps_to_column, maps_to_table) — each a Dict[subject_uri, value].
    """
    import re

    comments: Dict[str, str] = {}
    maps_to_column: Dict[str, str] = {}
    maps_to_table: Dict[str, str] = {}

    for line in nquad_content.splitlines():
        line = line.strip()
        if not line or not line.endswith("."):
            continue
        parts = line[:-1].strip().split(" ", 3)
        if len(parts) < 3:
            continue
        subj, pred = parts[0], parts[1]
        # Reconstruct the full object field — rdfs:comment literals contain spaces
        # so parts[2] alone captures only the first word.  Joining parts[2:] and
        # using a greedy regex finds the closing quote correctly (named graph URIs
        # use <>, not quotes, so the last " in the joined string is the literal's
        # closing quote).
        obj = " ".join(parts[2:])

        if pred == _RDFS_COMMENT_PRED:
            lit = re.match(r'"(.*)"', obj)
            if lit:
                comments[subj] = lit.group(1)
        elif pred == _MAPS_TO_COLUMN_PRED:
            lit = re.match(r'"(.*)"', obj)
            if lit:
                maps_to_column[subj] = lit.group(1)
        elif pred == _MAPS_TO_TABLE_PRED:
            lit = re.match(r'"(.*)"', obj)
            if lit:
                maps_to_table[subj] = lit.group(1)

    return comments, maps_to_column, maps_to_table


def _update_iceberg_metadata_for_s3tables(
    ontology_id: str,
    tables: List[Dict[str, Any]],
) -> None:
    """Layer 1 — write ontology descriptions into Apache Iceberg metadata for S3 Tables.

    Column doc strings are a first-class field in the Iceberg schema spec; they are
    persisted in the Iceberg metadata JSON files in S3 and survive independently of
    any catalog.  Table-level descriptions are stored as Iceberg table properties
    (also in S3 metadata).

    Only processes tables whose ``catalogId`` starts with ``"s3tablescatalog/"``.
    Skips gracefully when pyiceberg is not installed or a table cannot be loaded.
    """
    try:
        from pyiceberg.catalog import load_catalog  # type: ignore
    except ImportError:
        logger.warning(
            "[Iceberg] pyiceberg not installed — skipping S3 Tables metadata update"
        )
        return

    # Group tables by S3 bucket (extracted from 's3tablescatalog/<bucket>')
    bucket_tables: Dict[str, List[Dict[str, Any]]] = {}
    for table_info in tables:
        cat = table_info.get("catalogId", "")
        if not cat.startswith("s3tablescatalog/"):
            continue
        bucket = cat.split("/", 1)[1]
        bucket_tables.setdefault(bucket, []).append(table_info)

    if not bucket_tables:
        logger.info(
            "[Iceberg] No S3 Tables in this ontology — skipping Iceberg metadata update"
        )
        return

    session = get_boto_session()
    region = session.region_name or os.environ.get("AWS_REGION", "us-east-1")

    try:
        account_id = session.client("sts").get_caller_identity()["Account"]
    except Exception as sts_err:
        logger.warning(f"[Iceberg] Could not resolve AWS account ID: {sts_err} — skipping")
        return

    for bucket, tbl_list in bucket_tables.items():
        warehouse_arn = f"arn:aws:s3tables:{region}:{account_id}:bucket/{bucket}"
        logger.info(f"[Iceberg] Loading S3Tables catalog for bucket {bucket}")
        try:
            # Use load_catalog with type="rest" + SigV4 — mirrors the approach used
            # by the s3tables-manager Lambda which is the proven working pattern.
            # Direct RestCatalog(...) instantiation fails because pyiceberg 0.11.1
            # cannot validate the catalog-type returned by the S3 Tables config endpoint.
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
            logger.error(
                f"[Iceberg] Failed to initialise S3Tables catalog for {bucket}: {cat_err}",
                exc_info=True,
            )
            continue

        for table_info in tbl_list:
            db = table_info["database"]
            tbl = table_info["table"]
            logger.info(f"[Iceberg] Updating metadata for {db}.{tbl}")

            nquad_content = _load_nquads_for_table(ontology_id, tbl)
            if not nquad_content:
                logger.warning(f"[Iceberg] No N-Quads found for {db}.{tbl} — skipping")
                continue

            comments, maps_to_column, maps_to_table = _parse_nquad_metadata(nquad_content)

            # Build column_name → doc mapping
            col_docs: Dict[str, str] = {
                col_ref.split(".")[-1]: comments[prop_uri]
                for prop_uri, col_ref in maps_to_column.items()
                if prop_uri in comments
            }

            # Build table description from the OWL class rdfs:comment
            table_desc = next(
                (
                    comments[cls_uri]
                    for cls_uri, tbl_ref in maps_to_table.items()
                    if tbl in tbl_ref and cls_uri in comments
                ),
                "",
            )

            if not col_docs and not table_desc:
                logger.info(f"[Iceberg] No descriptions to write for {db}.{tbl}")
                continue

            try:
                iceberg_table = catalog.load_table((db, tbl))
            except Exception as load_err:
                logger.error(f"[Iceberg] Could not load table {db}.{tbl}: {load_err}")
                continue

            # ── column doc strings (Iceberg schema spec, persisted in S3) ─────
            if col_docs:
                try:
                    # Build a case-insensitive map from lowercase name → actual Iceberg field name.
                    # The ontology extracts column names from the Glue catalog (always lowercase)
                    # but the Iceberg schema may use PascalCase (e.g. "CodeValue") because the
                    # DynamoDB backfill preserves the original DynamoDB attribute casing.
                    # update_column() is case-sensitive, so we must look up the canonical name first.
                    iceberg_fields_lower: Dict[str, str] = {
                        f.name.lower(): f.name
                        for f in iceberg_table.schema().fields
                    }

                    written = 0
                    with iceberg_table.update_schema() as schema_update:
                        for col_name, doc in col_docs.items():
                            canonical = iceberg_fields_lower.get(col_name.lower(), col_name)
                            try:
                                schema_update.update_column(canonical, doc=doc)
                                written += 1
                            except Exception as col_err:
                                logger.warning(
                                    f"[Iceberg] Skipping column {col_name} ({db}.{tbl}): {col_err}"
                                )
                    logger.info(
                        f"[Iceberg] Wrote {written}/{len(col_docs)} column doc(s) for {db}.{tbl}"
                    )
                except Exception as schema_err:
                    logger.error(
                        f"[Iceberg] Schema update failed for {db}.{tbl}: {schema_err}"
                    )

            # ── table description (Iceberg table properties, persisted in S3) ─
            if table_desc:
                try:
                    with iceberg_table.transaction() as txn:
                        txn.set_properties({"description": table_desc})
                    logger.info(f"[Iceberg] Wrote table description for {db}.{tbl}")
                except Exception as prop_err:
                    logger.error(
                        f"[Iceberg] Property update failed for {db}.{tbl}: {prop_err}"
                    )


# ===========================================================================
# OTEL tool-output log hook
# ===========================================================================

_tool_io_logger = logging.getLogger("strands.tool_io")


def _tool_output_log_hook(event: AfterToolCallEvent) -> None:
    """Emit full tool I/O as a structured OTEL log record to the runtime log group.

    Strands already records tool outputs as attributes on ``gen_ai.choice`` span
    events in the traces pipeline (``aws/spans``). Those event attributes are
    subject to ``OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`` / the global attribute length
    cap — large tool results (e.g. Athena schema responses, KB retrievals) can be
    truncated before the AgentCore evaluator reads them.

    This hook writes the same data through the OTEL *logs* pipeline
    (``OTEL_LOGS_EXPORTER=otlp`` is already set on the runtime), which routes to
    the runtime log group with no per-record size restriction. The current span
    context (traceId / spanId) is auto-attached by the OTEL log handler, allowing
    the evaluator to correlate the log record with its matching span.
    """
    result = event.result
    content = result.get("content", []) if result else []

    # Resolve current span context for trace correlation — best-effort.
    trace_id = ""
    span_id = ""
    if _otel_trace is not None:
        span = _otel_trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx and ctx.is_valid:  # nosemgrep: is-function-without-parentheses — is_valid is a @property on SpanContext
            trace_id = format(ctx.trace_id, "032x")
            span_id = format(ctx.span_id, "016x")

    _tool_io_logger.info(
        json.dumps({
            "event": "tool_result",
            "tool": event.tool_use.get("name", ""),
            "tool_use_id": event.tool_use.get("toolUseId", ""),
            "status": result.get("status", "") if result else "error",
            "output": content,
            "trace_id": trace_id,
            "span_id": span_id,
        })
    )


def _create_bedrock_model() -> BedrockModel:
    """Shared model configuration for all ontology agents."""
    boto_config = Config(
        read_timeout=900,
        connect_timeout=60,
        retries={"max_attempts": 3, "mode": "adaptive"},
    )
    return BedrockModel(
        model_id=MODEL_ID,
        # NOTE: `temperature` is intentionally omitted — Opus 4.8 deprecated the
        # parameter and rejects any value with a ValidationException on
        # ConverseStream ("`temperature` is deprecated for this model").
        # 16 000 output tokens per turn. max_tokens is the OUTPUT limit only — it
        # does not affect input context. For wide tables with long column names,
        # 10 columns × 6 triples × ~300-char URIs ≈ 4 500 output tokens for N-Quads
        # alone, plus model reasoning. 8 000 was too tight and caused
        # MaxTokensReachedException mid-batch for financialactivity (167 cols).
        max_tokens=16000,
        cache_tools="default",
        boto_session=get_boto_session(),
        boto_client_config=boto_config,
    )


def create_phase1_agent() -> Agent:
    """
    Create an agent for Phase 1 per-table ontology generation.

    Tools scoped to the Phase 1 workflow only:
    schema retrieval, sample data EDA, RAG patterns, reference document access,
    incremental N-Quad append, intermediate save, and progress update.
    """
    tools = [
        get_single_table_schema,  # Fetch column definitions from Glue
        sample_table_data,  # EDA: sample rows to confirm value patterns and FK hints
        retrieve_ontology_patterns,  # RAG: retrieve relevant ontology patterns from Knowledge Base
        download_document_from_s3,  # Download uploaded reference documents to local filesystem
        search_document,  # Search a downloaded document for a term
        read_document_lines,  # Read specific line range from a downloaded document
        append_nquads,  # Incrementally write N-Quad batches (avoids MaxTokens for wide tables)
        save_intermediate_ontology,  # Finalise per-table N-Quads (auto-merges append_nquads batches)
        update_progress,  # Write tablesProcessed / progressPercent to DynamoDB
    ]
    return Agent(
        model=_create_bedrock_model(),
        system_prompt=build_phase1_system_prompt(),
        tools=tools,
        # Limit input context growth across many sequential append_nquads calls.
        # max_tokens (above) fixes output truncation (MaxTokensReachedException).
        # This fixes the separate input side: each tool call/result pair adds to
        # the conversation history (input tokens). For 17+ batches of a wide table,
        # evicting old messages keeps input cost and latency stable and prevents
        # eventually hitting the 200K input context limit. N-Quads are safe to
        # evict because they are persisted to disk by the tool, not re-read.
        conversation_manager=SlidingWindowConversationManager(window_size=30),
        hooks=[_tool_output_log_hook],
    )


def create_phase2_agent() -> Agent:
    """
    Create an agent for Phase 2 per-table refinement and persistence.

    Tools scoped to the Phase 2 workflow only:
    append FK triples to Phase 1 files, persist to Neptune (via Lambda — avoids
    max_tokens overflow for wide tables), and enrich the Glue Data Catalog.

    Note: update_nquads_in_file is intentionally absent — it required the agent
    to output the full N-Quads content (100-270 KB) as a tool-call parameter,
    which exceeds max_tokens for wide tables.
    persist_file_to_neptune reads the file in Python and calls the Neptune Gateway
    via MCPClient (aws_iam_streamablehttp_client), so N-Quads never appear as LLM
    output tokens.
    """
    tools = [
        append_fk_triples,  # Append ONLY new FK triples — no full content output
        persist_file_to_neptune,  # Read file in Python → call Neptune Lambda → no token overflow
        update_glue_metadata_from_ontology,  # Write rdfs:comment values to Glue column descriptions
    ]
    return Agent(
        model=_create_bedrock_model(),
        system_prompt=build_phase2_system_prompt(),
        tools=tools,
        conversation_manager=SlidingWindowConversationManager(window_size=10),
        hooks=[_tool_output_log_hook],
    )


def create_revision_agent() -> Agent:
    """
    Create a Strands agent for revising an existing ontology.

    Tools scoped to revision workflow:
    download reference documents, search/read them, save revised N-Quads,
    and persist to Neptune.
    """
    tools = [
        download_document_from_s3,
        read_document_lines,
        search_document,
        apply_targeted_edits,
        persist_revision_from_s3,
    ]
    return Agent(
        model=_create_bedrock_model(),
        system_prompt=build_revision_system_prompt(),
        tools=tools,
        conversation_manager=SlidingWindowConversationManager(window_size=20),
        hooks=[_tool_output_log_hook],
    )


def _run_revision_mode(ontology_id: str, config: dict) -> None:
    """
    Run revision mode: upload context to S3, delete old Neptune graph, run revision agent, update DynamoDB.

    Args:
        ontology_id: Ontology identifier
        config: Ontology configuration from DynamoDB containing targetVersion, revisionInstructions, etc.
    """
    bucket = os.environ["ARTIFACTS_BUCKET"]
    target_version = config["targetVersion"]
    ontology_path = config.get("metadataPath", "")
    revision_instructions = config.get("revisionInstructions", [])
    namespace = build_namespace(ontology_id, config)

    logger.info(
        f"[Revision] Starting revision mode for {ontology_id}, target version {target_version}"
    )
    update_dynamodb_status(
        ontology_id, "processing", phase="revision", progressPercent=10
    )

    # Create session once and reuse throughout function
    session = get_boto_session()

    # 1. Download existing N-Quads from S3
    try:
        s3 = session.client("s3")
        base_key = ontology_path.replace(f"s3://{bucket}/", "")
        obj = s3.get_object(Bucket=bucket, Key=base_key)
        base_nquads = obj["Body"].read().decode("utf-8")
        logger.info(f"[Revision] Downloaded base N-Quads ({len(base_nquads)} chars)")
    except Exception as e:
        logger.error(f"[Revision] Failed to download base N-Quads: {e}")
        update_dynamodb_status(ontology_id, "failed", errorMessage=str(e))
        return

    # 2. Upload base N-Quads as revision context
    base_context_key = f"ontologies/{ontology_id}/revision/base_{target_version}.nq"
    s3.put_object(Bucket=bucket, Key=base_context_key, Body=base_nquads.encode("utf-8"))
    base_nquads_s3_path = f"s3://{bucket}/{base_context_key}"
    logger.info(f"[Revision] Uploaded base context to {base_nquads_s3_path}")

    # 3. Upload instructions as markdown
    instructions_lines = []
    for ann in revision_instructions:
        instructions_lines.append(
            f"### Annotation\n**Highlighted text:** {ann.get('highlightedText', '')}\n"
            f"**Comment:** {ann.get('comment', '')}\n"
        )
    instructions_md = "\n".join(instructions_lines)
    instructions_key = (
        f"ontologies/{ontology_id}/revision/instructions_{target_version}.md"
    )
    s3.put_object(
        Bucket=bucket, Key=instructions_key, Body=instructions_md.encode("utf-8")
    )
    instructions_s3_path = f"s3://{bucket}/{instructions_key}"
    logger.info(f"[Revision] Uploaded instructions to {instructions_s3_path}")

    update_dynamodb_status(
        ontology_id, "processing", phase="revision", progressPercent=30
    )

    # 4. Delete old Neptune graph(s) — every graph this layer id ever wrote, since
    #    earlier rebuilds each minted their own and any survivor can be served.
    logger.info(
        "[Revision] Cleared Neptune graphs for %s: %s",
        ontology_id,
        _clear_prior_neptune_graphs(ontology_id=ontology_id),
    )

    update_dynamodb_status(
        ontology_id, "processing", phase="revision", progressPercent=50
    )

    # 5. Run revision agent
    try:
        agent = create_revision_agent()
        prompt = build_revision_prompt(
            ontology_id=ontology_id,
            target_version=target_version,
            base_nquads_s3_path=base_nquads_s3_path,
            instructions_s3_path=instructions_s3_path,
            namespace=namespace,
        )
        agent(prompt)
        logger.info(f"[Revision] Revision agent completed for {ontology_id}")
    except Exception as e:
        logger.error(f"[Revision] Revision agent failed: {e}")
        update_dynamodb_status(ontology_id, "failed", errorMessage=str(e))
        return

    update_dynamodb_status(
        ontology_id, "processing", phase="revision", progressPercent=80
    )

    # 6. Write history DynamoDB record (SK = targetVersion)
    revised_path = (
        f"s3://{bucket}/ontologies/{ontology_id}/ontology_{target_version}.nq"
    )
    now = datetime.now(timezone.utc).isoformat()
    history_item = {
        **config,
        "version": target_version,
        "metadataPath": revised_path,
        "status": "completed",
        "revisionMode": False,
        "completedAt": now,
    }
    history_item.pop("revisionInstructions", None)
    history_item.pop("targetVersion", None)
    history_item.pop("revisionBaseVersion", None)
    history_item.pop("currentVersion", None)  # not used in highest-version model
    table = session.resource("dynamodb").Table(os.environ["ONTOLOGY_METADATA_TABLE"])
    table.put_item(Item=history_item)
    logger.info(f"[Revision] Wrote history record for version {target_version}")

    # 7. Mark the previous active version as inactive — the new target_version
    #    record is now the highest and becomes the active record automatically.
    prev_version = config["version"]
    table.update_item(
        Key={"id": ontology_id, "version": prev_version},
        UpdateExpression="SET #status = :inactive, updatedAt = :now",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":inactive": "inactive", ":now": now},
    )
    logger.info(f"[Revision] Marked version {prev_version} as inactive; {target_version} is now active")

    # 8. Update Glue column comments + Iceberg metadata from the REVISED N-Quads.
    #
    # _load_nquads_for_table reads from local phase1 files written during the initial
    # build.  In revision mode those files either don't exist (fresh container) or
    # contain the pre-revision descriptions.  Fix: download the revised ontology file
    # from S3 and write synthetic phase1 markdown files so both
    # _update_iceberg_metadata_for_s3tables and update_glue_metadata_from_ontology
    # read the correct (post-revision) N-Quads.
    tables_list = _build_tables_list(config.get("dataSources", []))
    if tables_list:
        import tempfile as _tempfile
        revised_nquads = ""
        try:
            revised_key = f"ontologies/{ontology_id}/ontology_{target_version}.nq"
            obj = s3.get_object(Bucket=bucket, Key=revised_key)
            revised_nquads = obj["Body"].read().decode("utf-8")
            logger.info(
                f"[Revision] Downloaded revised N-Quads ({len(revised_nquads)} chars) "
                f"from {revised_key}"
            )
        except Exception as dl_err:
            logger.warning(f"[Revision] Could not download revised N-Quads: {dl_err}")

        if revised_nquads:
            # Populate phase1 local directory with one synthetic markdown file per
            # table so _load_nquads_for_table picks up the revised content.
            phase1_dir = os.path.join(
                _tempfile.gettempdir(), "ontologies", ontology_id, "phase1"
            )
            os.makedirs(phase1_dir, exist_ok=True)
            for idx, tinfo in enumerate(tables_list, 1):
                tbl = tinfo["table"]
                filename = f"table-{idx:02d}-{tbl}.md"
                path = os.path.join(phase1_dir, filename)
                with open(path, "w", encoding="utf-8") as _f:
                    _f.write(
                        f"**Ontology ID:** {ontology_id}\n"
                        f"**Table:** {tbl}\n\n"
                        f"```nquads\n{revised_nquads}\n```\n"
                    )
            logger.info(
                f"[Revision] Wrote {len(tables_list)} phase1 file(s) from revised N-Quads"
            )

            # Update Glue column comments from the revised descriptions.
            for tinfo in tables_list:
                try:
                    result_json = update_glue_metadata_from_ontology(
                        ontology_id=ontology_id,
                        database_name=tinfo["database"],
                        table_name=tinfo["table"],
                        catalog_id=tinfo.get("catalogId", ""),
                        glue_database_name=tinfo.get("glueDatabaseName", ""),
                        glue_table_name=tinfo.get("glueTableName", ""),
                    )
                    result = json.loads(result_json)
                    if result.get("success"):
                        logger.info(
                            f"[Revision] Updated Glue metadata for "
                            f"{tinfo['database']}.{tinfo['table']}: "
                            f"{result.get('columns_updated', 0)} columns"
                        )
                    else:
                        logger.warning(
                            f"[Revision] Glue update failed for "
                            f"{tinfo['database']}.{tinfo['table']}: "
                            f"{result.get('error', 'unknown')}"
                        )
                except Exception as glue_err:
                    logger.warning(
                        f"[Revision] Glue update error for "
                        f"{tinfo['database']}.{tinfo['table']} (non-fatal): {glue_err}"
                    )

        logger.info(
            f"[Revision] Writing column doc strings and table descriptions for {ontology_id}"
        )
        try:
            _update_iceberg_metadata_for_s3tables(ontology_id, tables_list)
        except Exception as iceberg_err:
            logger.warning(
                f"[Revision] Iceberg metadata update encountered an error (non-fatal): {iceberg_err}"
            )

    update_dynamodb_status(
        ontology_id, "completed", phase="revision", progressPercent=100, completedAt=now
    )
    emit_ontology_published(ontology_id=ontology_id, namespace=namespace)
    logger.info(f"[Revision] Revision mode complete for {ontology_id}")


# Helper function to update DynamoDB status
def emit_ontology_published(*, ontology_id: str, namespace: str) -> None:
    """Emit an EventBridge ``ontology.published`` event so the topic-router
    rebuild Lambda re-indexes the per-namespace KNN store.

    Without this, Tier 2 Phase 1 always returns no candidates and queries
    silently degrade to Tier 3 — which is exactly the regression the
    progressive-disclosure design doc warned against.

    Args:
        ontology_id: Ontology identifier (used for tracing only).
        namespace: Logical namespace; the rebuilder reads this from
            ``detail.namespace`` to scope the KNN index name.
    """
    try:
        client = get_boto_session().client("events")
        client.put_events(
            Entries=[{
                "Source": "semantic-layer.ontology",
                "DetailType": "ontology.published",
                "Detail": json.dumps({
                    "ontology_id": ontology_id,
                    "namespace": namespace,
                }),
            }],
        )
        logger.info(
            f"[EventBridge] Emitted ontology.published for {ontology_id} "
            f"(namespace={namespace})"
        )
    except Exception as e:
        # Fail loudly via log — the build is already committed; we don't
        # want to roll it back just because the event bus is unhappy.
        logger.error(f"[EventBridge] Failed to emit ontology.published: {e}")


def update_dynamodb_status(ontology_id: str, status: str, version: str = None, **kwargs):
    """Update ontology status in DynamoDB.

    Args:
        ontology_id: Ontology identifier.
        status: New status value.
        version: Explicit version sort-key to target. When omitted, resolves
            the active version via _resolve_active_version (highest numeric version).
    """
    try:
        session = get_boto_session()
        dynamodb = session.resource("dynamodb")
        table_name = os.environ.get(
            "ONTOLOGY_METADATA_TABLE", "semantic-layer-metadata"
        )
        table = dynamodb.Table(table_name)

        if version is None:
            version = _resolve_active_version(table, ontology_id)

        update_data = {
            "status": status,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }

        # Build update expression
        update_expr = "SET " + ", ".join([f"#{k} = :{k}" for k in update_data.keys()])
        expr_attr_names = {f"#{k}": k for k in update_data.keys()}
        expr_attr_values = {f":{k}": v for k, v in update_data.items()}

        table.update_item(
            Key={"id": ontology_id, "version": version},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_attr_names,
            ExpressionAttributeValues=expr_attr_values,
        )

        logger.info(f"Updated DynamoDB: {ontology_id} (version={version}) → {status}")

    except Exception as e:
        logger.error(f"Failed to update DynamoDB status: {e}")


# AgentCore entrypoint
@app.entrypoint
def invoke(payload, context):
    """
    Main entrypoint for ontology generation agent.

    Receives id, reads config from DynamoDB, builds prompts,
    starts background processing, and returns immediately.

    Args:
        payload: Contains id
        context: Request context

    Returns:
        Immediate response with status 'processing'
    """
    ontology_id = payload.get("id")
    session_id = context.session_id if hasattr(context, "session_id") else str(uuid.uuid4())
    # baggage.set_baggage returns a NEW Context — it does not mutate the active one.
    # Attach it so the "session.id" baggage is present on the current context, which is
    # then captured by contextvars.copy_context() below and carried into the background
    # thread so AgentCore tags its spans with this runtimeSessionId.
    if _otel_baggage and _otel_context:
        _otel_context.attach(_otel_baggage.set_baggage("session.id", session_id))

    if not ontology_id:
        return {"error": "id required in payload"}

    logger.info(f"[Entrypoint] Starting ontology build for: {ontology_id}")

    try:
        # Read configuration from DynamoDB — always use the highest version record
        session = get_boto_session()
        dynamodb = session.resource("dynamodb")
        table_name = os.environ.get(
            "ONTOLOGY_METADATA_TABLE", "semantic-layer-metadata"
        )
        table = dynamodb.Table(table_name)

        active_version = _resolve_active_version(table, ontology_id)
        response = table.get_item(
            Key={"id": ontology_id, "version": active_version},
            ConsistentRead=True,
        )

        if "Item" not in response:
            return {"error": f"Ontology configuration not found: {ontology_id}"}

        config = response["Item"]
        ontology_namespace = build_namespace(ontology_id, config)
        logger.info(
            f"[Entrypoint] Loaded config (version={active_version}): {config.get('name')} "
            f"with {len(config.get('dataSources', []))} data sources"
            f" (namespace: {ontology_namespace})"
        )

        # Update status to 'processing' on the active version
        update_dynamodb_status(
            ontology_id=ontology_id,
            status="processing",
            version=active_version,
            buildStartedAt=datetime.now(timezone.utc).isoformat(),
        )

        # Parse tables list from config — all four identifiers are passed per-table through prompts
        tables_list = _build_tables_list(config.get("dataSources", []))
        delta_targets = config.get("deltaTargetTables")
        tables_list = _filter_delta_targets(tables_list, delta_targets)
        total_tables = len(tables_list)
        logger.info(f"[Entrypoint] {total_tables} tables to process")

        # Start async task tracking
        task_id = app.add_async_task("ontology_build", {"ontology_id": ontology_id})

        # Start background processing
        def background_work():
            try:
                if config.get("revisionMode"):
                    logger.info(f"[Revision] Revision mode for {ontology_id}")
                    _run_revision_mode(ontology_id, config)
                    return  # skip Phase 1 / Phase 2 / Assembly

                logger.info(
                    f"[Background] Starting two-phase ontology generation for {ontology_id}"
                )

                # A rebuild writes into a NEW named graph (the URI embeds the
                # timestamped layer name), so without this the superseded graphs
                # survive and can be served instead of the fresh one. Skipped for
                # a delta build, which by definition adds to the existing graph.
                if not delta_targets:
                    logger.info(
                        "[Background] Clearing prior Neptune graphs for %s: %s",
                        ontology_id,
                        _clear_prior_neptune_graphs(ontology_id=ontology_id),
                    )

                # ── PHASE 1: incremental per-table processing ──────────────────
                update_dynamodb_status(
                    ontology_id=ontology_id,
                    status="processing",
                    phase="incremental",
                )

                phase1_failures: List[str] = []
                for idx, table_info in enumerate(tables_list, 1):
                    db = table_info["database"]
                    tbl = table_info["table"]
                    cat = table_info["catalogId"]
                    logger.info(
                        f"[Phase 1] Table {idx}/{total_tables}: {db}.{tbl} (catalog: {cat})"
                    )

                    table_prompt = build_phase1_table_prompt(
                        ontology_id=ontology_id,
                        config=config,
                        table_info=table_info,
                        all_tables=tables_list,
                        step=idx,
                        total_steps=total_tables,
                    )
                    # Fresh agent per table — avoids conversation history accumulation
                    # across large table sets (1000s of tables would overflow context).
                    # Per-table state is persisted to filesystem by save_intermediate_ontology,
                    # so a clean context is safe here.
                    try:
                        agent = create_phase1_agent()
                        agent(table_prompt)
                    except MaxTokensReachedException:
                        logger.warning(
                            f"[Phase 1] MaxTokensReachedException for {db}.{tbl} — "
                            "skipping table and continuing with next"
                        )
                        phase1_failures.append(f"{db}.{tbl}")
                    except Exception as table_err:
                        logger.warning(
                            f"[Phase 1] Unexpected error for {db}.{tbl}: {table_err} — "
                            "skipping table and continuing with next"
                        )
                        phase1_failures.append(f"{db}.{tbl}")

                if phase1_failures:
                    logger.warning(
                        f"[Phase 1] Skipped {len(phase1_failures)} table(s) due to errors: {phase1_failures}"
                    )

                logger.info(f"[Phase 1] All {total_tables} tables processed")

                # ── PHASE 2: holistic refinement ───────────────────────────────
                # Build FK plan in Python — avoids loading all N-Quads into an
                # agent context just to extract small fk_hints strings.
                update_dynamodb_status(
                    ontology_id=ontology_id,
                    status="processing",
                    phase="refinement",
                    progressPercent=95,
                )

                fragments_json = load_phase1_fragments(ontology_id)
                fragments = json.loads(fragments_json)
                fk_plan = build_fk_plan(fragments)

                logger.info(
                    f"[Phase 2] FK plan: {sum(len(v) for v in fk_plan.values())} relationships across {len(fk_plan)} tables"
                )

                # Fresh agent per table — mirrors Phase 1 to prevent context accumulation.
                # Each agent handles exactly one table: read → optionally add ObjectProperties
                # → persist to Neptune → update Glue.
                phase2_failures: List[str] = []
                for idx, table_info in enumerate(tables_list, 1):
                    db = table_info["database"]
                    tbl = table_info["table"]
                    cat = table_info["catalogId"]
                    fk_rels = fk_plan.get(tbl, [])
                    logger.info(
                        f"[Phase 2] Table {idx}/{total_tables}: {db}.{tbl} (catalog: {cat}, {len(fk_rels)} FK(s))"
                    )

                    table_prompt = build_phase2_table_prompt(
                        ontology_id=ontology_id,
                        namespace=ontology_namespace,
                        table_info=table_info,
                        fk_relationships=fk_rels,
                    )
                    p2_ok = True
                    try:
                        agent_p2 = create_phase2_agent()
                        agent_p2(table_prompt)
                    except MaxTokensReachedException:
                        logger.warning(
                            f"[Phase 2] MaxTokensReachedException for {db}.{tbl} — "
                            "skipping table and continuing with next"
                        )
                        phase2_failures.append(f"{db}.{tbl}")
                        p2_ok = False
                    except Exception as table_err:
                        logger.warning(
                            f"[Phase 2] Unexpected error for {db}.{tbl}: {table_err} — "
                            "skipping table and continuing with next"
                        )
                        phase2_failures.append(f"{db}.{tbl}")
                        p2_ok = False

                    # Deterministic SHACL enum-shape emission (no LLM): probe each
                    # string column's distinct values and append sh:in constraints to
                    # the table's N-Quads block before assembly. Fail-soft — a probe
                    # failure never aborts the build (the table just carries no enum
                    # shape). enum_shape_builder is intentionally NOT in any agent's
                    # tool list; it must only ever be invoked deterministically here.
                    try:
                        enum_res = json.loads(
                            enum_shape_builder(
                                ontology_id=ontology_id,
                                table_name=tbl,
                                database=db,
                                catalog=cat,
                            )
                        )
                        logger.info(
                            f"[EnumShapes] {db}.{tbl}: {enum_res.get('shapes_written', 0)} "
                            f"shape(s) from {enum_res.get('enums_found', 0)} enum column(s)"
                        )
                    except Exception as enum_err:  # noqa: BLE001 — never abort the build
                        logger.warning(
                            f"[EnumShapes] {db}.{tbl} failed: {enum_err} — continuing"
                        )

                    # Fingerprint only after Phase 2 succeeded for this table — a table
                    # is fully built only after BOTH phases (+enum shapes). A Phase 2
                    # failure leaves no fingerprint, so it is retried next delta run.
                    # Wrapped so a fingerprint hiccup never aborts the build.
                    if p2_ok:
                        try:
                            _record_table_fingerprint(
                                get_boto_session().resource("dynamodb").Table(
                                    os.environ.get("ONTOLOGY_METADATA_TABLE", "semantic-layer-metadata")),
                                ontology_id, active_version, db, tbl, cat)
                        except Exception as fp_err:  # noqa: BLE001
                            logger.warning("fingerprint write failed for %s.%s: %s", db, tbl, fp_err)

                if phase2_failures:
                    logger.warning(
                        f"[Phase 2] Skipped {len(phase2_failures)} table(s) due to errors: {phase2_failures}"
                    )

                logger.info(f"[Phase 2] Refinement complete for {ontology_id}")

                # ── ASSEMBLY: concatenate all per-table N-Quads into one S3 file ──
                # Runs after Phase 2 so all files contain the final content
                # (Phase 1 classes/properties + Phase 2 ObjectProperty additions).
                # Done here in Python — not inside a Phase 2 agent — because no
                # single per-table agent knows when it is last, and loading all
                # fragments into an agent context would defeat per-table isolation.
                logger.info(
                    f"[Assembly] Building consolidated ontology for {ontology_id}"
                )
                assembly_succeeded = False
                try:
                    # Build the COMPLETE consolidated N-Quads over ALL tables'
                    # last-built fragments (not just this run's delta subset).
                    # See _assemble_consolidated for the completeness guarantee.
                    consolidated = _assemble_consolidated(ontology_id)
                    # Stamp a VERIFIED primary key per class before saving. Ontop
                    # treats the subject template as row identity, so without a
                    # declared key it guesses by column name and every property
                    # access self-joins on that guess. Measured, not asked of the
                    # model — see key_stamp for why. Fail-soft: an unstamped class
                    # keeps today's heuristic behaviour.
                    key_delta = ""
                    if consolidated:
                        consolidated, key_delta = _stamp_verified_primary_keys(
                            ontology_id=ontology_id,
                            nquads=consolidated,
                            tables=tables_list,
                        )
                    # Push the key triples to NEPTUNE as well. Phase 1 persisted the
                    # per-table N-Quads to Neptune, and the query path reads the
                    # ontology from Neptune — the S3 file is documentation/backup. A
                    # key stamped only into S3 would never reach the translator, so
                    # the whole fix would be silently inert. persist_to_neptune is an
                    # INSERT DATA, so re-running a build re-inserts identical quads
                    # harmlessly. Non-fatal: without it the translator falls back to
                    # its name heuristic, which is today's behaviour.
                    if key_delta.strip():
                        try:
                            push_result = json.loads(
                                persist_nquads_to_neptune(key_delta)
                            )
                            if push_result.get("success"):
                                logger.info(
                                    "[KeyStamp] pushed verified key triples to Neptune "
                                    "for %s", ontology_id,
                                )
                            else:
                                logger.error(
                                    "[KeyStamp] Neptune push of key triples FAILED for "
                                    "%s (%s) — the ontology in Neptune carries no "
                                    "declared keys, so row identity stays a name guess",
                                    ontology_id, push_result.get("error")
                                    or push_result.get("message"),
                                )
                        except Exception as push_err:  # noqa: BLE001 — non-fatal
                            logger.error(
                                "[KeyStamp] Neptune push of key triples raised for %s: "
                                "%s — row identity stays a name guess",
                                ontology_id, push_err,
                            )
                    if consolidated:
                        save_result = json.loads(
                            save_ontology_to_s3(consolidated, ontology_id)
                        )
                        if save_result.get("success"):
                            logger.info(
                                f"[Assembly] Saved consolidated ontology: {save_result['s3_location']}"
                            )
                            update_dynamodb_status(
                                ontology_id=ontology_id,
                                status="processing",
                                metadataPath=save_result["s3_location"],
                            )
                            assembly_succeeded = True
                        else:
                            logger.error(
                                f"[Assembly] S3 save failed: {save_result.get('message')}"
                            )
                    else:
                        logger.error(
                            f"[Assembly] No N-Quads collected for {ontology_id} "
                            f"({len(fragments.get('tables', []))} fragment(s) checked, "
                            f"all empty or failed) — ontology.nq will NOT be saved"
                        )
                except Exception as assembly_err:
                    logger.error(
                        f"[Assembly] Failed to save consolidated ontology: {assembly_err}",
                        exc_info=True,
                    )

                if not assembly_succeeded:
                    update_dynamodb_status(
                        ontology_id=ontology_id,
                        status="failed",
                        error=(
                            "Assembly step produced no N-Quads. "
                            "Check CloudWatch logs for [Assembly] and [Phase 1] errors."
                        ),
                        failedAt=datetime.now(timezone.utc).isoformat(),
                    )
                    return  # skip Iceberg update and completed status

                # ── LAYER 1 ICEBERG: write descriptions into S3 metadata files ──
                # Column doc strings and table-level descriptions are stored directly
                # in the Iceberg metadata JSON files in S3 — independent of any catalog,
                # they persist forever.  Non-fatal: a failure here does not block completion.
                logger.info(
                    f"[Iceberg] Writing column doc strings and table descriptions for {ontology_id}"
                )
                try:
                    _update_iceberg_metadata_for_s3tables(ontology_id, tables_list)
                except Exception as iceberg_err:
                    logger.warning(
                        f"[Iceberg] Metadata update encountered an error (non-fatal): {iceberg_err}"
                    )

                # ── QUERYABILITY GATE ──────────────────────────────────────────
                # Refuse to stamp `completed` on a layer that cannot answer a
                # single query. A well-formed ontology can still fail to build an
                # Ontop reformulator, and that failure is invisible until query
                # time: bird_card_games was `completed` while every one of its
                # queries failed, silently scoring 52 benchmark rows zero. A loud
                # build failure is recoverable; a silently-unqueryable layer is not.
                queryable, probe_detail = _probe_layer_is_queryable(
                    ontology_id=ontology_id
                )
                if not queryable:
                    logger.error(
                        "[Queryability] %s is NOT queryable: %s", ontology_id, probe_detail
                    )
                    update_dynamodb_status(
                        ontology_id=ontology_id,
                        status="failed",
                        error=(
                            "Layer built but is NOT queryable — refusing to mark it "
                            f"completed. {probe_detail}"
                        ),
                        failedAt=datetime.now(timezone.utc).isoformat(),
                    )
                    return  # never publish or request an eval for a dead layer
                logger.info("[Queryability] %s OK: %s", ontology_id, probe_detail)

                update_dynamodb_status(
                    ontology_id=ontology_id,
                    status="completed",
                    phase="refinement_complete",
                    progressPercent=100,
                    completedAt=datetime.now(timezone.utc).isoformat(),
                )
                emit_ontology_published(
                    ontology_id=ontology_id,
                    namespace=ontology_namespace,
                )
                # Request an OnDemand evaluation of this freshly-completed VKG
                # layer against its maintained ground-truth dataset. Best-effort
                # event emit — never blocks/rolls back the completed build.
                try:
                    try:
                        from agents.shared.eval_trigger import emit_evaluation_requested  # type: ignore
                    except ImportError:
                        from shared.eval_trigger import emit_evaluation_requested  # type: ignore
                    emit_evaluation_requested(
                        ontology_id=ontology_id,
                        version=_resolve_active_version(
                            get_boto_session().resource("dynamodb").Table(
                                os.environ.get("ONTOLOGY_METADATA_TABLE", "semantic-layer-metadata")
                            ),
                            ontology_id,
                        ),
                        layer_type="VKG",
                        boto_session=get_boto_session(),
                    )
                except Exception as _eval_exc:  # noqa: BLE001 — non-fatal
                    logger.error(f"[Eval] evaluation.requested emit failed: {_eval_exc}")

            except Exception as e:
                logger.error(
                    f"[Background] Build failed for {ontology_id}: {e}", exc_info=True
                )
                update_dynamodb_status(
                    ontology_id=ontology_id,
                    status="failed",
                    error=str(e),
                    failedAt=datetime.now(timezone.utc).isoformat(),
                )

            finally:
                app.complete_async_task(task_id)

        # Start background thread (daemon=True allows process to exit).
        # Run inside a copy of the current context so the OTel "session.id" baggage
        # (attached above) propagates into the thread; otherwise its spans are untagged
        # and AgentCore Evaluations finds no spans for this session.
        _ctx = contextvars.copy_context()
        threading.Thread(target=_ctx.run, args=(background_work,), daemon=True).start()

        # Return immediately (within ~3 seconds)
        return {
            "status": "processing",
            "message": "Ontology build started in background",
            "id": ontology_id,
            "task_id": task_id,
        }

    except Exception as e:
        logger.error(f"[Entrypoint] Failed to start ontology build: {e}", exc_info=True)

        # Update to failed status
        update_dynamodb_status(ontology_id=ontology_id, status="failed", error=str(e))

        return {"error": str(e), "id": ontology_id}


# Run the app
if __name__ == "__main__":
    logger.info("🚀 Starting Ontology Generation Agent")
    logger.info("Configuration:")
    logger.info(f"  - Region: {os.environ.get('AWS_REGION', 'not set')}")
    logger.info(
        f"  - DynamoDB Table: {os.environ.get('ONTOLOGY_METADATA_TABLE', 'not set')}"
    )
    logger.info(
        f"  - Artifacts Bucket: {os.environ.get('ARTIFACTS_BUCKET', 'not set')}"
    )
    logger.info(
        f"  - Neptune Gateway: {os.environ.get('NEPTUNE_GATEWAY_URL', 'not configured')}"
    )
    app.run()
