"""
Metadata Query Agent with Bedrock Knowledge Base
Queries Bedrock KB for metadata context, generates SQL queries, and executes on Athena.
Returns query results with semantic context from the knowledge base.
"""

import os
import json
import logging
import re
import threading
import contextvars
from typing import Dict, Any, Optional
import boto3
try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp
except ImportError as e:
    import sys
    print(f"STARTUP ERROR: failed to import BedrockAgentCoreApp: {e}", flush=True)
    sys.exit(1)
try:
    from opentelemetry import baggage as _otel_baggage
except ImportError:
    _otel_baggage = None  # type: ignore
from strands import Agent, tool
from strands.models import BedrockModel
from .token_manager import count_tokens
from boto3.dynamodb.conditions import Key
from .query_prompts import SYSTEM_PROMPT, QUERY_MODEL_ID

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
metadata_table_name = os.getenv('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
dynamodb = boto3.resource('dynamodb', region_name= region)
metadata_table = dynamodb.Table(metadata_table_name)


# Token management constants
MAX_TOKENS_PER_REQUEST = 150000

# Per-invocation state storage.
# _session_id_var propagates to executor threads (asyncio.run_in_executor copies
# the current Context in Python 3.7+), so tools running in a worker thread and
# the invoke function share the same session_id and therefore the same state dict.
_session_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    'session_id', default=None
)
_states: Dict[str, dict] = {}
_states_lock = threading.Lock()


def _get_state() -> dict:
    """Return the state dict for the current invocation session."""
    sid = _session_id_var.get()
    if not sid:
        # Fallback: return a throwaway dict (should not normally happen)
        return {'kb_context_retrieved': False, 'disambiguation_complete': False,
                'query_executed': False, 'cached_results': {}}
    with _states_lock:
        return _states.setdefault(sid, {
            'kb_context_retrieved': False,
            'disambiguation_complete': False,
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
            'kb_context_retrieved': False,
            'disambiguation_complete': False,
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

@tool
def retrieve_kb_context(user_query: str) -> str:
    """
    Retrieve semantic context from Bedrock Knowledge Base for user query.

    Args:
        user_query: Natural language query from user

    Returns:
        JSON string with retrieved KB context and metadata
    """
    state = _get_state()

    # Return cached result if already executed
    if state['kb_context_retrieved'] and 'kb_context' in state['cached_results']:
        logger.info("retrieve_kb_context already executed, returning cached result")
        return state['cached_results']['kb_context']

    try:
        state['kb_context_retrieved'] = True
        logger.info("=== retrieve_kb_context STARTED ===")
        logger.info(f"User query: {user_query}")

        # Get KB ID from environment
        kb_id = os.getenv('SEMANTIC_RAG_KB_ID')
        if not kb_id:
            return json.dumps({"error": "SEMANTIC_RAG_KB_ID environment variable not set"})

        session = get_boto_session()
        bedrock_client = session.client('bedrock-agent-runtime', region_name=region)

        # Query the knowledge base
        response = bedrock_client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={'text': user_query},
            retrievalConfiguration={
                'vectorSearchConfiguration': {
                    'numberOfResults': 5
                }
            }
        )

        # Extract and structure the retrieval results
        retrieved_docs = response.get('retrievalResults', [])
        context_items = []

        for doc in retrieved_docs:
            location = doc.get('location', {})
            source_uri = (
                location.get('s3Location', {}).get('uri', '')
                or location.get('webLocation', {}).get('url', '')
                or (str(location) if location else '')
            )
            context_items.append({
                "content": doc.get('content', {}).get('text', ''),
                "source": source_uri,
                "metadata": doc.get('metadata', {}),
                "score": doc.get('score', 0)
            })

        result = {
            "query": user_query,
            "kb_id": kb_id,
            "documents_retrieved": len(context_items),
            "context": context_items
        }

        final_result = json.dumps(result, indent=2)
        final_tokens = count_tokens(final_result)
        logger.info(f"=== retrieve_kb_context COMPLETED - {final_tokens} tokens ===")

        # Cache the result
        state['cached_results']['kb_context'] = final_result

        return final_result

    except Exception as e:
        logger.error(f"Error retrieving KB context: {str(e)}")
        return json.dumps({"error": str(e), "user_query": user_query})

@tool
def disambiguate_query_terms(user_query: str) -> str:
    """
    Detect ambiguous terms in user query and suggest clarifications.
    Uses two signals: token-match against KB table names, and KB synonym extraction.
    Automatically uses the KB context cached by retrieve_kb_context.

    Args:
        user_query: Original natural language query from user

    Returns:
        JSON string with disambiguation results
    """
    state = _get_state()

    if state['disambiguation_complete'] and 'disambiguation_result' in state['cached_results']:
        logger.info("disambiguate_query_terms already executed, returning cached result")
        return state['cached_results']['disambiguation_result']

    try:
        state['disambiguation_complete'] = True
        kb_context = state['cached_results'].get('kb_context', '{}')
        logger.info("=== disambiguate_query_terms STARTED ===")
        logger.info(f"User query: {user_query}")

        stop_words = {
            # question words
            'how', 'many', 'what', 'which', 'who', 'where', 'when', 'why',
            # verbs / auxiliaries
            'show', 'me', 'get', 'find', 'list', 'give', 'tell', 'count',
            'are', 'is', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had',
            'do', 'does', 'did', 'can', 'could', 'would', 'should', 'will',
            # articles / determiners
            'all', 'the', 'a', 'an', 'any', 'some', 'each', 'every',
            'this', 'that', 'these', 'those',
            # prepositions / conjunctions
            'from', 'with', 'their', 'for', 'in', 'on', 'at', 'to', 'of',
            'and', 'or', 'not', 'no', 'by', 'as', 'per',
            # misc common query fillers
            'there', 'total', 'number', 'records', 'entries', 'items',
            'please', 'just', 'only', 'top', 'first', 'last', 'latest',
        }
        query_lower = user_query.lower()
        words = re.findall(r'\b\w+\b', query_lower)
        query_terms = [w for w in words if w not in stop_words and len(w) > 2]
        logger.info(f"Extracted query terms: {query_terms}")

        # Parse KB context
        kb = {}
        try:
            kb = json.loads(kb_context) if kb_context else {}
        except Exception as e:
            logger.warning(f"KB context parse failed: {e}")

        # Build table name → list of {table, database, catalog} from KB chunk metadata
        table_name_to_info: dict = {}
        for doc in kb.get('context', []):
            metadata = doc.get('metadata', {})
            table_name = (
                metadata.get('table_name') or
                metadata.get('table') or
                metadata.get('tableName') or
                ''
            ).lower().strip()
            database = metadata.get('database_name') or metadata.get('database') or ''
            catalog = metadata.get('catalog_id') or metadata.get('catalog_name') or metadata.get('catalog') or ''

            if table_name:
                entry = {'table': table_name, 'database': database, 'catalog': catalog}
                if table_name not in table_name_to_info:
                    table_name_to_info[table_name] = []
                if entry not in table_name_to_info[table_name]:
                    table_name_to_info[table_name].append(entry)

        logger.info(f"Built table mappings: {list(table_name_to_info.keys())}")

        # Build KB synonym map: term → {table, database, catalog}
        # For each doc, if the table name appears in the content, treat other
        # significant words as synonyms that also resolve to that table.
        kb_synonyms: dict = {}
        try:
            for doc in kb.get('context', []):
                text = doc.get('content', '').lower()
                metadata = doc.get('metadata', {})
                table_name = (
                    metadata.get('table_name') or
                    metadata.get('table') or
                    metadata.get('tableName') or
                    ''
                ).lower().strip()
                database = metadata.get('database_name') or metadata.get('database') or ''
                catalog = metadata.get('catalog_id') or metadata.get('catalog_name') or metadata.get('catalog') or ''

                if table_name and table_name in text:
                    doc_words = re.findall(r'\b\w+\b', text)
                    for w in doc_words:
                        if w not in stop_words and len(w) > 2 and w != table_name:
                            kb_synonyms[w] = {'table': table_name, 'database': database, 'catalog': catalog}
        except Exception as e:
            logger.warning(f"KB synonym extraction failed: {e}")

        # Analyze each query term
        mappings = {}
        ambiguities = []
        unknown_terms = []

        for term in query_terms:
            table_matches = table_name_to_info.get(term, [])

            # Check plural/singular variations
            term_singular = term.rstrip('s') if term.endswith('s') else term
            term_plural = term + 's' if not term.endswith('s') else term

            if not table_matches:
                table_matches = (
                    table_name_to_info.get(term_singular, []) or
                    table_name_to_info.get(term_plural, [])
                )

            total_matches = len(table_matches)

            if total_matches == 0:
                # KB synonym fallback — promotes unknown terms that KB maps to a table
                if term in kb_synonyms:
                    syn = kb_synonyms[term]
                    mappings[term] = {
                        "status": "CLEAR",
                        "table": syn["table"],
                        "database": syn["database"],
                        "catalog": syn["catalog"],
                        "confidence": 0.8,
                        "source": "kb_synonym",
                    }
                    continue  # skip unknown_terms append

                # Unknown term — suggest alternatives
                suggestions = [
                    {"table": name, "similarity": 0.5}
                    for name in list(table_name_to_info.keys())[:5]
                ]
                unknown_terms.append({
                    "term": term,
                    "suggestions": suggestions,
                    "message": f"No '{term}' found. Did you mean one of these?",
                })

            elif total_matches == 1:
                match = table_matches[0]
                mappings[term] = {
                    "status": "CLEAR",
                    "table": match["table"],
                    "database": match["database"],
                    "catalog": match["catalog"],
                    "confidence": 0.9,
                }

            else:
                # Multiple matches — deduplicate by (table, database) pair before
                # deciding whether this is truly ambiguous.
                seen_pairs: set = set()
                matches_list = []
                for match in table_matches:
                    pair = (match['table'], match['database'])
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        matches_list.append({
                            "interpretation": f"Table: {match['table']} (database: {match['database']})",
                            "table": match["table"],
                            "database": match["database"],
                            "catalog": match["catalog"],
                        })

                if len(seen_pairs) == 1:
                    only = matches_list[0]
                    mappings[term] = {
                        "status": "CLEAR",
                        "table": only["table"],
                        "database": only["database"],
                        "catalog": only["catalog"],
                        "confidence": 1.0,
                    }
                else:
                    ambiguities.append({
                        "term": term,
                        "status": "AMBIGUOUS",
                        "matches": matches_list,
                        "clarification_needed": f"Which interpretation of '{term}' do you mean?",
                    })

        # Determine overall status
        if ambiguities:
            status = "AMBIGUOUS"
            can_proceed = False
        elif unknown_terms:
            status = "UNKNOWN"
            can_proceed = False
        else:
            status = "CLEAR"
            can_proceed = True

        result = {
            "status": status,
            "terms_analyzed": len(query_terms),
            "mappings": mappings,
            "ambiguities": ambiguities,
            "unknown_terms": unknown_terms,
            "can_proceed": can_proceed,
        }

        final_result = json.dumps(result)
        logger.info(f"=== disambiguate_query_terms COMPLETED - Status: {status} ===")
        state['cached_results']['disambiguation_result'] = final_result
        return final_result

    except Exception as e:
        logger.error(f"Error in disambiguation: {str(e)}")
        return json.dumps({"error": str(e)})


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

        # Start query execution
        response = athena_client.start_query_execution(
            QueryString=sql_query,
            QueryExecutionContext=query_context,
            ResultConfiguration={'OutputLocation': s3_output_location},
            WorkGroup=workgroup
        )

        query_execution_id = response['QueryExecutionId']
        logger.info(f"Query submitted: execution_id={query_execution_id}")

        # Wait for query completion
        import time
        max_wait_time = 600
        wait_interval = 2
        elapsed_time = 0

        while elapsed_time < max_wait_time:
            response = athena_client.get_query_execution(QueryExecutionId=query_execution_id)
            status = response['QueryExecution']['Status']['State']

            if status == 'SUCCEEDED':
                logger.info(f"Query succeeded")
                break
            elif status in ['FAILED', 'CANCELLED']:
                error_msg = response['QueryExecution']['Status'].get('StateChangeReason', 'Unknown error')
                logger.error(f"Query failed: {error_msg}")
                return json.dumps({"error": f"Query failed: {error_msg}", "query_execution_id": query_execution_id})

            time.sleep(wait_interval)  # nosemgrep: arbitrary-sleep — intentional polling interval for Athena query status
            elapsed_time += wait_interval

        if elapsed_time >= max_wait_time:
            return json.dumps({"error": "Query timed out", "query_execution_id": query_execution_id})

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


def create_metadata_query_agent() -> Agent:
    """Create and configure the Metadata Query Agent with Bedrock KB"""
    model = BedrockModel(
        model_id=QUERY_MODEL_ID,
        temperature=0.0,
        max_tokens=4000,
        boto_session=get_boto_session()
    )

    # Local tools for KB and query execution
    local_tools = [
        retrieve_kb_context,
        disambiguate_query_terms,
        execute_sql_query,
    ]

    # Create agent with tools
    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=local_tools,
    )

    logger.info("Metadata Query Agent created with Bedrock KB integration")
    return agent


@app.entrypoint
def invoke(payload: Dict[str, Any], context) -> Dict[str, Any]:
    """
    Entrypoint for Bedrock AgentCore Runtime.
    Invoke the agent with a payload.

    Args:
        payload: Dictionary with 'question' and 'id' keys
        context: AgentCore runtime context (session info, metadata)

    Returns:
        Dictionary with the agent's response
    """
    try:
        import uuid
        session_id = str(uuid.uuid4())[:8]
        if _otel_baggage:
            _otel_baggage.set_baggage("session.id", context.session_id if hasattr(context, "session_id") else session_id)
        reset_agent_state(session_id)

        question = payload.get('question', '')
        if not question:
            return {'error': 'question is required in payload'}

        id = payload.get('id', '')
        config = get_latest_metadata_item(id)
        if not config:
            raise ValueError(f"metadata config not found: {id}")

        agent = create_metadata_query_agent()
        user_input = (
            f"{question}"
        )
        logger.info(f"Session {session_id} — id={id} q={user_input[:100]}...")
        response = agent(user_input)
        try:
            result_text = response.message['content'][0]['text']
        except (KeyError, IndexError, TypeError):
            result_text = str(response)

        # Build structured response from cached tool results so the query service
        # can store sql_query, results[], and reasoning{} in S3.
        cached = _get_state()['cached_results']
        query_result: dict = {}
        try:
            raw = cached.get('query_result', '{}')
            query_result = json.loads(raw) if raw else {}
        except Exception as _e:
            logger.debug("Failed to parse query_result from cached_results: %s", _e)  # nosec B110

        disambiguation_result: dict = {}
        try:
            raw = cached.get('disambiguation_result', '{}')
            disambiguation_result = json.loads(raw) if raw else {}
        except Exception as _e:
            logger.debug("Failed to parse disambiguation_result from cached_results: %s", _e)  # nosec B110

        kb_context: dict = {}
        try:
            raw = cached.get('kb_context', '{}')
            kb_context = json.loads(raw) if raw else {}
        except Exception as _e:
            logger.debug("Failed to parse kb_context from cached_results: %s", _e)  # nosec B110

        # Build results list from inline columns/rows (metadata agent does not use S3 for rows)
        columns: list = query_result.get('columns', [])
        rows: list = query_result.get('rows', [])
        results_list: list = [
            {col: (row[i] if i < len(row) else '') for i, col in enumerate(columns)}
            for row in rows
        ]

        sql_query: str = query_result.get('sql_query', '')

        # Build KB source citations from retrieved KB documents.
        # n_quads repurposed for SemanticRAG: each entry is a source citation dict.
        kb_sources: list = []
        for doc in kb_context.get('context', []):
            meta = doc.get('metadata', {})
            excerpt = doc.get('content', '')[:200].strip()
            kb_sources.append({
                "sourceUri": doc.get('source', ''),
                "excerpt": excerpt,
                "score": round(float(doc.get('score', 0)), 4),
                "tableName": meta.get('table_name') or meta.get('table') or '',
                "database": meta.get('database_name') or meta.get('database') or '',
            })

        # Build reasoning summary from KB disambiguation and execution metadata
        mappings: dict = disambiguation_result.get('mappings', {})
        kb_mapping_parts = [
            f"{term} → {info.get('table', '')} (database: {info.get('database', '')})"
            for term, info in mappings.items()
            if info.get('status') == 'CLEAR'
        ]
        kb_mapping_summary = ', '.join(kb_mapping_parts) if kb_mapping_parts else 'KB metadata mappings applied'

        execution_id = query_result.get('query_execution_id', '')
        data_source = f"Athena execution: {execution_id}" if execution_id else "Athena"

        logger.info(f"Session {session_id} — returning structured response: sql_query={bool(sql_query)}, rows={len(rows)}, kb_sources={len(kb_sources)}")

        return {
            "answer": result_text,
            "sql_query": sql_query,
            "results": results_list,
            "n_quads": kb_sources,
            "reasoning": {
                "interpretation": f"Analyzed {disambiguation_result.get('terms_analyzed', 0)} query terms against KB metadata",
                "graphTraversal": kb_mapping_summary,
                "dataSourceSelection": data_source,
                "sqlQuery": sql_query,
                "summarization": f"Query returned {len(rows)} row(s) across {len(columns)} column(s)",
            },
            "metadata": {
                "executionTimeMs": 0,
                "dataScannedBytes": 0,
            },
        }

    except Exception as e:
        logger.error(f"Error in invoke: {str(e)}")
        return {'error': f'Agent execution failed: {str(e)}'}

if __name__ == '__main__':
    try:
        app.run()
    except Exception as e:
        import traceback
        logger.error(f"STARTUP FATAL: app.run() failed: {e}\n{traceback.format_exc()}")  # nosemgrep: logging-error-without-handling — startup fatal; must log before re-raise to ensure the error is captured
        raise
