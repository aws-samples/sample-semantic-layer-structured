"""
Metadata Query Agent with Bedrock Knowledge Base
Queries Bedrock KB for metadata context, generates SQL queries, and executes on Athena.
Returns query results with semantic context from the knowledge base.
"""

import os
import json
import logging
import threading
import contextvars
from typing import Dict, Any, List, Optional
import boto3
try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp
except ImportError as e:
    import sys
    print(f"STARTUP ERROR: failed to import BedrockAgentCoreApp: {e}", flush=True)
    sys.exit(1)
try:
    from opentelemetry import baggage as _otel_baggage
    from opentelemetry import context as _otel_context
except ImportError:
    _otel_baggage = None  # type: ignore
    _otel_context = None  # type: ignore
from datetime import datetime, timezone
from strands import Agent, tool
from strands.models import BedrockModel
from .token_manager import count_tokens
from boto3.dynamodb.conditions import Key
from .query_prompts import QUERY_MODEL_ID, JUDGE_PROMPT, ROUTER_MODEL_ID, ROUTER_PROMPT
try:
    from agents.shared.prior_results import (
        set_session_id as _set_prior_results_session_id,
    )
    from agents.shared.followup import contextualize_question
    from agents.shared.clarification import (
        accumulate_prior,
        build_pending_clarification,
        load_pending_clarification,
        resolve_clarification_reply,
    )
    from agents.shared.chat_sessions import (
        ChatSessionService,
        SessionOwnershipError,
    )
    from agents.shared.answer_span import emit_answer_span
    from agents.shared.provenance import build_provenance
    from agents.shared.advisory import build_advisory_answer, classify_intent
except ImportError:
    from shared.prior_results import (  # type: ignore
        set_session_id as _set_prior_results_session_id,
    )
    from shared.followup import contextualize_question  # type: ignore
    from shared.clarification import (  # type: ignore
        accumulate_prior,
        build_pending_clarification,
        load_pending_clarification,
        resolve_clarification_reply,
    )
    from shared.chat_sessions import (  # type: ignore
        ChatSessionService,
        SessionOwnershipError,
    )
    from shared.answer_span import emit_answer_span  # type: ignore
    from shared.provenance import build_provenance  # type: ignore
    from shared.advisory import build_advisory_answer, classify_intent  # type: ignore

# Tier 1 (governed-metric) pre-check wiring. Tier 2 is now the Strands graph
# workflow (see .tier2.workflow); the old Tier 3 supervised-worker hand-off has
# been removed in favour of the graph's Phase 5 bounded execution agent.
try:
    from agents.shared.metric_lookup import lookup as tier1_lookup
    from agents.shared.metric_executor import execute_metric as tier1_execute
    from agents.shared import knn_index
    from agents.shared import cw_metrics
except ImportError:  # container path: agents/ is on PYTHONPATH
    from shared.metric_lookup import lookup as tier1_lookup  # type: ignore
    from shared.metric_executor import execute_metric as tier1_execute  # type: ignore
    from shared import knn_index  # type: ignore
    from shared import cw_metrics  # type: ignore
from .tier2.rag_topic_router import RagTopicRouter
from .tier2.rag_slice_builder import RagSliceBuilder
from .tier2.rag_query_generator import RagQueryGenerator
from .tier2.workflow import (
    PhaseDeps,
    SLICE_TOKEN_BUDGET,
    WorkflowContext,
    tier2_rag_workflow,
)
from .tier2.execution_agent import (
    apply_over_limit,
    build_execution_agent,
    ensure_limit,
    run_execution,
)
from .query_prompts import EXECUTION_PROMPT
try:
    from agents.ontology_query_agent.tier2.slice_judge import build_slice_judge
except ImportError:  # container path: agents/ is on PYTHONPATH
    from ontology_query_agent.tier2.slice_judge import build_slice_judge  # type: ignore

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
# Reduce noise from AWS SDK
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)

# Create AgentCore app instance
app = BedrockAgentCoreApp()
logging.getLogger('urllib3').setLevel(logging.WARNING)

region = os.getenv('AWS_REGION', 'us-east-1')

# Optional Bedrock reranker model ID for the Phase-1 RAG Retrieve. When set
# (e.g. "cohere.rerank-v3-5:0"), retrieve_kb_context_structured over-fetches
# candidates and then reranks them down to the requested top_k, so the most
# query-relevant table docs survive instead of the raw vector-similarity order.
# Empty (the default) disables reranking entirely. The KB service role must hold
# bedrock:Rerank + bedrock:InvokeModel on this model (see bedrock-kb-stack.ts).
RERANK_MODEL_ID = os.getenv('RERANK_MODEL_ID', '').strip()

# How many candidates to over-fetch from the vector store BEFORE reranking. The
# reranker reorders this larger pool and returns the top numberOfRerankedResults
# (= the caller's top_k), so a wider pool gives the reranker more to choose from.
# Both numberOfResults values are capped at the Retrieve API maximum of 100.
RERANK_OVERFETCH = int(os.getenv('RERANK_OVERFETCH', '50'))

metadata_table_name = os.getenv('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
dynamodb = boto3.resource('dynamodb', region_name= region)
metadata_table = dynamodb.Table(metadata_table_name)


# Token management constants
MAX_TOKENS_PER_REQUEST = 150000


# In-process cache for retrieved KB context — keyed by ontology/namespace id.
# Lives for the lifetime of the warm runtime; mirrors _ontology_cache in
# ontology_query_agent.
_kb_cache: Dict[str, str] = {}


# Per-invocation context shared with @tool functions so they can write
# step-label updates to the query-results DDB row (the @tool schema cannot
# carry arbitrary kwargs through Strands).
_invocation_ctx: Dict[str, Optional[str]] = {
    'query_id': None,
    'query_results_table': None,
}


def _write_step(step: str) -> None:
    """Best-effort: write current_step to the query-results DDB row.

    Skipped silently when query_id/table are not set or when DDB writes fail.
    Step labels are diagnostic UX — they must never break a query.
    """
    qid = _invocation_ctx.get('query_id')
    tbl = _invocation_ctx.get('query_results_table')
    if not qid or not tbl:
        return
    try:
        table = dynamodb.Table(tbl)
        table.update_item(
            Key={'queryId': qid},
            UpdateExpression='SET current_step = :s, updated_at = :u',
            ExpressionAttributeValues={
                ':s': step,
                ':u': datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as e:
        logger.debug(f"step-label write failed for {qid} step={step}: {e}")  # nosec B110

# Per-invocation state storage.
# _session_id_var propagates to executor threads (asyncio.run_in_executor copies
# the current Context in Python 3.7+), so tools running in a worker thread and
# the invoke function share the same session_id and therefore the same state dict.
_session_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    'session_id', default=None
)

# Semantic-layer scope for the current invocation. The KB is shared across all
# layers, so retrieve_kb_context uses these to constrain results to the chunks
# tagged with the matching semantic_layer_id and semantic_layer_version (set
# by the metadata_agent in the .metadata.json sidecar at ingestion time).
_layer_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    'semantic_layer_id', default=None
)
_layer_version_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    'semantic_layer_version', default=None
)

_states: Dict[str, dict] = {}
_states_lock = threading.Lock()


def _get_state() -> dict:
    """Return the state dict for the current invocation session."""
    sid = _session_id_var.get()
    if not sid:
        # Fallback: return a throwaway dict (should not normally happen)
        return {'query_executed': False, 'cached_results': {}}
    with _states_lock:
        return _states.setdefault(sid, {
            'query_executed': False,
            'current_session': sid,
            'cached_results': {},
        })

# Global boto3 session for credential injection
_boto_session = None

def set_boto_session(session: boto3.Session):
    """
    Set the boto3 session to use for all AWS API calls.

    Args:
        session: Configured boto3.Session with desired credentials
    """
    global _boto_session
    _boto_session = session
    logger.info(f"Boto3 session set with region: {session.region_name}")

def get_boto_session() -> boto3.Session:
    """Get the configured boto3 session, or create a default one"""
    global _boto_session
    if _boto_session is None:
        _boto_session = boto3.Session()
    return _boto_session

def reset_agent_state(session_id: str = None):
    """Reset agent state for a new invocation."""
    _session_id_var.set(session_id)
    with _states_lock:
        _states[session_id] = {
            'query_executed': False,
            'current_session': session_id,
            'cached_results': {},
        }
        # Prune old sessions to avoid unbounded growth (keep last 20)
        if len(_states) > 20:
            oldest = next(iter(_states))
            del _states[oldest]
    logger.info(f"Agent state reset for session: {session_id}")

# ==============================================================================
# BEDROCK KB AND QUERY TOOLS
# ==============================================================================

def _bedrock_agent_runtime():
    """Return a bedrock-agent-runtime client. Indirected so tests can stub."""
    return get_boto_session().client('bedrock-agent-runtime', region_name=region)


def retrieve_kb_context_structured(*, user_query: str, kb_id: str,
                                    top_k: int = 20) -> Dict[str, Any]:
    """Phase-1 (RAG) retrieval — returns structured candidates + chunks-by-table.

    Output:
      ``{candidates: [{table_id, score, column_id?}],
         chunks: [str, ...],
         chunks_by_table: {table_id: markdown_body}}``.

    The ``table_id`` is built as ``"{database_name}.{table_name}"`` from the
    KB chunk metadata attributes that ``save_metadata_document_to_s3`` writes
    into the companion ``.metadata.json`` (so this matches one-doc-per-table
    ingestion). ``chunks_by_table`` lets the Phase-2 slice builder parse the
    markdown body of each candidate table without a second KB round-trip.

    Args:
        user_query: Natural-language query.
        kb_id: Bedrock Knowledge Base id.
        top_k: Maximum number of retrieval results.
    """
    # Scope retrieval to the current semantic layer + version. Without this
    # filter, tier-2 leaks chunks from other layers/versions into the slice
    # the agent shows the user (the same bug retrieve_kb_context guards
    # against). Layer/version are populated per-invocation by the entrypoint.
    layer_id = _layer_id_var.get()
    layer_version = _layer_version_var.get()
    if not layer_id or not layer_version:
        raise RuntimeError(
            "semantic_layer_id and semantic_layer_version must be set "
            "before calling retrieve_kb_context_structured"
        )
    # API hard limit: numberOfResults must be 1..100 (KnowledgeBaseVectorSearch
    # configuration). When reranking is on we over-fetch a wider candidate pool
    # for the reranker to reorder, then cut back to top_k via
    # numberOfRerankedResults below.
    number_of_results = min(max(RERANK_OVERFETCH, top_k), 100) if RERANK_MODEL_ID \
        else min(top_k, 100)
    vector_config: Dict[str, Any] = {
        'numberOfResults': number_of_results,
        'filter': {
            'andAll': [
                {'equals': {'key': 'semantic_layer_id', 'value': layer_id}},
                {'equals': {'key': 'semantic_layer_version', 'value': layer_version}},
            ]
        },
    }
    if RERANK_MODEL_ID:
        # Rerank the over-fetched pool with a Bedrock reranker model (e.g. Cohere
        # Rerank 3.5) and keep only the top_k most query-relevant chunks. The KB
        # service role performs the bedrock:Rerank call, so its IAM policy must
        # allow that action (wired in cdk bedrock-kb-stack.ts createKbRole).
        vector_config['rerankingConfiguration'] = {
            'type': 'BEDROCK_RERANKING_MODEL',
            'bedrockRerankingConfiguration': {
                'modelConfiguration': {
                    'modelArn': (
                        f'arn:aws:bedrock:{region}::foundation-model/{RERANK_MODEL_ID}'
                    ),
                },
                'numberOfRerankedResults': min(top_k, 100),
            },
        }
    client = _bedrock_agent_runtime()
    resp = client.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={'text': user_query},
        retrievalConfiguration={'vectorSearchConfiguration': vector_config},
    )
    candidates: List[Dict[str, Any]] = []
    chunks: List[str] = []
    chunks_by_table: Dict[str, str] = {}
    for r in resp.get('retrievalResults', []):
        meta = r.get('metadata', {}) or {}
        # Prefer an explicit table_id when the chunk carries one, otherwise
        # compose it from database_name + table_name (the current ingestion
        # shape from agents/metadata_agent/main.py).
        table_id = meta.get('table_id')
        if not table_id:
            db = meta.get('database_name') or meta.get('database') or ''
            tn = meta.get('table_name') or meta.get('table') or ''
            if db and tn:
                table_id = f"{db}.{tn}"
        score = r.get('score')
        body = r.get('content', {}).get('text', '')
        chunks.append(body)
        if table_id and score is not None:
            cand: Dict[str, Any] = {'table_id': table_id, 'score': score}
            if 'column_id' in meta:
                cand['column_id'] = meta['column_id']
            # Carry the Athena execution context so Phase 5 can run the query
            # against the correct catalog/database without a second KB call or
            # parsing it out of the table_id. catalog_id is REQUIRED for
            # federated catalogs (S3 Tables, etc.) — without it execution hits
            # SCHEMA_NOT_FOUND against the default AwsDataCatalog.
            catalog_id = (meta.get('catalog_id') or meta.get('catalog_name')
                          or meta.get('catalog') or '')
            database_name = (meta.get('database_name') or meta.get('database')
                             or (table_id.split('.', 1)[0] if '.' in table_id else ''))
            if catalog_id:
                cand['catalog_id'] = catalog_id
            if database_name:
                cand['database_name'] = database_name
            candidates.append(cand)
            # Keep the highest-scoring chunk per table_id (one doc per table
            # is the dominant case, but defend against the multi-chunk path).
            if table_id not in chunks_by_table:
                chunks_by_table[table_id] = body
    return {
        'candidates': candidates,
        'chunks': chunks,
        'chunks_by_table': chunks_by_table,
    }


@tool
def execute_sql_query(sql_query: str, database_name: str, catalog_id: str) -> str:
    """
    Generate and execute SQL query on Athena using KB context.

    Args:
        sql_query: SQL query to execute
        database_name: Athena database name to query against
        catalog_id: Athena catalog to use for querying

    Returns:
        JSON string with query results and context
    """
    state = _get_state()

    # Return cached result if already executed
    if state['query_executed'] and 'query_result' in state['cached_results']:
        logger.info("execute_sql_query already executed, returning cached result")
        return state['cached_results']['query_result']

    try:
        state['query_executed'] = True
        logger.info("=== execute_sql_query STARTED ===")
        logger.info(f"SQL query: {sql_query}")
        logger.info(f"Database: {database_name}")

        region = os.getenv('AWS_REGION', 'us-east-1')
        session = get_boto_session()
        athena_client = session.client('athena', region_name=region)

        # S3 bucket for query results
        try:
            session_ssm = get_boto_session()
            ssm_client = session_ssm.client('ssm', region_name=region)
            s3_bucket_param = f'/{os.getenv("PROJECT_NAME", "semantic-layer")}/athena/query-results-bucket'
            response = ssm_client.get_parameter(Name=s3_bucket_param, WithDecryption=True)
            s3_output_location = f"s3://{response['Parameter']['Value']}/metadata-query-results/"
        except:
            s3_bucket = os.getenv('ATHENA_RESULTS_BUCKET', f'{os.getenv("PROJECT_NAME", "semantic-layer")}-athena-results')
            s3_output_location = f"s3://{s3_bucket}/metadata-query-results/"

        logger.info(f"S3 output location: {s3_output_location}")

        # Build QueryExecutionContext based on catalog type:
        #
        # S3 Tables (s3tablescatalog/<bucket>) and other federated catalogs:
        #   Set QueryExecutionContext.Catalog = catalog_id
        #   SQL uses standard 2-part notation: "database"."table" (NO catalog prefix in SQL)
        #
        # Standard Glue (AWSDataCatalog / empty):
        #  No Catalog in context — Athena uses default AwsDataCatalog.
        if catalog_id and catalog_id not in ('AWSDataCatalog', 'AwsDataCatalog'):
            query_type = 'S3_TABLES_ICEBERG' if catalog_id.startswith('s3tablescatalog/') else f'FEDERATED ({catalog_id})'
            query_context: dict = {'Database': database_name, 'Catalog': catalog_id}
            logger.info(f"Query type    : {query_type} ")
        else:
            query_type = 'STANDARD_GLUE'
            query_context = {'Database': database_name}
            logger.info(f"Query type: {query_type} ")

        workgroup = os.getenv('ATHENA_WORKGROUP', 'primary')
        logger.info(f"QueryExecutionContext: {query_context}")
        logger.info(f"Workgroup     : {workgroup}")

        # Start + poll a single Athena execution. Returns
        # ('SUCCEEDED', execution_id, '') on success, ('FAILED', execution_id, reason)
        # on a query-level failure, or ('TIMED_OUT', execution_id, '') if it never
        # settled. A boto error from start_query_execution itself is raised as a
        # transient-or-not classification by the caller (it may be the connector
        # cold-starting before the query is even accepted).
        import time

        def _start_and_poll() -> tuple:
            resp = athena_client.start_query_execution(
                QueryString=sql_query,
                QueryExecutionContext=query_context,
                ResultConfiguration={'OutputLocation': s3_output_location},
                WorkGroup=workgroup,
            )
            qid = resp['QueryExecutionId']
            logger.info(f"Query submitted: execution_id={qid}")
            max_wait_time = 600
            wait_interval = 2
            elapsed = 0
            while elapsed < max_wait_time:
                ex = athena_client.get_query_execution(QueryExecutionId=qid)
                st = ex['QueryExecution']['Status']['State']
                if st == 'SUCCEEDED':
                    logger.info("Query succeeded")
                    return 'SUCCEEDED', qid, ''
                if st in ('FAILED', 'CANCELLED'):
                    reason = ex['QueryExecution']['Status'].get('StateChangeReason', 'Unknown error')
                    return 'FAILED', qid, reason
                time.sleep(wait_interval)  # nosemgrep: arbitrary-sleep — intentional polling interval for Athena query status
                elapsed += wait_interval
            return 'TIMED_OUT', qid, ''

        # Bounded deterministic retry for TRANSIENT connector failures (e.g. the
        # DynamoDB federated connector Lambda cold-starting:
        # 409 CodeArtifactUserPendingException / "Lambda is initializing"). These
        # are infrastructure errors the LLM cannot fix by rewriting SQL, so the tool
        # owns the retry. Deterministic SQL errors (SCHEMA/COLUMN_NOT_FOUND, syntax)
        # and the ProjectionExpression-size error are NOT retried — re-running the
        # identical query would just loop.
        from .athena_errors import is_transient_error, is_projection_size_error
        _MAX_ATTEMPTS = 3
        _BACKOFFS = [2, 5, 10]  # seconds before attempts 2 and 3 (index 0 unused)
        query_execution_id = ''
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                outcome, query_execution_id, error_msg = _start_and_poll()
            except Exception as start_err:  # noqa: BLE001 — start may fail on cold connector
                reason = str(start_err)
                if attempt < _MAX_ATTEMPTS and is_transient_error(reason):
                    delay = _BACKOFFS[min(attempt, len(_BACKOFFS) - 1)]
                    logger.info(f"execute_sql_query: transient start error "
                                f"(attempt {attempt}/{_MAX_ATTEMPTS}), retrying in "
                                f"{delay}s: {reason[:160]}")
                    time.sleep(delay)  # nosemgrep: arbitrary-sleep — bounded retry backoff
                    continue
                logger.error(f"Error starting SQL query: {reason}")
                return json.dumps({"error": str(start_err), "sql_query": sql_query,
                                   "database_name": database_name, "catalog_id": catalog_id})

            if outcome == 'SUCCEEDED':
                break
            if outcome == 'TIMED_OUT':
                return json.dumps({"error": "Query timed out", "query_execution_id": query_execution_id})

            # outcome == 'FAILED'
            if is_projection_size_error(error_msg):
                # SELECT * / COUNT(*) over a wide federated (DynamoDB) table exceeded
                # the connector's ProjectionExpression size limit. Retrying is futile;
                # surface an ACTIONABLE error so the agent narrows the projection.
                logger.error(f"Query failed (projection-size limit): {error_msg}")
                return json.dumps({
                    "error": ("Query failed: the table is a wide federated source and "
                              "SELECT */COUNT(*) exceeded its projection-size limit. "
                              "Re-issue selecting only the explicit columns needed "
                              "(use COUNT(<key column>) instead of COUNT(*))."),
                    "athena_error": error_msg,
                    "query_execution_id": query_execution_id,
                })
            if attempt < _MAX_ATTEMPTS and is_transient_error(error_msg):
                delay = _BACKOFFS[min(attempt, len(_BACKOFFS) - 1)]
                logger.info(f"execute_sql_query: transient query failure "
                            f"(attempt {attempt}/{_MAX_ATTEMPTS}), retrying in "
                            f"{delay}s: {error_msg[:160]}")
                time.sleep(delay)  # nosemgrep: arbitrary-sleep — bounded retry backoff
                continue
            # Deterministic failure (or retries exhausted) — surface it.
            logger.error(f"Query failed: {error_msg}")
            return json.dumps({"error": f"Query failed: {error_msg}", "query_execution_id": query_execution_id})
        else:
            # Loop exhausted without a SUCCEEDED break (all attempts were transient).
            return json.dumps({"error": "Query failed after retries: connector "
                               "repeatedly unavailable (transient).",
                               "query_execution_id": query_execution_id})

        # Get query results
        paginator = athena_client.get_paginator('get_query_results')
        page_iterator = paginator.paginate(QueryExecutionId=query_execution_id)

        results = {
            "query_execution_id": query_execution_id,
            "sql_query": sql_query,
            "database_name": database_name,
            "columns": [],
            "rows": []
        }

        first_page = True
        for page in page_iterator:
            for row in page['ResultSet']['Rows']:
                row_data = [col.get('VarCharValue', '') for col in row['Data']]

                if first_page and not results["columns"]:
                    # First row contains column names
                    results["columns"] = row_data
                    first_page = False
                    continue
                else:
                    results["rows"].append(row_data)

        logger.info(f"Query returned {len(results['rows'])} rows")

        final_result = json.dumps(results, indent=2)
        final_tokens = count_tokens(final_result)
        logger.info(f"=== execute_sql_query COMPLETED - {final_tokens} tokens ===")

        # Cache the result
        state['cached_results']['query_result'] = final_result

        return final_result

    except Exception as e:
        logger.error(f"Error executing SQL query: {str(e)}")
        return json.dumps({"error": str(e), "sql_query": sql_query, "database_name": database_name, "catalog_id": catalog_id})

def get_latest_metadata_item(id: str) -> Optional[dict]:
        """Return the metadata item with the highest version for the given id.

        Versions are stored as 'v1', 'v2', … The sort key is a string, so we
        fetch all versions and pick the one whose numeric suffix is largest
        (handles v10, v11, … correctly, unlike a raw lexicographic sort).
        """
        resp = metadata_table.query(
            KeyConditionExpression=Key('id').eq(id),
        )
        items = resp.get('Items', [])
        if not items:
            return None

        def _version_num(item: dict) -> int:
            try:
                return int(item.get('version', 'v0').lstrip('v'))
            except ValueError:
                return 0

        return max(items, key=_version_num)


def _catalog_database_for_layer(config: dict) -> tuple[str, str]:
    """Return the ``(catalog_id, database_name)`` a layer's tables live in.

    The Tier 1 governed-metric executor needs the SAME Athena catalog/database
    the Tier 2 path uses per-table (see execute_sql_query): for an S3 Tables
    layer the schema (e.g. ``normalized``) only resolves when the federated
    catalog is named in the QueryExecutionContext. A layer's tables are all
    ingested into one catalog+database, so the first ``dataSources`` entry is
    representative.

    Args:
        config: A metadata-layer config item (from get_latest_metadata_item).

    Returns:
        ``(catalog_id, database_name)``; ``("", "")`` when the config carries no
        dataSources (the executor then falls back to the default Glue catalog).
    """
    data_sources = config.get("dataSources") or []
    if not data_sources:
        return "", ""
    first = data_sources[0] or {}
    catalog_id = first.get("catalogId") or first.get("catalog_id") or ""
    database_name = first.get("databaseName") or first.get("database_name") or ""
    return catalog_id, database_name


# Live per-phase trace sink (phase, action, payload) -> None, installed by the
# AG-UI streaming runner so the Tier 2 graph's phase nodes can emit tier_event
# envelopes that reach the SSE stream immediately (not buffered to end-of-run).
# None on the request/response path → phase tracing is a no-op.
_STREAMING_PHASE_SINK = None


def _extract_usage_summary(response: Any) -> Dict[str, Any]:
    """Return a small Bedrock-shaped usage dict from a Strands ``AgentResult``.

    Mirrors ``ontology_query_agent._extract_usage_summary`` — both agents
    surface the same ``inputTokens/outputTokens/totalTokens`` trio (plus cache
    numbers when present) so the chat UI can render token counts uniformly.
    Returns an empty dict when metrics are unavailable.
    """
    try:
        usage = response.metrics.accumulated_usage
    except AttributeError:
        return {}
    out: Dict[str, Any] = {}
    for key in ('inputTokens', 'outputTokens', 'totalTokens'):
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        if value is not None:
            out[key] = int(value)
    for key in ('cacheReadInputTokens', 'cacheWriteInputTokens'):
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        if value:
            out[key] = int(value)
    return out


def _build_query_model() -> BedrockModel:
    """BedrockModel used by the Tier 2 graph's model phases (SQL generation in
    Phase 4 and the bounded Phase 5 execution agent).

    Prompt caching (``cache_config=auto``) caches each phase agent's stable
    prefix — its system prompt + tool definitions — across that agent's calls
    (Phase 5 execution + its bounded retries) and across queries within the warm
    runtime's 5-min cache window. Cache reads bill at ~0.1x input vs a 1.25x
    write, so a multi-call agent nets ~70-80% off the prefix's input cost. The
    ``auto`` strategy resolves to the ``anthropic`` cache strategy for our Claude
    model id and no-ops on unsupported models.
    """
    from strands.models.bedrock import CacheConfig
    return BedrockModel(
        model_id=QUERY_MODEL_ID,
        # NOTE: `temperature` is intentionally omitted — Sonnet 5 has adaptive
        # thinking ON by default and Bedrock rejects `temperature`/`top_p`/`top_k`
        # when thinking is active, surfacing as a ValidationException on
        # ConverseStream (same class of issue as Opus 4.8). max_tokens raised from
        # 4000: adaptive thinking tokens share the OUTPUT budget, so headroom is
        # needed to avoid stop_reason="max_tokens" before the SQL/answer emits.
        max_tokens=8000,
        boto_session=get_boto_session(),
        cache_config=CacheConfig(strategy="auto"),
    )


def _build_judge_model() -> BedrockModel:
    """BedrockModel used by the supervisor judge + decomposer (Sonnet 5).

    Judge emits a small structured-output decision (~200–500 tokens) per
    attempt — we keep it on the same Sonnet model as the worker to bound spend.
    """
    return BedrockModel(
        model_id=QUERY_MODEL_ID,
        # `temperature` omitted — Sonnet 5 adaptive thinking is on by default and
        # is incompatible with temperature (see _build_query_model). max_tokens
        # raised from 1500 so adaptive thinking tokens don't truncate the verdict.
        max_tokens=4000,
        boto_session=get_boto_session(),
    )


def _router_classify_fn(question: str) -> Dict[str, Any]:
    """Run the Haiku intent classifier and parse its JSON verdict.

    Used as the ``classify_fn`` injected into ``classify_intent`` for the
    model gray-zone (the regex fast-path handles obvious cases with no call).

    Calls Bedrock ``converse`` directly via boto3 — NOT through a Strands
    ``Agent`` — on purpose. A Strands ``Agent`` auto-emits a harvested
    ``strands.telemetry.tracer`` model-invoke span whose OUTPUT is this router's
    raw ``{"intent": ...}`` JSON. The AgentCore SESSION eval judges then see that
    intermediate routing JSON in the conversation ``{context}`` and can mistake
    it for the assistant's turn (e.g. scoring a correctly-clarified turn as "the
    agent returned JSON, not a clarifying question"). A bare ``converse`` call is
    not instrumented as a model span, so the routing JSON never enters telemetry.
    The router is also cheap/low-latency by design: no tools, no cache.

    :param question: The contextualized user question.
    :returns: ``{"intent": str, "confidence": float}`` parsed from the model,
        or ``{"intent": "data_query", "confidence": 0.0}`` on any parse failure
        (the caller's conservative default).
    """
    client = get_boto_session().client("bedrock-runtime")
    try:
        response = client.converse(
            modelId=ROUTER_MODEL_ID,
            system=[{"text": ROUTER_PROMPT}],
            messages=[{"role": "user", "content": [{"text": question}]}],
            inferenceConfig={"temperature": 0.0, "maxTokens": 100},
        )
        text = response["output"]["message"]["content"][0]["text"].strip()
    except Exception:  # noqa: BLE001 — fail-soft: any router error → conservative data_query
        return {"intent": "data_query", "confidence": 0.0}
    # Strip a ```json fence if the model wrapped its object.
    if text.startswith('```'):
        text = text.strip('`')
        if text.startswith('json'):
            text = text[4:]
    try:
        verdict = json.loads(text)
        return {
            "intent": verdict.get("intent", "data_query"),
            "confidence": float(verdict.get("confidence", 0.0)),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"intent": "data_query", "confidence": 0.0}


def _advisory_kb_retrieve(query: str, *, kb_id: str) -> str:
    """KB-retrieve callable for advisory answers (layer/version-scoped).

    Wraps the same ``retrieve_kb_context_structured`` the Tier 2 graph uses, so
    advisory grounds in the SAME scoped chunks. Returns a JSON string shaped
    ``{"context": [...]}`` (what ``build_advisory_answer`` expects).

    :param query: The retrieval query (the user's advisory question).
    :param kb_id: The Bedrock KB id for this layer.
    :returns: JSON string with a ``context`` list (empty on any retrieval error).
    """
    try:
        retrieved = retrieve_kb_context_structured(user_query=query, kb_id=kb_id)
        # retrieve_kb_context_structured returns {"chunks": [str, ...], ...};
        # the advisory parser expects {"context": [{"content": str}, ...]}.
        chunks = retrieved.get("chunks", []) if isinstance(retrieved, dict) else []
        return json.dumps({"context": [{"content": c} for c in chunks]})
    except Exception as exc:  # noqa: BLE001 — advisory treats empty KB as a known case
        logger.warning("advisory kb_retrieve failed (non-fatal): %s", exc)
        return json.dumps({"context": []})


def _advisory_synthesize(prompt: str) -> str:
    """Synthesis callable for advisory answers — one prose completion.

    Uses the query model (Sonnet 4.6, already granted) since the advisory module
    has already assembled the grounded context into ``prompt``.

    :param prompt: The fully-formed advisory prompt.
    :returns: The model's text answer ('' on an unexpected response shape).
    """
    agent = Agent(model=_build_query_model(),
                  system_prompt="You are a helpful semantic-layer advisor.")
    response = agent(prompt)
    try:
        return response.message['content'][0]['text']
    except (KeyError, IndexError, TypeError):
        return ''


# ----------------------------------------------------------------------------
# Tier 1 / Tier 2 / Tier 3 cascade — progressive disclosure (RAG mode)
# ----------------------------------------------------------------------------
def metrics_table():
    """Return the boto3 Table resource for the governed-metrics catalog."""
    return dynamodb.Table(os.environ.get("METRICS_TABLE", "semantic-layer-metrics"))


def _athena_client():
    """Return an Athena client honoring the injected boto session."""
    return get_boto_session().client("athena", region_name=region)


def _summarize_metric_rows(*, metric_id: str, columns: List[str],
                           rows: List[list]) -> str:
    """Build a natural-language answer from a governed-metric result.

    Mirror the VKG agent's `_summarize_select`: shape a useful sentence from the
    result itself (scalar / single record / multi-row), so the user sees the
    value(s) rather than a bare "Metric X returned N rows across M columns" count.

    Args:
        metric_id: The governed metric id (for the multi-row fallback label).
        columns: Result column names.
        rows: Result rows, each a positional list aligned to ``columns``.

    Returns:
        A concise plain-English answer string.
    """
    if not rows:
        return f"The governed metric '{metric_id}' returned no results."
    if len(rows) == 1:
        row = rows[0]
        # Scalar (1x1) — the dominant "what is the total/count" case.
        if len(columns) == 1 and len(row) == 1:
            return f"The result is {row[0]}."
        # Single record — render its fields as "column: value" pairs.
        pairs = ", ".join(
            f"{col}: {row[i] if i < len(row) else ''}"
            for i, col in enumerate(columns)
        )
        return f"The governed metric '{metric_id}' returned one result — {pairs}."
    return (
        f"The governed metric '{metric_id}' returned {len(rows)} rows across "
        f"{len(columns)} column(s). See the result table for details."
    )


def _metric_rows_to_positional(*, columns: List[str],
                               rows: List[Any]) -> List[list]:
    """Normalize execute_metric's dict-rows to positional lists for the UI.

    ``execute_metric`` returns rows as ``[{col: val}, ...]`` but the chat UI's
    results table (PhaseTimeline / ToolCallCard) renders rows POSITIONALLY
    (``row[idx]`` indexed by column order). Convert so the governed-metric result
    table renders the same as a Tier 2 execution result. A row that is already a
    list/tuple is passed through.

    Args:
        columns: Ordered column names.
        rows: Rows as dicts (or already-positional lists).

    Returns:
        Rows as positional lists aligned to ``columns``.
    """
    out: List[list] = []
    for r in rows:
        if isinstance(r, dict):
            out.append([r.get(c, "") for c in columns])
        elif isinstance(r, (list, tuple)):
            out.append(list(r))
        else:
            out.append([r])
    return out


def _build_response_from_metric(*, metric, result: Dict[str, Any],
                                id: str) -> Dict[str, Any]:
    """Shape a Tier 1 metric result into the same payload as a Tier 2/3 answer.

    Produces a real NL answer from the rows (not a bare row/column count) and
    exposes ``columns`` + positional ``results`` so the chat UI renders the SQL
    and result table the same way the semantic-layer (Tier 2) path does. The
    Tier 1 call site additionally emits phase-sink events carrying this SQL +
    columns + rows so the live "Show reasoning" panel renders them.
    """
    cols = result.get("columns", []) or []
    pos_rows = _metric_rows_to_positional(columns=cols, rows=result.get("rows", []))
    sql = getattr(metric, "compiled_sql", "")
    answer = _summarize_metric_rows(
        metric_id=metric.metric_id, columns=cols, rows=pos_rows,
    )
    return {
        "answer": answer,
        "sql_query": sql,
        # Positional rows (aligned to `columns`) so the UI table renders values,
        # not empty cells — matches the Tier 2 execution-result shape.
        "results": pos_rows,
        "columns": cols,
        "n_quads": [],
        "reasoning": {
            "interpretation": f"Tier 1 governed-metric match: {metric.metric_id}",
            "graphTraversal": _sql_entities_summary(sql) or "governed metric",
            "dataSourceSelection": "Athena (governed metric)",
            "sqlQuery": sql,
            "summarization": answer,
        },
        "metadata": {
            "tier": 1,
            "metric_id": metric.metric_id,
            "ontology_id": id,
        },
        # Uniform answer-source label (Tier 1 = governed metric). Threaded into
        # chat totals so the UI renders a "Governed Metric" badge.
        "provenance": build_provenance(
            tier="governed_metric",
            sources=[f"metric:{metric.metric_id}"],
        ),
    }


def _kb_id_for(namespace: str) -> str:
    """Map a namespace to its Bedrock KB id (env-driven)."""
    return os.environ.get("SEMANTIC_RAG_KB_ID", "")


def _sql_entities_summary(sql: str) -> str:
    """Return a 'tables: a, b · columns: x, y' summary of what the SQL touches.

    Parses the generated SQL with sqlglot (the same parser the grounding gate
    uses) and lists the referenced tables + columns. Used as the graph-traversal
    summary when term→table disambiguation produced no bindings, so the panel
    always reflects what the query actually traversed. Returns '' on parse
    failure (caller falls back to a generic label).
    """
    if not sql:
        return ""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="athena")
    except Exception:  # noqa: BLE001 — best-effort summary, never break the response
        return ""
    tables = sorted({t.name for t in tree.find_all(exp.Table) if t.name})
    columns = sorted({c.name for c in tree.find_all(exp.Column)
                      if c.name and c.name != "*"})
    parts = []
    if tables:
        parts.append("tables: " + ", ".join(tables[:8]))
    if columns:
        parts.append("columns: " + ", ".join(columns[:12]))
    return " · ".join(parts)


def _build_phase_deps(*, kb_id: str, recall_resolver=None) -> PhaseDeps:
    """Assemble the injected Tier 2 phase implementations for one resolution.

    Wires the RAG-mode router / slice builder / SQL generator that
    ``tier2_resolve`` uses, plus the Phase 5 bounded execution agent. There is
    no Glue lookup in this path, so RAG mode has zero runtime dependency on the
    Glue catalog (catalog_id still flows through to Phase 5 for Athena
    execution).

    Args:
        kb_id: Bedrock Knowledge Base id for Phase 1 retrieval.
        recall_resolver: Optional Phase 2 long-term-lessons resolver
            ``(term, candidate_table_ids) -> Optional[table_id]`` (see
            ``agents.shared.lessons_recall.build_recall_resolver``). ``None``
            disables memory-backed disambiguation.
    """
    router = RagTopicRouter(
        retrieve_fn=retrieve_kb_context_structured,
        kb_id_for=lambda ns: kb_id or _kb_id_for(ns),
        # top_k=20: tried 40 (whole 40-table namespace) to fix gt-04's "Missing:
        # party" — but it did NOT help (party still ranks below the effective
        # score_floor/min_candidates cutoff for a payout-phrased question, slice
        # still capped at ~16 sources without party) AND it regressed other rows by
        # widening the candidate pool (0.75→0.62). gt-04 is a hard semantic-retrieval
        # miss (party is distant from the question text), not a top_k breadth issue.
        # Reverted to 20.
        top_k=20,
    )
    judge = build_slice_judge(
        model_factory=_build_judge_model,
        system_prompt=JUDGE_PROMPT,
    )
    builder = RagSliceBuilder(
        chunks_lookup=router.chunks_for,
        judge_fn=judge,
        token_counter=count_tokens,
        budget=SLICE_TOKEN_BUDGET,
    )
    generator = RagQueryGenerator(
        agent_factory=lambda: Agent(
            model=_build_query_model(),
            system_prompt="Emit only an Athena SQL SELECT query.",
            tools=[],
        ),
        dialect="athena",
    )

    def _run_execution(sql: str, database_name: str, catalog_id: str,
                       slice_text: Optional[str] = None) -> Dict[str, Any]:
        """Phase 5 execution: ground-checked SQL → bounded agent → result dict.

        Enforces the LIMIT 100 / over-limit contract around the bounded
        execution agent (its only tool is ``execute_sql_query``), then returns
        the parsed Athena result the workflow stores on the context.

        ``slice_text`` is the Phase-3 retrieved schema slice the SQL was grounded
        in; it is forwarded into the execution prompt so it lands in the
        ``execute_sql_query`` span for the SESSION-level ``SqlGrounded`` judge
        (see ``run_execution``). It does not affect execution behaviour.
        """
        limited_sql, injected = ensure_limit(sql)
        agent = build_execution_agent(
            model_factory=_build_query_model,
            execute_tool=execute_sql_query,
            system_prompt=EXECUTION_PROMPT,
        )
        run_out = run_execution(
            agent=agent, sql=limited_sql,
            database_name=database_name, catalog_id=catalog_id,
            slice_text=slice_text,
        )
        # The execute_sql_query tool caches its parsed result in per-invocation
        # state; read it back and apply over-limit trimming/flagging.
        cached = _get_state().get('cached_results', {})
        try:
            result = json.loads(cached.get('query_result', '{}') or '{}')
        except (json.JSONDecodeError, TypeError):
            result = {}
        result = apply_over_limit(result, injected=injected)
        # Carry the agent's plain-English answer + token usage so Phase 5 can
        # surface them (the parsed Athena rows alone have no prose answer).
        result['answer'] = run_out.get('answer', '')
        result['usage'] = run_out.get('usage', {})
        return result

    return PhaseDeps(
        router=router, builder=builder, generator=generator,
        run_execution=_run_execution, recall_resolver=recall_resolver,
    )


def tier2_resolve(question: str, namespace: str, kb_id: str = "",
                  phase_sink=None, clarification_resolution=None,
                  recall_resolver=None,
                  prior_clarification_options=None,
                  prior_clarification_terms=None) -> WorkflowContext:
    """Run the Tier 2 RAG resolution graph (Phase 1→5) against the namespace.

    Args:
        question: Natural-language user question.
        namespace: Semantic-layer namespace for KB scoping.
        kb_id: Bedrock Knowledge Base id.
        phase_sink: Optional live per-phase trace sink (streaming path).
        clarification_resolution: A
            :class:`agents.shared.clarification.ClarificationResolution` when
            this turn answers a prior clarification; Phase 1 prunes the rival
            candidate tables it names. ``None`` on a normal turn.
        recall_resolver: Optional Phase 2 long-term-lessons resolver; ``None``
            disables memory-backed disambiguation.
        prior_clarification_options: The ``[{id, label}]`` options shown on the
            prior turn's clarification, passed ONLY when re-asking the SAME
            question, so a low-confidence re-ask keeps a stable option set.
        prior_clarification_terms: The ambiguous term(s) the prior clarification
            was about (paired with ``prior_clarification_options``).
    """
    deps = _build_phase_deps(kb_id=kb_id, recall_resolver=recall_resolver)
    return tier2_rag_workflow(
        question=question, namespace=namespace, kb_id=kb_id,
        deps=deps, phase_sink=phase_sink,
        clarification_resolution=clarification_resolution,
        prior_clarification_options=prior_clarification_options,
        prior_clarification_terms=prior_clarification_terms,
    )


def _run_query_core(payload: Dict[str, Any], context=None) -> Dict[str, Any]:
    """Resolve one question through the Tier 1 metric lookup → Tier 2 graph cascade.

    This is the request/response entrypoint behind ``invoke`` (MCP tools, direct
    runtime invocation); the AG-UI chat stream below also wraps it. Mirrors
    ``ontology_query_agent._run_query_core`` so both query agents share the same
    cascade shape.

    ``_run_query`` wraps this to persist the resolved turn into AgentCore Memory;
    callers (chat stream, MCP entrypoint, tests) go through that wrapper.
    """
    try:
        import time
        import uuid
        # Wall-clock runtime — surfaced in run_finished.totals.runtimeMs.
        run_started_at = time.monotonic()
        # Prefer the REST-API session id so AgentCore Memory partitions per
        # chat session and follow-ups see prior turns.
        chat_session_id = payload.get('sessionId') or ''
        session_id = chat_session_id or str(uuid.uuid4())[:8]
        # Attach the new Context returned by set_baggage so "session.id" is actually
        # present on the active context (set_baggage alone does not mutate it).
        if _otel_baggage and _otel_context:
            _otel_context.attach(_otel_baggage.set_baggage(
                "session.id", context.session_id if hasattr(context, "session_id") else session_id))
        reset_agent_state(session_id)
        # Scope the lazy prior-results lookup to this chat session so the
        # tool can fetch DDB-stored rows without exposing sessionId to the
        # model.
        _set_prior_results_session_id(chat_session_id)

        # Trusted caller identity for ownership checks + memory scoping. Derive
        # from the platform-validated JWT (Cognito sub) on EVERY path, not just
        # the chat stream — the direct invoke path must NOT trust a request-body
        # `userId`, or the session-ownership guard on the history read below
        # becomes a no-op (both sides attacker-controlled). _user_id_from_context
        # falls back to payload['userId'] only when no Bearer token is present.
        trusted_user_id = _user_id_from_context(context, payload) or 'anonymous'

        question = payload.get('question', '')
        if not question:
            return {'error': 'question is required in payload'}

        # Clarification resolution. If the PREVIOUS assistant turn was a
        # clarification ("Which interpretation of 'party'?"), this turn's message
        # is the user's selection. Match it to one offered option; on a single
        # match, re-run the ORIGINAL standalone question (not the bare reply) and
        # carry a resolution that Phase 1 uses to prune the rival candidates so
        # Phase 2 does not re-fire the identical clarification. Fail-soft: no
        # pending clarification / no unique match → resolution is None and the
        # turn proceeds through normal contextualization. See
        # agents/shared/clarification.py.
        clarification_resolution = None
        # When the user's reply does NOT cleanly resolve a pending clarification
        # and we end up re-asking the SAME low-confidence question, reuse the
        # options the user already saw instead of re-deriving a fresh,
        # non-deterministic top-5 (the "different 5 each turn" churn). Populated
        # only when the pending record's original_question matches this turn's.
        prior_clarification_options: list = []
        prior_clarification_terms: list = []
        _pending = None
        _history = []  # prior session turns; reused for the eval answer-span context
        if chat_session_id:
            try:
                # Enforce ownership on the history read: a forged sessionId from
                # a valid JWT holder must not leak another user's conversation
                # context. user_id is JWT-derived on the chat path; a mismatch
                # yields [] (logged) rather than the victim's transcript.
                _history = ChatSessionService().history_window(
                    session_id=chat_session_id, n=10,
                    user_id=trusted_user_id,
                )
                _pending = load_pending_clarification(_history)
                clarification_resolution = resolve_clarification_reply(
                    reply=question, pending=_pending,
                )
            except Exception as exc:  # noqa: BLE001 — resolution is best-effort
                logger.warning("clarification resolve failed (non-fatal) "
                               "session=%s: %s", chat_session_id, exc)

        if clarification_resolution is not None:
            # Re-run the original query; skip contextualization (the standalone
            # question is already known).
            logger.info("Session %s — clarification resolved: reply=%r -> "
                        "rerun %r (chose %s)", session_id, question,
                        clarification_resolution.original_question,
                        clarification_resolution.chosen_ids)
            question = clarification_resolution.original_question
            # Weave the chosen entity INTO the re-run question when the original
            # is underspecified. "How many are there?" + chose `party` re-runs as
            # the bare text, which has no subject — Phase 1 retrieval is noun-less
            # and the Phase 3 judge correctly rejects ("question does not specify
            # what to count"), so seeding `party` into the candidates is not
            # enough. Append the chosen entity so the question states what it is
            # about. Conservative: only when the chosen name is not ALREADY in the
            # question (an explicit "How many parties…?" is left untouched).
            try:
                from agents.shared.clarification import local_name as _ln
            except ImportError:
                from shared.clarification import local_name as _ln  # type: ignore
            _chosen = [
                _ln(c) for c in (clarification_resolution.chosen_ids or [])
            ]
            _qlow = (question or "").lower()
            _absent = [c for c in _chosen if c and c not in _qlow]
            if _absent:
                _subject = ", ".join(_absent).replace("_", " ")
                question = f"{question.rstrip(' ?')} (for {_subject})?"
                logger.info("Session %s — clarified question augmented with "
                            "chosen subject -> %r", session_id, question)
        elif isinstance(_pending, dict) and (_pending.get("original_question") or "").strip():
            # The previous turn WAS a clarification but this reply did not uniquely
            # resolve it (e.g. "not sure", or an option label the resolver could
            # not match). Do NOT hand the bare reply to the free-form follow-up
            # rewriter — it hallucinates (observed: reply "party" → "How many
            # political parties are there?"). Instead deterministically rebuild a
            # standalone question from the pending original PLUS the reply as the
            # intended subject, when the reply looks like a content pick (a short
            # noun, not a meta-reply like "not sure"/"idk"). Short meta-replies
            # fall through to re-asking with the carried-forward options below.
            pending_q = (_pending.get("original_question") or "").strip()
            reply = (question or "").strip()
            _meta = {"not sure", "unsure", "idk", "i don't know", "i dont know",
                     "no", "neither", "none", "dunno", "?"}
            if reply and reply.lower() not in _meta and len(reply.split()) <= 4 \
                    and reply.lower() not in pending_q.lower():
                question = f"{pending_q.rstrip(' ?')} (for {reply})?"
                logger.info("Session %s — pending clarification reply %r folded "
                            "into pending question -> %r (no free-form rewrite)",
                            session_id, reply, question)
            else:
                # Meta/unmatched reply: keep the original pending question so the
                # node re-asks (with the carried-forward stable options below)
                # rather than rewriting the bare reply.
                question = pending_q
        else:
            # Follow-up contextualization. The Tier 2 graph resolves a single
            # standalone question (Phase 1 embeds it into KB retrieval, Phase 2
            # tokenizes it), so a follow-up like "again, how many are there?"
            # would reach the topic router with no antecedent. Rewrite it into a
            # self-contained question using this session's history BEFORE
            # Tier 1/2. Fail-soft: returns the original question on a first turn,
            # a non-follow-up, or any error. See agents/shared/followup.py.
            contextualized = contextualize_question(
                question=question,
                session_id=chat_session_id,
                model_factory=_build_query_model,
                user_id=trusted_user_id,
            )
            if contextualized.changed:
                logger.info(
                    "Session %s — follow-up rewritten: %r -> %r",
                    session_id, contextualized.original, contextualized.rewritten,
                )
            question = contextualized.rewritten

            # The reply did NOT resolve the pending clarification (no unique
            # option match). If it's a re-ask of the SAME question, carry the
            # options the user already saw so Phase 2 re-asks with a STABLE set
            # rather than a fresh non-deterministic top-5 (session 4c8a50c7
            # churn). Gate on original_question equality so a genuinely new
            # question still gets fresh options.
            if isinstance(_pending, dict):
                pending_q = (_pending.get("original_question") or "").strip()
                if pending_q and pending_q == (question or "").strip():
                    prior_clarification_options = _pending.get("options") or []
                    prior_clarification_terms = _pending.get("terms") or []

        # Capture per-invocation context so @tool functions can write step
        # labels to the query-results DDB row. Populated by the lambda async
        # worker (agentcore_service.py); absent on the chat path.
        _invocation_ctx['query_id'] = payload.get('query_id') or None
        _invocation_ctx['query_results_table'] = (
            payload.get('query_results_table') or None
        )

        id = payload.get('id', '')
        config = get_latest_metadata_item(id)
        if not config:
            raise ValueError(f"metadata config not found: {id}")

        # Scope KB retrieval to this semantic layer + its active version so
        # retrieve_kb_context cannot see chunks from other layers / versions
        # in the shared SemanticRAG KB.
        active_version = config.get('version', '')
        if not active_version:
            raise ValueError(
                f"metadata config {id} has no 'version' attribute — cannot scope KB retrieval"
            )
        _layer_id_var.set(id)
        _layer_version_var.set(active_version)

        user_id = trusted_user_id
        namespace = config.get('namespace') or id
        kb_id = config.get('kbId') or os.environ.get('SEMANTIC_RAG_KB_ID', '')
        hint = f"[ontology: {id}]"

        # When this turn RESOLVED a prior clarification, persist a crisp
        # "<term> → <chosen>" mapping lesson so a later session recalls the
        # binding (see _persist_mapping_lesson_from_resolution / lessons_recall).
        # Best-effort: never blocks the query.
        if clarification_resolution is not None:
            _persist_mapping_lesson_from_resolution(
                resolution=clarification_resolution,
                semantic_layer_id=id,
                semantic_layer_version=active_version,
                user_id=user_id,
                session_id=chat_session_id,
            )

        # --- Intent router: advisory questions never enter the SQL cascade ----
        # Classify the contextualized question; an "advisory" verdict (regex
        # fast-path, or Haiku above the confidence floor) is answered in-process
        # from KB metadata + governed metrics. Fail-soft: any error falls through
        # to the unchanged data path, so the router can only ADD a route, never
        # break an answerable query.
        try:
            intent = classify_intent(
                question=question, classify_fn=_router_classify_fn,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("intent classify error (%s) — defaulting to data_query", e)
            intent = {"intent": "data_query", "confidence": 0.0}
        if intent.get("intent") == "advisory":
            try:
                _write_step("advisory_route")
                advisory = build_advisory_answer(
                    question=question,
                    layer_id=id,
                    kb_retrieve=lambda q: _advisory_kb_retrieve(q, kb_id=kb_id),
                    metrics_table=metrics_table(),
                    synthesize=_advisory_synthesize,
                    layer_name=config.get('name') or id,
                )
                metric_sources = [
                    f"metric:{m['metric_id']}" for m in advisory.get("metrics", [])
                ]
                return {
                    "answer": advisory.get("answer", ""),
                    "sql_query": "",
                    "executed_sql": "",
                    "executed": False,
                    "results": [],
                    "n_quads": [],
                    "reasoning": {
                        "interpretation": "classified as meta-question → advisory",
                        "graphTraversal": "",
                        "dataSourceSelection": "Advisory (KB metadata + governed metrics)",
                        "sqlQuery": "",
                        "summarization": advisory.get("answer", ""),
                    },
                    "metadata": {"runtimeMs": 0, "usage": {}},
                    "provenance": build_provenance(
                        tier="advisory",
                        sources=["kb"] + metric_sources,
                    ),
                }
            except Exception as e:  # noqa: BLE001 — never hard-fail; fall through
                logger.warning(
                    "advisory build failed (%s) — falling through to data path", e)

        # --- Tier 1: governed-metric lookup -----------------------------------
        try:
            metric = tier1_lookup(
                question=question, namespace=namespace,
                ddb_table=metrics_table(), knn=knn_index,
                knn_endpoint="",
                knn_index="metrics",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("tier1_lookup error (%s) — falling through", e)
            metric = None
        if metric is not None:
            _write_step("tier1_metric_hit")
            try:
                # Resolve the layer's Athena catalog/database so the metric's
                # compiled SQL (e.g. FROM normalized.holding) resolves against
                # the S3 Tables federated catalog rather than the default Glue
                # catalog (which has no `normalized` schema → SCHEMA_NOT_FOUND).
                metric_catalog_id, metric_database = _catalog_database_for_layer(config)
                result = tier1_execute(
                    metric=metric, filters={},
                    athena=_athena_client(),
                    workgroup=os.environ.get("ATHENA_WORKGROUP", ""),
                    output_loc=os.environ.get("ATHENA_OUTPUT_LOCATION", ""),
                    catalog_id=metric_catalog_id,
                    database_name=metric_database,
                )
                response = _build_response_from_metric(
                    metric=metric, result=result, id=id,
                )
                # Emit a live phase event so the chat "Show reasoning" panel
                # renders the metric's SQL + result table — the UI only shows
                # those INSIDE the reasoning panel, which needs at least one
                # phase/tool event. Tier 1 short-circuits before the Tier 2 graph
                # (the only other phase-event source), so without this the
                # governed-metric answer showed no SQL and no results (matching
                # the Phase-5 execution-result payload shape the panel renders).
                # Best-effort: a sink error must never break the answer.
                sink = _STREAMING_PHASE_SINK
                if sink is not None:
                    try:
                        sink(5, "phase_start", {"step": "metric"})
                        sink(5, "phase_result", {
                            "step": "metric",
                            "grounded": True,
                            "metricId": metric.metric_id,
                            "sql_query": response["sql_query"],
                            "columns": response["columns"],
                            "rows": response["results"],
                            "rowCount": len(response["results"]),
                        })
                    except Exception as exc:  # noqa: BLE001 — telemetry only
                        logger.warning("tier1 phase emit failed (non-fatal): %s", exc)
                return response
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "tier1_execute failed (%s) — falling through to Tier 2", e,
                )

        # --- Tier 2: Strands graph workflow (Phase 1 → 5) -------------------
        # Fail-soft: any unexpected workflow error degrades to a plain error
        # answer, never a 5xx. The graph itself routes recoverable conditions
        # (empty candidates, sql-repair-failed, grounding-unresolved) to its
        # degraded terminal and records the reason on the context.
        phase_sink = _STREAMING_PHASE_SINK
        # Phase-2 memory recall: a resolver that consults THIS user's long-term
        # lessons (scoped to this layer + version) to settle a term that is
        # ambiguous on the current retrieval but was resolved in a prior session.
        # ``None`` when LESSONS_MEMORY_ID is unset — recall stays off.
        recall_resolver = build_recall_resolver(
            memory_id=os.environ.get('LESSONS_MEMORY_ID', ''),
            semantic_layer_id=id,
            semantic_layer_version=active_version,
            user_id=user_id,
            region=region,
        )
        try:
            wf: WorkflowContext = tier2_resolve(
                question, namespace, kb_id=kb_id, phase_sink=phase_sink,
                clarification_resolution=clarification_resolution,
                recall_resolver=recall_resolver,
                prior_clarification_options=prior_clarification_options,
                prior_clarification_terms=prior_clarification_terms,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("tier2 workflow error")
            return {'error': f'Agent execution failed: {str(e)}'}

        _write_step('summarizing')
        runtime_ms = int((time.monotonic() - run_started_at) * 1000)

        # Clarification short-circuit — Phase 2 / 3b produced a needs_clarification
        # payload. Surface it in the same shape the frontend already handles, and
        # attach a pending-clarification record (the standalone question + the
        # offered options) so the NEXT turn can resolve the user's selection.
        # The chat layer persists this record into the assistant turn's totals.
        if wf.needs_clarification is not None:
            payload = dict(wf.needs_clarification)
            payload.setdefault('answer', payload.get('clarification_question', ''))
            payload['n_quads'] = wf.kb_sources
            payload['metadata'] = {'runtimeMs': runtime_ms, 'usage': wf.usage}
            # Carry forward every ambiguity resolved earlier in this chain plus the
            # one this turn just resolved, so the NEXT rerun re-prunes all of them
            # and a multi-ambiguity question converges (see ResolvedChoice). On a
            # first clarification (no resolution this turn) ``prior`` is empty.
            prior = accumulate_prior(clarification_resolution)
            payload['clarification'] = build_pending_clarification(
                original_question=question, payload=wf.needs_clarification,
                prior=prior,
            )
            # Eval-only telemetry: a Phase-2 disambiguation clarification is
            # deterministic (no model call), so when this turn also skipped the
            # follow-up rewrite the SDK emits NO harvested span and the SESSION
            # judges either fail with "no spans to evaluate" or grade an
            # intermediate span (the rewrite). Emit a span carrying the actual
            # clarification question + offered options so the judges grade what
            # the user saw. Fail-soft inside the helper — never breaks the turn.
            emit_answer_span(
                question=question,
                answer=payload.get('clarification_question', '') or payload.get('answer', ''),
                options=payload.get('options'),
                operation_label='clarification',
                conversation_history=_history,
            )
            return payload

        exec_result = wf.execution_result or {}
        columns: list = exec_result.get('columns', [])
        rows: list = exec_result.get('rows', [])
        results_list: list = [
            {col: (row[i] if i < len(row) else '') for i, col in enumerate(columns)}
            for row in rows
        ]
        # ``wf.sql`` is the LAST-GENERATED SQL — on a degraded run (e.g.
        # grounding_unresolved) it is the gate-rejected query that NEVER ran. To
        # keep the response honest we expose two distinct fields:
        #   * ``sql_query``     — the generated SQL (back-compat; UI/reasoning).
        #   * ``executed_sql``  — the SQL that ACTUALLY executed against Athena,
        #                         or "" when the run degraded before execution.
        # ``executed`` makes the distinction unambiguous for the eval harness so
        # a clean degrade is never scored as if a hallucinated query had run.
        sql_query: str = wf.sql or exec_result.get('sql_query', '')
        executed: bool = bool(wf.execution_result) and wf.degraded is None
        executed_sql: str = exec_result.get('sql_query', '') if executed else ''
        execution_id = exec_result.get('query_execution_id', '')
        data_source = f"Athena execution: {execution_id}" if execution_id else "Athena"

        # Answer text: prefer the execution agent's prose; on a degraded path
        # explain what happened without a 5xx.
        if wf.degraded == "phase1_empty":
            result_text = (
                "I couldn't find any tables in the knowledge base relevant to "
                "your question."
            )
        elif wf.degraded == "phase3_max_rounds":
            # Prefer the specific gap the judge identified (set in the Phase 3
            # loop) — it names the missing column/lookup so the user learns the
            # data isn't modelled, rather than a generic "narrow your question".
            result_text = getattr(wf, "degraded_detail", None) or (
                "I found relevant tables but couldn't assemble a complete enough "
                "schema slice to answer your question reliably. Try narrowing the "
                "question or asking about fewer columns at once."
            )
        elif wf.degraded == "relationship_unsupported":
            # The Phase 3b guard set a specific user-facing detail; fall back to a
            # generic message if it is somehow empty.
            result_text = getattr(wf, "degraded_detail", None) or (
                "This question requires comparing party roles on a policy (e.g. the "
                "insured vs the policyholder), which the current data model does not "
                "record, so it can't be answered with the available schema."
            )
        elif wf.degraded == "sql_repair_failed":
            result_text = (
                "I was unable to construct a valid SQL query for your question."
            )
        elif wf.degraded == "grounding_unresolved":
            result_text = (
                "I couldn't build a query fully grounded in the available "
                "schema for your question."
            )
        else:
            result_text = exec_result.get('answer', '') or (
                f"Query returned {len(rows)} row(s) across {len(columns)} column(s)."
            )

        over_limit = bool(exec_result.get('over_limit'))
        summarization = (
            f"Query returned {len(rows)} row(s) across {len(columns)} column(s)"
            + (" (truncated to first 100)" if over_limit else "")
        )
        # Graph-traversal summary: prefer the resolved term→table bindings;
        # otherwise fall back to the tables/columns the generated SQL actually
        # referenced (always populated when SQL ran), so the panel is never a
        # bare "mappings applied".
        if wf.disambiguation:
            traversal_parts = []
            for term, info in wf.disambiguation.items():
                tgt = (info.get('table') if isinstance(info, dict) else str(info))
                traversal_parts.append(f"{term} → {tgt}" if tgt else term)
            disambig_summary = ', '.join(traversal_parts)
        else:
            disambig_summary = (
                _sql_entities_summary(sql_query) or 'KB metadata mappings applied'
            )

        logger.info(
            f"Session {session_id} — structured response: sql={bool(sql_query)}, "
            f"rows={len(rows)}, kb_sources={len(wf.kb_sources)}, "
            f"degraded={wf.degraded}, grounding_rounds={wf.grounding_rounds}"
        )

        # Eval-only telemetry: emit a span carrying the FINAL natural-language
        # answer the user received. The deterministic graph's last model span is
        # often an intermediate phase (the follow-up rewrite, or a SliceSufficiency
        # tool result like {"sufficient":false,...} on a degraded run), which the
        # SESSION judges would otherwise mistake for the assistant's answer. This
        # span lands last in the turn so the judges grade the real answer.
        # Fail-soft inside the helper — never breaks the response.
        emit_answer_span(
            question=question,
            answer=result_text,
            operation_label='final_answer',
            conversation_history=_history,
            # Carry the Phase-3 schema slice so the SqlGrounded judge can verify the
            # executed SQL against it from this (now turn-anchoring) invoke_agent span.
            retrieved_schema=getattr(wf, 'slice_text', None),
        )

        # Provenance sources: the slice tables resolved by the Tier 2 graph
        # (``wf.candidates`` are ``database.table`` ids). On a degrade before any
        # candidate survived, fall back to ``["kb"]`` — never run a fresh query
        # just to populate this. The local table name is enough for the badge.
        prov_sources = (
            [f"table:{c.split('.')[-1]}" for c in wf.candidates]
            if wf.candidates
            else ["kb"]
        )

        return {
            "answer": result_text,
            "sql_query": sql_query,
            # Executed-vs-generated distinction (see above): ``executed_sql`` is
            # "" on a degraded run, ``executed`` is False, and ``degraded`` names
            # the terminal reason. The eval harness scores ``executed_sql`` /
            # ``executed`` so a gate-rejected query is not judged as if it ran.
            "executed_sql": executed_sql,
            "executed": executed,
            "degraded": wf.degraded,
            "grounding_rounds": wf.grounding_rounds,
            "results": results_list,
            "n_quads": wf.kb_sources,
            "reasoning": {
                "interpretation": f"Resolved {len(wf.candidates)} candidate table(s) via KB retrieval",
                "graphTraversal": disambig_summary,
                "dataSourceSelection": data_source,
                # Only surface SQL in the reasoning trace when it ACTUALLY ran.
                # On a degrade ``sql_query`` is the gate-rejected query; emitting
                # it here would leak it into the OTEL span context where the
                # SqlGrounded judge mistakes it for executed SQL (the eval-fidelity
                # bug). Empty string on degrade keeps the trace honest.
                "sqlQuery": executed_sql,
                "summarization": summarization,
            },
            "metadata": {
                "executionTimeMs": 0,
                "dataScannedBytes": 0,
                "runtimeMs": runtime_ms,
                "overLimit": over_limit,
                "usage": wf.usage,
            },
            # Uniform answer-source label (Tier 2 = semantic SQL). ``degraded``
            # mirrors the terminal reason so the badge can flag a degraded run.
            "provenance": build_provenance(
                tier="semantic_sql",
                sources=prov_sources,
                degraded=wf.degraded,
            ),
        }

    except Exception as e:
        logger.error(f"Error in invoke: {str(e)}")
        return {'error': f'Agent execution failed: {str(e)}'}
    finally:
        # Always clear per-invocation context so a stale query_id from this
        # run can't leak into the next one (warm-runtime scenario).
        _invocation_ctx['query_id'] = None
        _invocation_ctx['query_results_table'] = None


# ----------------------------------------------------------------------------
# AgentCore Memory — lessons-learned turn persistence (item #2)
# ----------------------------------------------------------------------------
# Fail-closed, keyword-only guardrail shim (distinct from the chat ``_guardrails``
# singleton below, which is fail-OPEN/positional for INPUT/OUTPUT screening). The
# memory writer must never persist un-redacted PII, so it uses the shim that
# returns action='ERROR' on failure → the turn is dropped.
try:
    from agents.shared.guardrail_service_shim import (  # type: ignore
        GuardrailService as _MemoryGuardrailService,
    )
    from agents.shared.memory_hooks import (  # type: ignore
        persist_mapping_lesson,
        persist_turn_pair,
    )
    from agents.shared.lessons_recall import build_recall_resolver  # type: ignore
except ImportError:  # container path: agents/ is on PYTHONPATH
    from shared.guardrail_service_shim import (  # type: ignore
        GuardrailService as _MemoryGuardrailService,
    )
    from shared.memory_hooks import (  # type: ignore
        persist_mapping_lesson,
        persist_turn_pair,
    )
    from shared.lessons_recall import build_recall_resolver  # type: ignore

_memory_guardrail = _MemoryGuardrailService()


def _persist_lessons_turn(payload: Dict[str, Any], result: Dict[str, Any]) -> None:
    """Best-effort: persist this turn's (question, answer) into AgentCore Memory.

    The query agent runs a deterministic Tier 2 graph (no conversational Strands
    ``Agent``), so there is no ``MessageAddedEvent`` for ``LessonsMemoryHooks`` to
    observe. Instead we feed the resolved turn into the same guarded write path
    here. AgentCore's ``SemanticStrategy`` consolidates the long-term records
    asynchronously on the service side.

    The memory's strategy template is ``/lessons/{actorId}/{sessionId}/``. We
    encode ``actorId`` as ``"<semanticLayerId>/<semanticLayerVersion>/<userId>"``
    (slashes are valid in an actorId) so the resolved long-term namespace is
    ``/lessons/<semanticLayerId>/<semanticLayerVersion>/<userId>/<sessionId>/`` —
    scoping lessons per layer, per layer-version, per user, per chat session.
    Pinning the version keeps lessons learned against a prior schema version from
    leaking into a re-modelled layer.

    No-op when ``LESSONS_MEMORY_ID`` is unset or the result is an error/empty —
    and never raises (a memory failure must not affect the user's reply).

    Args:
        payload: The request payload (carries ``question``/``message``, ``id``/
            ``ontologyId``, ``sessionId``, ``userId``).
        result: The structured answer dict returned by ``_run_query_core``.
    """
    memory_id = os.environ.get('LESSONS_MEMORY_ID', '')
    if not memory_id or not isinstance(result, dict) or result.get('error'):
        return
    question = payload.get('question') or payload.get('message') or ''
    answer = result.get('answer') or ''
    if not question or not answer:
        return
    semantic_layer_id = payload.get('id') or payload.get('ontologyId') or ''
    # Active layer version resolved by _run_query_core (stashed per-invocation in
    # _layer_version_var) so the namespace pins the exact schema version the
    # answer was grounded in.
    semantic_layer_version = _layer_version_var.get() or ''
    user_id = payload.get('userId') or 'anonymous'
    session_id = payload.get('sessionId') or ''
    if not semantic_layer_id or not semantic_layer_version or not session_id:
        # Without a layer id + version + session we can't form the per-scope
        # namespace (/lessons/<layerId>/<layerVersion>/<userId>/<sessionId>/) —
        # skip rather than pollute a fallback namespace.
        return
    persist_turn_pair(
        memory_id=memory_id,
        actor_id=f"{semantic_layer_id}/{semantic_layer_version}/{user_id}",
        session_id=session_id,
        user_text=question,
        assistant_text=answer,
        guardrail=_memory_guardrail,
        region=region,
    )


def _persist_mapping_lesson_from_resolution(
    *, resolution, semantic_layer_id: str, semantic_layer_version: str,
    user_id: str, session_id: str,
) -> None:
    """Best-effort: write a crisp "<term> → <chosen>" lesson on a resolved
    clarification so a later session recalls the binding.

    No-op when memory is unconfigured, the scope is incomplete, or the
    resolution carries no terms. Never raises.

    Args:
        resolution: The :class:`ClarificationResolution` for this turn.
        semantic_layer_id: Layer id (namespace segment 1).
        semantic_layer_version: Active layer version (segment 2).
        user_id: Cognito subject (segment 3).
        session_id: Chat session id (segment 4).
    """
    memory_id = os.environ.get('LESSONS_MEMORY_ID', '')
    if not memory_id or not semantic_layer_id or not semantic_layer_version \
            or not session_id:
        return
    terms = getattr(resolution, 'terms', None) or []
    chosen_names = getattr(resolution, 'chosen_names', None) or []
    if not terms or not chosen_names:
        return
    try:
        persist_mapping_lesson(
            memory_id=memory_id,
            actor_id=f"{semantic_layer_id}/{semantic_layer_version}/{user_id}",
            session_id=session_id,
            terms=terms,
            chosen_label=chosen_names[0],
            guardrail=_memory_guardrail,
            region=region,
        )
    except Exception as exc:  # noqa: BLE001 — mapping lesson must never break a turn
        logger.warning("mapping-lesson persistence failed (non-fatal): %s", exc)


def _run_query(payload: Dict[str, Any], context=None) -> Dict[str, Any]:
    """Resolve one question, then persist the turn into AgentCore Memory.

    Thin wrapper over ``_run_query_core`` so every invocation path (MCP/direct
    entrypoint, chat fallback, live-streaming runner) records lessons through a
    single chokepoint. Persistence is best-effort and never alters the result.
    """
    result = _run_query_core(payload, context=context)
    try:
        # Scope the lessons-memory write to the JWT-derived caller identity, not a
        # request-body `userId`. On the chat path payload['userId'] was already
        # overwritten with the JWT sub; on the direct invoke path it was not, so
        # stamp the trusted value here before persistence (single chokepoint).
        payload = {**payload, 'userId': _user_id_from_context(context, payload) or 'anonymous'}
        _persist_lessons_turn(payload, result)
    except Exception as exc:  # noqa: BLE001 — memory write must never break a reply
        logger.warning("lessons-turn persistence failed (non-fatal): %s", exc)
    return result


# ----------------------------------------------------------------------------
# AG-UI streaming chat dispatch (item #1 — frontend-chat-ag-ui)
# ----------------------------------------------------------------------------
try:
    from agents.shared.agui_emitter import AGUIEmitter  # type: ignore
except ImportError:  # pragma: no cover — runtime container has shared on PYTHONPATH
    try:
        from shared.agui_emitter import AGUIEmitter  # type: ignore
    except ImportError:
        AGUIEmitter = None  # noqa: N806


def _chunk_text(text: str, *, max_chars: int = 80):
    """Split text into chunks for streaming the assistant message."""
    if not text:
        return
    for i in range(0, len(text), max_chars):
        yield text[i : i + max_chars]


# Chat INPUT guardrail + turn-persistence singletons. ``cw_metrics`` is already
# imported at module top (lines ~61/67) — reuse it; do not re-import.
try:
    from agents.shared.guardrails import GuardrailService  # type: ignore
    from agents.shared.chat_sessions import ChatSessionService  # type: ignore
except ImportError:  # pragma: no cover
    from shared.guardrails import GuardrailService  # type: ignore
    from shared.chat_sessions import ChatSessionService  # type: ignore

_guardrails = GuardrailService()
_chat_sessions = ChatSessionService()


def _user_id_from_context(context, payload) -> str:
    """Best-effort Cognito sub from the forwarded Bearer JWT; fallback to payload['userId'].

    AgentCore already validated the token, so decode claims without re-verifying.

    Args:
        context: AgentCore invocation context (may carry ``request_headers``).
        payload: The chat request payload (used for the ``userId`` fallback).

    Returns:
        The Cognito subject id, or the payload ``userId``, or empty string.
    """
    import base64
    import json as _json
    headers = (getattr(context, 'request_headers', None) or {}) if context else {}
    auth = headers.get('Authorization') or headers.get('authorization') or ''
    if auth.startswith('Bearer '):
        try:
            seg = auth[len('Bearer '):].split('.')[1]
            # Re-pad the base64url segment so urlsafe_b64decode accepts it.
            seg += '=' * (-len(seg) % 4)
            return _json.loads(base64.urlsafe_b64decode(seg)).get('sub', '') or ''
        except Exception:  # noqa: BLE001 — malformed token must not break the turn  # nosec B110 — best-effort cleanup/telemetry; failure must not break the request path
            pass
    return payload.get('userId', '') or ''


def _chat_stream(payload: Dict[str, Any], context):
    """AG-UI streaming generator for the Semantic-RAG chat path.

    When ``ENABLE_LIVE_STREAMING=true``, runs the agent in a worker thread
    and emits AG-UI events live from the Strands callback handler.
    Otherwise falls back to the synthesise-after-completion path.
    """
    if AGUIEmitter is None:  # pragma: no cover
        yield {
            'type': 'run_error',
            'turnId': payload.get('turnId') or 't-anon',
            'error': 'AG-UI emitter unavailable',
        }
        return

    turn_id = payload.get('turnId') or 't-anon'
    emitter = AGUIEmitter(turn_id=turn_id)

    emitter.run_started(agent='metadata_query', model=QUERY_MODEL_ID)
    for line in emitter.drain():
        yield line

    message = payload.get('message', '')
    session_id = payload.get('sessionId', '')
    user_id = _user_id_from_context(context, payload)

    # INPUT guardrail: screen the user message BEFORE invoking the model. If the
    # guardrail intervenes, emit a blocked metric + run_error and return without
    # running the agent. (OUTPUT screening is deferred to a later task.)
    g_in = _guardrails.apply(message, source='INPUT')
    if g_in['blocked']:
        cw_metrics.emit('chat.guardrail.blocked', dimensions={'source': 'INPUT'})
        emitter.run_error(error=g_in['message'], reason='GUARDRAIL_INPUT')
        for line in emitter.drain():
            yield line
        return

    # Enforce session-to-user binding BEFORE doing anything with this session.
    # AgentCore Runtime accepts a valid JWT bearing any sessionId, so we must
    # reject a session the authenticated user does not own — otherwise a valid
    # token holder could append to (and read) another user's transcript.
    if session_id:
        try:
            _chat_sessions.get_or_create(session_id=session_id,
                                         ontology_id=payload.get('ontologyId', ''),
                                         mode=payload.get('mode', 'semantic-rag'),
                                         user_id=user_id,
                                         source=payload.get('source', 'chat'))
        except SessionOwnershipError:
            cw_metrics.emit('chat.session.ownership_violation',
                            dimensions={'agent': 'metadata'})
            logger.warning('chat session ownership violation: session=%s user=%s',
                           session_id, user_id)
            emitter.run_error(
                error='This chat session does not belong to the authenticated user.',
                reason='FORBIDDEN_SESSION')
            for line in emitter.drain():
                yield line
            return

        # Persist the USER turn right after the ownership check passes. The
        # user_id guard makes the write atomically owner-checked at the DB
        # level. Fail-soft: a DDB error must never break the stream.
        try:
            _chat_sessions.append_turn(session_id=session_id, role='user',
                                       text=message, turn_id=turn_id,
                                       user_id=user_id)
        except Exception as exc:  # noqa: BLE001 — never break the stream on DDB error
            logger.warning('chat persist (user) failed (non-fatal) session=%s: %s', session_id, exc)

    if os.environ.get('ENABLE_LIVE_STREAMING', '').lower() == 'true':
        try:
            from agents.shared.streaming_runner import stream_agent_run  # type: ignore
        except ImportError:
            from shared.streaming_runner import stream_agent_run  # type: ignore

        query_payload = {
            'question': payload.get('message', ''),
            'id': payload.get('ontologyId', '') or payload.get('id', ''),
            # Forward chat history + sessionId so the agent retains context
            # across turns.
            'messages': payload.get('messages', []),
            'sessionId': payload.get('sessionId', ''),
            # Prefer the JWT-derived subject (resolved above) so AgentCore
            # Memory scopes lessons to the real user, not 'anonymous'.
            'userId': user_id or payload.get('userId', '') or 'anonymous',
        }

        def _run_with_callback(callback, hook=None, phase_sink=None) -> Dict[str, Any]:
            # The deterministic Tier 2 graph streams exclusively through the
            # per-phase trace sink (its phase nodes call it directly); it has no
            # model tool-loop, so the Strands ``callback`` / ``hook`` channels are
            # accepted for the ``stream_agent_run`` contract but not otherwise wired.
            global _STREAMING_PHASE_SINK
            _STREAMING_PHASE_SINK = phase_sink
            try:
                return _run_query(query_payload, context=context)
            finally:
                _STREAMING_PHASE_SINK = None

        # Persist the ASSISTANT turn from inside the runner, just before
        # run_finished, using the exact answer + totals the stream emits — so a
        # reopened chat renders identically to the live stream (the fallback
        # path below does the same via append_turn). Fail-soft: a DDB error
        # must never break the stream.
        def _persist_assistant(answer_text: str, totals: Dict[str, Any]) -> None:
            if not session_id:
                return
            try:
                _chat_sessions.append_turn(
                    session_id=session_id, role='assistant',
                    text=answer_text, turn_id=turn_id, totals=totals,
                    user_id=user_id)
            except Exception as exc:  # noqa: BLE001 — never break the stream on DDB error
                logger.warning('chat persist (assistant, live) failed (non-fatal) session=%s: %s',
                               session_id, exc)

        for line in stream_agent_run(
            emitter=emitter, run_agent=_run_with_callback,
            on_result=_persist_assistant,
        ):
            yield line
        # NOTE: OUTPUT guardrail for the live path is still deferred to the
        # output-screening task. Assistant-turn persistence now happens via the
        # on_result sink above.
        return

    try:
        query_payload = {
            'question': payload.get('message', ''),
            'id': payload.get('ontologyId', '') or payload.get('id', ''),
            # Forward chat history + sessionId so the agent retains context
            # across turns.
            'messages': payload.get('messages', []),
            'sessionId': payload.get('sessionId', ''),
            # Prefer the JWT-derived subject (resolved above) so AgentCore
            # Memory scopes lessons to the real user, not 'anonymous'.
            'userId': user_id or payload.get('userId', '') or 'anonymous',
        }
        result = _run_query(query_payload, context=context)
    except Exception as exc:  # noqa: BLE001
        emitter.run_error(error=f"agent failed: {exc}")
        for line in emitter.drain():
            yield line
        return

    sql_query = result.get('sql_query', '') if isinstance(result, dict) else ''
    if sql_query:
        call_id = f"sql-{turn_id}"
        emitter.tool_call_start(
            tool_name='execute_sql_query',
            call_id=call_id,
            args={'sql': sql_query[:500]},
        )
        for line in emitter.drain():
            yield line
        emitter.tool_call_end(
            call_id=call_id,
            result={
                'rowCount': len(result.get('results', []) or []),
                'kbSources': len(result.get('n_quads', []) or []),
            },
        )
        for line in emitter.drain():
            yield line

    answer_text = (result.get('answer') or '') if isinstance(result, dict) else str(result)
    for delta in _chunk_text(answer_text):
        emitter.message_chunk(delta=delta)
        for line in emitter.drain():
            yield line

    rows = result.get('results', []) or [] if isinstance(result, dict) else []
    kb_sources = result.get('n_quads', []) or [] if isinstance(result, dict) else []
    metadata = result.get('metadata', {}) if isinstance(result, dict) else {}
    _ROW_CAP = 200

    totals = {
        'sql': sql_query,
        'rowCount': len(rows),
        'rows': rows[:_ROW_CAP],
        'truncated': len(rows) > _ROW_CAP,
        'kbSources': kb_sources,
        'usage': metadata.get('usage') or {},
        'runtimeMs': metadata.get('runtimeMs') or 0,
        # Answer-source label so the UI renders a per-tier trust badge. Present
        # on every Tier 1/2 response dict; None-safe when a path omits it.
        'provenance': result.get('provenance') if isinstance(result, dict) else None,
        # Executed-vs-generated distinction for the eval harness: ``executedSql`` is
        # "" on a degraded/gate-rejected run (``sql`` above is then the rejected
        # query), and ``degraded`` names the terminal reason. Lets the eval record
        # the SQL that ACTUALLY ran + why a turn produced no rows.
        'executedSql': (result.get('executed_sql', '') if isinstance(result, dict) else ''),
        'degraded': (result.get('degraded') if isinstance(result, dict) else None),
    }
    # Carry the pending-clarification record (if this turn asked one) into the
    # persisted totals so the NEXT turn can resolve the user's selection.
    if isinstance(result, dict) and result.get('clarification'):
        totals['clarification'] = result['clarification']

    # Persist the ASSISTANT turn in the fallback path only — here the final
    # ``result`` dict (and thus the answer text + totals) is available. Fail-soft.
    if session_id:
        try:
            _chat_sessions.append_turn(
                session_id=session_id, role='assistant', text=answer_text,
                turn_id=turn_id, totals=totals, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning('chat persist (assistant) failed (non-fatal) session=%s: %s', session_id, exc)

    emitter.run_finished(message_id=f"m-{turn_id}", totals=totals)
    for line in emitter.drain():
        yield line


@app.entrypoint
def invoke(payload: Dict[str, Any], context):
    """Dispatching entrypoint: chat stream when payload has turnId/messages,
    otherwise the request/response path (MCP tools, direct runtime
    invocation)."""
    is_chat = bool(payload.get('turnId') or 'messages' in payload)
    if is_chat:
        return _chat_stream(payload, context=context)
    return _run_query(payload, context=context)


if __name__ == '__main__':
    try:
        app.run()
    except Exception as e:
        import traceback
        logger.error(f"STARTUP FATAL: app.run() failed: {e}\n{traceback.format_exc()}")  # nosemgrep: logging-error-without-handling — startup fatal; must log before re-raise to ensure the error is captured
        raise
