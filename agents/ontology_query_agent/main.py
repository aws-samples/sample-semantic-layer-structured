"""
Virtual Knowledge Graph Query Agent
Transforms natural language queries into SQL using ontology mappings,
executes on Athena, and returns semantic RDF results.

NOTE: Neptune access is now via AgentCore Gateway (not direct)
"""

import os
import json
import logging
import re
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
from typing import Dict, Any, List, Optional
from boto3.dynamodb.conditions import Key
from strands import Agent, tool
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
try:
    from strands.types.exceptions import StructuredOutputException
except ImportError:
    StructuredOutputException = None
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from .token_manager import count_tokens, get_token_status

from .query_prompts import SYSTEM_PROMPT, QUERY_MODEL_ID, QueryAnswer

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

metadata_table_name = os.getenv('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
dynamodb = boto3.resource('dynamodb', region_name=os.getenv('AWS_REGION', 'us-east-1'))
metadata_table = dynamodb.Table(metadata_table_name)


# Token management constants
MAX_TOKENS_PER_REQUEST = 150000

# Global state tracking to prevent loops
_agent_state = {
    'ontology_retrieved': False,
    'disambiguation_complete': False,
    'query_executed': False,
    'rdf_mapped': False,
    'current_session': None,
    'cached_results': {}
}

# Global boto3 session for credential injection (used in notebooks/testing)
_boto_session = None

def set_boto_session(session: boto3.Session):
    """
    Set the boto3 session to use for all AWS API calls.
    Useful for injecting credentials from notebooks or tests.

    If the provided session has no region set, a new session is created that
    preserves the same profile/credentials but includes the resolved region
    (from AWS_REGION env var, defaulting to us-east-1). This ensures downstream
    boto3 clients (e.g. BedrockModel) can always determine the region.

    Args:
        session: Configured boto3.Session with desired credentials
    """
    global _boto_session, dynamodb, metadata_table
    region_name = session.region_name or os.getenv('AWS_REGION', 'us-east-1')

    if session.region_name is None:
        # boto3 sessions are immutable — rebuild with the resolved region while
        # preserving the profile (accessed via the internal botocore session).
        try:
            profile_name = session._session.get_config_variable('profile') or None
        except Exception:
            profile_name = None
        _boto_session = boto3.Session(profile_name=profile_name, region_name=region_name)
    else:
        _boto_session = session

    # Reinitialize module-level DynamoDB so it uses the injected session credentials
    dynamodb = _boto_session.resource('dynamodb', region_name=region_name)
    metadata_table = dynamodb.Table(metadata_table_name)
    logger.info(f"Boto3 session set with region: {region_name}")

def get_boto_session() -> boto3.Session:
    """Get the configured boto3 session, or create a default one"""
    global _boto_session
    if _boto_session is None:
        _boto_session = boto3.Session()
    return _boto_session


def get_region() -> str:
    """Return the AWS region from the active session, falling back to env var."""
    return get_boto_session().region_name or os.getenv('AWS_REGION', 'us-east-1')


def reset_agent_state(session_id: str = None):
    """Reset agent state for new query"""
    global _agent_state
    _agent_state = {
        'ontology_retrieved': False,
        'sparql_discovery_complete': False,
        'disambiguation_complete': False,
        'query_executed': False,
        'rdf_mapped': False,
        'current_session': session_id,
        'cached_results': {}
    }
    logger.info(f"Agent state reset for session: {session_id}")


# ==============================================================================
# NEPTUNE Tools accessed via AgentCore Gateway via MCP
# ==============================================================================
# - discover_named_graphs() (internal use only)
# - get_ontology_from_neptune(ontology_id)
# - execute_sparql_query(sparql_query, query_type)
# ==============================================================================

def _normalize_ontology_data(ontology: dict) -> dict:
    """Convert list-format ontology fields to URI-keyed dicts if needed."""
    result = dict(ontology)
    for field, uri_key in [('classes', 'uri'), ('mappings', 'uri'), ('properties', 'uri')]:
        val = ontology.get(field, {})
        if isinstance(val, list):
            d = {}
            for item in val:
                if isinstance(item, dict):
                    uri = (item.get(uri_key) or item.get('classUri')
                           or item.get('propertyUri') or '')
                    if uri:
                        d[uri] = item
            result[field] = d
    return result


@tool
def disambiguate_query_terms(user_query: str, ontology_info: str,
                              kb_context: str = '{}',
                              sparql_context: str = '{}') -> str:
    """
    Detect ambiguous terms in user query and suggest clarifications.
    Uses three signals: token-match, ontology synonyms (from rdfs:label/comment), and SPARQL entity-discovery.

    Args:
        user_query: Original natural language query from user
        ontology_info: JSON string from get_ontology_from_neptune
        kb_context: Knowledge base context (optional, deprecated parameter)
        sparql_context: JSON string from execute_sparql_query (optional)

    Returns:
        JSON string with disambiguation results
    """
    global _agent_state

    # Return cached result if already executed
    if _agent_state['disambiguation_complete'] and 'disambiguation_result' in _agent_state['cached_results']:
        logger.info("disambiguate_query_terms already executed, returning cached result")
        return _agent_state['cached_results']['disambiguation_result']

    try:
        _agent_state['disambiguation_complete'] = True
        logger.info("=== disambiguate_query_terms STARTED ===")
        logger.info(f"User query: {user_query}")

        ontology = json.loads(ontology_info)
        ontology = _normalize_ontology_data(ontology)

        if "error" in ontology:
            return json.dumps({"error": f"Ontology error: {ontology['error']}"})

        # Extract query terms (simple word tokenization)
        # Remove common words and extract potential entity/table references
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

        # Build simplified mappings from ontology
        # Map class/table names to ontology URIs
        class_name_to_uri = {}
        for class_uri, class_info in ontology.get('classes', {}).items():
            # Extract class name from URI (last part)
            class_name = class_uri.split('/')[-1].lower()
            if class_name not in class_name_to_uri:
                class_name_to_uri[class_name] = []
            class_name_to_uri[class_name].append(class_uri)

        # Also check table mappings
        table_to_class = {}
        for uri, mapping in ontology.get('mappings', {}).items():
            if 'table' in mapping and uri in ontology.get('classes', {}):
                table_name = mapping['table'].split('.')[-1].lower()  # Get table name without database prefix
                if table_name not in table_to_class:
                    table_to_class[table_name] = []
                table_to_class[table_name].append({
                    'class_uri': uri,
                    'table': mapping['table']
                })

        logger.info(f"Built mappings - classes: {len(class_name_to_uri)}, tables: {len(table_to_class)}")

        # Build ontology synonym map from rdfs:label and rdfs:comment
        ontology_synonyms: dict = {}
        try:
            for class_uri, class_info in ontology.get('classes', {}).items():
                table = ontology.get('mappings', {}).get(class_uri, {}).get('table', '')
                # Collect all label/comment text for this class
                label_texts = []
                for key in ('label', 'rdfs:label', 'comment', 'rdfs:comment'):
                    val = class_info.get(key, '') if isinstance(class_info, dict) else ''
                    if val:
                        label_texts.append(val.lower())
                combined = ' '.join(label_texts)
                # Map each meaningful word in label/comment to this class
                for w in re.findall(r'\b\w+\b', combined):
                    if w not in stop_words and len(w) > 2:
                        ontology_synonyms.setdefault(w, {'class_uri': class_uri, 'table': table})
            logger.info(f"Built {len(ontology_synonyms)} ontology synonyms from rdfs:label/comment fields")
        except Exception as e:
            logger.warning(f"Ontology synonym extraction failed: {e}")

        # Build set of class URIs confirmed by SPARQL entity-discovery
        sparql_confirmed: set = set()
        try:
            sc = json.loads(sparql_context) if sparql_context else {}
            for row in sc.get('results', []):
                uri = row.get('class') or row.get('classUri', '')
                if uri:
                    sparql_confirmed.add(uri)
        except Exception as e:
            logger.warning(f"SPARQL context parsing failed: {e}")

        # Analyze each query term
        mappings = {}
        ambiguities = []
        unknown_terms = []

        for term in query_terms:
            # Check for exact matches in class names
            class_matches = class_name_to_uri.get(term, [])

            # Check for exact matches in table names
            table_matches = table_to_class.get(term, [])

            # Also check for plural/singular variations
            term_singular = term.rstrip('s') if term.endswith('s') else term
            term_plural = term + 's' if not term.endswith('s') else term

            if not class_matches:
                class_matches = class_name_to_uri.get(term_singular, []) or class_name_to_uri.get(term_plural, [])

            if not table_matches:
                table_matches = table_to_class.get(term_singular, []) or table_to_class.get(term_plural, [])

            total_matches = len(class_matches) + len(table_matches)

            if total_matches == 0:
                # Ontology synonym fallback — promotes unknown terms matched via rdfs:label/comment
                if term in ontology_synonyms:
                    syn = ontology_synonyms[term]
                    mappings[term] = {
                        "status": "CLEAR",
                        "class": syn["class_uri"],
                        "table": syn["table"],
                        "confidence": 0.8,
                        "source": "ontology_synonym",
                    }
                    continue  # skip unknown_terms append

                # Unknown term - suggest alternatives
                suggestions = []
                for name in list(class_name_to_uri.keys())[:5]:  # Top 5 suggestions
                    suggestions.append({
                        "class": name,
                        "similarity": 0.5  # Placeholder
                    })
                unknown_terms.append({
                    "term": term,
                    "suggestions": suggestions,
                    "message": f"No '{term}' found. Did you mean one of these?"
                })

            elif total_matches == 1:
                # Clear match — boost confidence when SPARQL confirms the class URI
                if class_matches:
                    class_uri = class_matches[0]
                    table_mapping = ontology['mappings'].get(class_uri, {}).get('table', '')
                    confidence = 1.0 if class_uri in sparql_confirmed else 0.9
                    mappings[term] = {
                        "status": "CLEAR",
                        "class": class_uri,
                        "table": table_mapping,
                        "confidence": confidence,
                    }
                elif table_matches:
                    match = table_matches[0]
                    confidence = 1.0 if match['class_uri'] in sparql_confirmed else 0.9
                    mappings[term] = {
                        "status": "CLEAR",
                        "class": match['class_uri'],
                        "table": match['table'],
                        "confidence": confidence,
                    }

            else:
                # Multiple matches found — deduplicate by (class, table) pair before
                # deciding whether this is truly ambiguous.  A term that matches both
                # its class name and the corresponding table name resolves to a single
                # entity and should be treated as CLEAR, not AMBIGUOUS.
                seen_pairs: set = set()
                matches_list = []
                for class_uri in class_matches:
                    table_mapping = ontology['mappings'].get(class_uri, {}).get('table', '')
                    pair = (class_uri, table_mapping)
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        matches_list.append({
                            "interpretation": f"Class: {class_uri.split('/')[-1]}",
                            "class": class_uri,
                            "table": table_mapping
                        })
                for table_match in table_matches:
                    pair = (table_match['class_uri'], table_match['table'])
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        matches_list.append({
                            "interpretation": f"Table: {table_match['table']}",
                            "class": table_match['class_uri'],
                            "table": table_match['table']
                        })

                if len(seen_pairs) == 1:
                    # All interpretations map to the same entity — not truly ambiguous.
                    only = matches_list[0]
                    mappings[term] = {
                        "status": "CLEAR",
                        "class": only["class"],
                        "table": only["table"],
                        "confidence": 1.0
                    }
                else:
                    ambiguities.append({
                        "term": term,
                        "status": "AMBIGUOUS",
                        "matches": matches_list,
                        "clarification_needed": f"Which interpretation of '{term}' do you mean?"
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
            "can_proceed": can_proceed
        }

        final_result = json.dumps(result)
        logger.info(f"=== disambiguate_query_terms COMPLETED - Status: {status} ===")

        # Cache the result
        _agent_state['cached_results']['disambiguation_result'] = final_result

        return final_result

    except Exception as e:
        logger.error(f"Error in disambiguation: {str(e)}")
        return json.dumps({"error": str(e)})

@tool
def execute_sql_query(sql_query: str, database_name: str, catalog_id: str) -> str:
    """
    Execute SQL query on Athena

    Args:
        sql_query: SQL query to execute
        database_name: Athena database name to query against
        catalog_id: Athena catalog to use for query

    Returns:
        JSON string with columns and rows
    """
    global _agent_state

    if not _agent_state['disambiguation_complete']:
        logger.error("execute_sql_query called before disambiguate_query_terms")
        return json.dumps({"error": "Must disambiguate query first"})

    try:
        # NOTE: Do NOT set query_executed = True here. It is only set after a
        # successful execution so that the agent can retry with a corrected query
        # if the first attempt fails (e.g. wrong catalog or schema).
        logger.info("=== execute_sql_query STARTED ===")
        logger.info(f"Original SQL  : {sql_query}")
        logger.info(f"Database param: {database_name}")
        logger.info(f"Catalog ID    : '{catalog_id}'")

        region = get_region()
        session = get_boto_session()
        athena_client = session.client('athena', region_name=region)

        # S3 bucket for query results
        try:
            session_ssm = get_boto_session()
            ssm_client = session_ssm.client('ssm', region_name=region)
            s3_bucket_param = f'/{os.getenv("PROJECT_NAME", "semantic-layer")}/athena/query-results-bucket'
            response = ssm_client.get_parameter(Name=s3_bucket_param, WithDecryption=True)
            athena_bucket = response['Parameter']['Value']
            s3_output_location = f"s3://{athena_bucket}/virtual-kg-query-results/"
            logger.info(f"Output bucket : SSM parameter ({s3_bucket_param})")
        except:
            # Fallback to environment variable
            athena_bucket = os.getenv('ATHENA_RESULTS_BUCKET', f'{os.getenv("PROJECT_NAME", "semantic-layer")}-athena-results')
            s3_output_location = f"s3://{athena_bucket}/virtual-kg-query-results/"
            logger.info(f"Output bucket : env/default fallback (SSM lookup failed)")

        logger.info(f"Output location: {s3_output_location}")

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
        execution_time_ms: int = 0
        data_scanned_bytes: int = 0

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
        max_wait_time = 600  # 10 minutes
        wait_interval = 2
        elapsed_time = 0
        last_state = None

        while elapsed_time < max_wait_time:
            response = athena_client.get_query_execution(QueryExecutionId=query_execution_id)
            status = response['QueryExecution']['Status']['State']

            if status != last_state:
                logger.info(f"Query state   : {status} (elapsed {elapsed_time}s, id={query_execution_id})")
                last_state = status

            if status == 'SUCCEEDED':
                stats = response['QueryExecution'].get('Statistics', {})
                execution_time_ms = stats.get('EngineExecutionTimeInMillis', 0)
                data_scanned_bytes = stats.get('DataScannedInBytes', 0)
                logger.info(
                    f"Query SUCCEEDED in {elapsed_time}s — "
                    f"scanned={data_scanned_bytes}B, "
                    f"engine_ms={execution_time_ms}ms"
                )
                break
            elif status in ['FAILED', 'CANCELLED']:
                error_msg = response['QueryExecution']['Status'].get('StateChangeReason', 'Unknown error')
                logger.error(f"Query {status}: {error_msg}")
                logger.error(f"  query_type={query_type}, execution_id={query_execution_id}")
                logger.error(f"  SQL submitted: {sql_query}")
                return json.dumps({"error": f"Query failed: {error_msg}", "query_execution_id": query_execution_id})

            time.sleep(wait_interval)  # nosemgrep: arbitrary-sleep - intentional Athena query status polling loop
            elapsed_time += wait_interval

        if elapsed_time >= max_wait_time:
            return json.dumps({"error": "Query timed out", "query_execution_id": query_execution_id})

        # Get query results
        paginator = athena_client.get_paginator('get_query_results')
        page_iterator = paginator.paginate(QueryExecutionId=query_execution_id)

        columns: list = []
        rows: list = []
        first_page = True
        for page in page_iterator:
            for row in page['ResultSet']['Rows']:
                row_data = [col.get('VarCharValue', '') for col in row['Data']]
                if first_page and not columns:
                    columns = row_data
                    first_page = False
                    continue
                rows.append(row_data)

        logger.info(f"Query returned {len(rows)} rows, {len(columns)} columns")

        # Offload the full result set to S3 so the LLM never sees raw row data
        # (avoids paying tokens for potentially hundreds/thousands of rows).
        s3_result_key: str = f"structured-query-results/{query_execution_id}.json"
        s3_save_ok = False
        try:
            s3_client = get_boto_session().client('s3', region_name=region)
            s3_client.put_object(
                Bucket=athena_bucket,
                Key=s3_result_key,
                Body=json.dumps({"columns": columns, "rows": rows}, ensure_ascii=False).encode('utf-8'),
                ContentType='application/json',
            )
            s3_save_ok = True
            logger.info(f"Full result set saved to s3://{athena_bucket}/{s3_result_key}")
        except Exception as s3_err:
            logger.warning(f"Could not save result set to S3: {s3_err} — storing inline (fallback)")

        # Return a compact summary to the LLM — no row data in the token stream
        execution_stats = {
            "execution_time_ms": execution_time_ms,
            "data_scanned_bytes": data_scanned_bytes,
        }

        if s3_save_ok:
            compact = {
                "sql_query": sql_query,
                "query_execution_id": query_execution_id,
                "row_count": len(rows),
                "column_names": columns,
                "s3_bucket": athena_bucket,
                "s3_key": s3_result_key,
                **execution_stats,
            }
        else:
            # Fallback: include inline data if S3 write failed (cap at 50 rows)
            compact = {
                "sql_query": sql_query,
                "query_execution_id": query_execution_id,
                "row_count": len(rows),
                "columns": columns,
                "rows": rows[:50],
                **execution_stats,
            }

        final_result = json.dumps(compact)
        final_tokens = count_tokens(final_result)
        logger.info(f"=== execute_sql_query COMPLETED - {final_tokens} tokens (compact summary) ===")

        # Mark as executed and cache only on success so retries are possible on failure
        _agent_state['query_executed'] = True
        _agent_state['cached_results']['query_result'] = final_result

        return final_result

    except Exception as e:
        logger.error(f"Error executing Athena query: {str(e)}")
        # Do NOT set query_executed — allow the agent to retry with a corrected query
        return json.dumps({"error": str(e), "sql_query": sql_query, "database_name": database_name, "catalog_id": catalog_id})

@tool
def map_sql_results_to_rdf(query_results: str, ontology_info: str, max_rows: int = 10) -> str:
    """
    Map SQL query results to RDF n-quads using ontology mappings

    Args:
        query_results: JSON string of SQL query results from execute_sql_query
        ontology_info: JSON string of ontology information from get_ontology_from_neptune
        max_rows: Maximum number of rows to convert to RDF (default: 10, max: 100)

    Returns:
        JSON string with RDF n-quads
    """
    global _agent_state

    # Ensure proper sequence
    # Note: ontology_retrieved is NOT checked — get_ontology_from_neptune is an MCP tool
    # and cannot set the local flag. query_executed IS checkable (local tool).
    if not _agent_state['query_executed']:
        logger.error("map_sql_results_to_rdf called before execute_sql_query")
        return json.dumps({"error": "Must execute query first"})

    # Return cached result if already executed
    if _agent_state['rdf_mapped'] and 'rdf_result' in _agent_state['cached_results']:
        logger.info("map_sql_results_to_rdf already executed - WORKFLOW COMPLETE")
        return _agent_state['cached_results']['rdf_result']

    try:
        _agent_state['rdf_mapped'] = True
        logger.info("=== map_sql_results_to_rdf STARTED ===")

        results = json.loads(query_results)
        ontology = json.loads(ontology_info)
        ontology = _normalize_ontology_data(ontology)

        if "error" in results:
            return json.dumps({"error": f"Query error: {results['error']}"})

        if "error" in ontology:
            return json.dumps({"error": f"Ontology error: {ontology['error']}"})

        # If the execute_sql_query tool stored rows in S3, fetch them now.
        # This keeps full row data out of the LLM token stream while still
        # allowing the RDF mapping to process every row.
        if 's3_key' in results:
            try:
                region = get_region()
                s3_client = get_boto_session().client('s3', region_name=region)
                obj = s3_client.get_object(Bucket=results['s3_bucket'], Key=results['s3_key'])
                full_data = json.loads(obj['Body'].read().decode('utf-8'))
                columns = full_data.get('columns', [])
                rows = full_data.get('rows', [])
                logger.info(f"Loaded {len(rows)} rows from S3: {results['s3_key']}")
            except Exception as s3_err:
                logger.error(f"Failed to load result set from S3: {s3_err}")
                return json.dumps({"error": f"Failed to load query results from S3: {s3_err}"})
        else:
            # Inline fallback (S3 write failed during execute_sql_query)
            columns = results.get('columns', results.get('column_names', []))
            rows = results.get('rows', [])

        # Dynamic row processing with safety limits
        MAX_ROWS = min(max(1, max_rows), 100)  # Clamp between 1-100
        processed_rows = rows[:MAX_ROWS]

        if len(rows) > MAX_ROWS:
            logger.warning(f"Result set ({len(rows)} rows) exceeds max_rows limit ({MAX_ROWS}), truncating")

        logger.info(f"Processing {len(processed_rows)} rows using ontology mappings")

        # Derive database_name from the first table mapping in the ontology
        database_name = 'unknown'
        for _uri in ontology.get('mappings', {}).values():
            _table = _uri.get('table', '') if isinstance(_uri, dict) else ''
            if '.' in _table:
                database_name = _table.split('.', 1)[0]
                break

        # Named graph for data
        data_graph = f"<http://example.com/data/{database_name}/1.0.0>"

        # Build mapping from database schema to ontology
        table_to_class = {}
        column_to_property = {}

        # Map ontology classes to database tables
        for class_uri, class_info in ontology.get('classes', {}).items():
            if class_uri in ontology.get('mappings', {}):
                table_mapping = ontology['mappings'][class_uri].get('table', '')
                if '.' in table_mapping:
                    table_name = table_mapping.split('.', 1)[1]
                    table_to_class[table_name] = class_uri

        # Map ontology properties to database columns
        for prop_uri, prop_info in ontology.get('properties', {}).items():
            if prop_uri in ontology.get('mappings', {}):
                column_mapping = ontology['mappings'][prop_uri].get('column', '')
                if '.' in column_mapping:
                    table_name, col_name = column_mapping.split('.', 1)
                    column_to_property[f"{table_name}.{col_name}"] = prop_uri

        logger.info(f"Built mappings - {len(table_to_class)} tables, {len(column_to_property)} columns")

        # Generate n-quads using ontology mappings
        n_quads = []

        # Determine primary table (simplified - use first table)
        primary_table = list(table_to_class.keys())[0] if table_to_class else 'entity'
        primary_class = table_to_class.get(primary_table, f"http://example.com/{database_name}/Entity")

        for i, row in enumerate(processed_rows):
            if len(row) > 0 and any(cell for cell in row if cell):
                # Create entity URI
                entity_uri = f"<http://example.com/data/{primary_table}/{i+1}>"

                # Add type statement
                n_quads.append(f"{entity_uri} <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <{primary_class}> {data_graph} .")

                # Map each column to its ontology property
                for col_idx, column_name in enumerate(columns):
                    if col_idx < len(row) and row[col_idx]:
                        # Look for property mapping
                        property_key = f"{primary_table}.{column_name}"
                        if property_key in column_to_property:
                            prop_uri = column_to_property[property_key]
                            value = str(row[col_idx]).replace('"', '\\"')
                            n_quads.append(f'{entity_uri} <{prop_uri}> "{value}" {data_graph} .')
                        else:
                            # Fallback to simple property
                            prop_uri = f"http://example.com/{database_name}/{column_name}"
                            value = str(row[col_idx]).replace('"', '\\"')
                            n_quads.append(f'{entity_uri} <{prop_uri}> "{value}" {data_graph} .')

        # Return result
        result = {
            "success": True,
            "n_quads_count": len(n_quads),
            "rows_processed": len(processed_rows),
            "total_rows": len(rows),
            "ontology_mappings_used": {
                "classes": len(table_to_class),
                "properties": len(column_to_property)
            },
            "sample_n_quads": n_quads[:3] if n_quads else []
        }

        # Include all n-quads if small dataset
        if len(n_quads) <= 15:
            result["n_quads"] = n_quads

        logger.info(f"Generated {len(n_quads)} n-quads from {len(processed_rows)} rows")

        final_result = json.dumps(result)
        final_tokens = count_tokens(final_result)
        logger.info(f"=== map_sql_results_to_rdf COMPLETED - {final_tokens} tokens ===")

        # Cache the result
        _agent_state['cached_results']['rdf_result'] = final_result

        return final_result

    except Exception as e:
        logger.error(f"Error in RDF mapping: {str(e)}")
        return json.dumps({"error": str(e)})


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


def _make_neptune_tool_logger(session_id: str):
    """Return a Strands callback_handler that logs get_ontology_from_neptune I/O."""
    _pending: dict = {}  # toolUseId -> tool name

    def handler(**kwargs):
        # ── Input: tool use initiated ──────────────────────────────────────────
        current_tool_use = kwargs.get('current_tool_use')
        if current_tool_use:
            name = current_tool_use.get('name', '')
            if 'get_ontology_from_neptune' in name:
                tool_id = current_tool_use.get('toolUseId', '')
                tool_input = current_tool_use.get('input', {})
                _pending[tool_id] = name
                logger.info(
                    f"[{session_id}] === get_ontology_from_neptune INPUT === "
                    f"{json.dumps(tool_input)}"
                )

        # ── Output: tool result returned to model ──────────────────────────────
        message = kwargs.get('message')
        if message and _pending:
            for block in message.get('content', []):
                if not isinstance(block, dict):
                    continue
                tool_result = block.get('toolResult')
                if not tool_result:
                    continue
                tool_id = tool_result.get('toolUseId', '')
                if tool_id not in _pending:
                    continue
                raw = tool_result.get('content', '')
                if isinstance(raw, list):
                    result_str = ' '.join(
                        c.get('text', '') if isinstance(c, dict) else str(c)
                        for c in raw
                    )
                else:
                    result_str = json.dumps(raw) if not isinstance(raw, str) else raw
                preview = result_str[:2000] + ('...' if len(result_str) > 2000 else '')
                logger.info(
                    f"[{session_id}] === get_ontology_from_neptune RESPONSE "
                    f"({len(result_str)} chars) === {preview}"
                )
                del _pending[tool_id]

    return handler


def create_query_agent(callback_handler=None) -> Agent:
    """Create and configure the Virtual KG query agent"""
    region = get_region()
    model = BedrockModel(
        model_id=QUERY_MODEL_ID,
        temperature=0.0,
        max_tokens=4000,
        boto_session=get_boto_session(),
    )

    # Configure MCP client for Neptune Gateway with IAM authentication
    mcp_clients = []
    neptune_gateway_url = os.getenv('NEPTUNE_GATEWAY_URL', '')
    if neptune_gateway_url:
        logger.info("Neptune Gateway configured - using Gateway for Neptune tools (IAM auth)")
        try:
            # Create MCP client with AWS IAM authentication
            neptune_mcp = MCPClient(
                lambda: aws_iam_streamablehttp_client(
                    endpoint=neptune_gateway_url,
                    aws_region=region,
                    aws_service="bedrock-agentcore"
                )
            )
            mcp_clients.append(neptune_mcp)
            logger.info(f"Neptune Gateway MCP client configured: {neptune_gateway_url}")
        except Exception as e:
            logger.error(f"Failed to configure Neptune Gateway MCP client: {e}")
            logger.warning("Agent will not have access to Neptune tools")
    else:
        logger.warning("Neptune Gateway not configured - Neptune tools will not be available")
        logger.warning("Set NEPTUNE_GATEWAY_URL environment variable")

    # Local tools (not Neptune-related)
    local_tools = [
        disambiguate_query_terms,
        execute_sql_query,
        map_sql_results_to_rdf,
    ]

    # Combine local tools with MCP clients
    # MCP clients provide: get_ontology_from_neptune, execute_sparql_query
    all_tools = local_tools + mcp_clients

    # Create agent with all tools (local tools + MCP clients).
    # structured_output_model enforces that the agent's final text is a validated
    # QueryAnswer instance — eliminating raw-JSON bleed-through in the answer field.
    agent_kwargs = dict(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=all_tools,
        structured_output_model=QueryAnswer,
    )
    if callback_handler is not None:
        agent_kwargs['callback_handler'] = callback_handler

    agent = Agent(**agent_kwargs)
    return agent


@app.entrypoint
def invoke(payload: Dict[str, Any], context=None) -> Dict[str, Any]:
    """
    Entrypoint for Bedrock AgentCore Runtime.
    Invoke the agent with a payload.

    Args:
        payload: Dictionary with 'question' and 'id' keys
        context: AgentCore runtime context (session info, metadata)

    Returns:
        Structured dictionary with answer, sql_query, results, n_quads, reasoning
    """
    try:
        # Generate unique session ID and reset state
        import uuid
        global _catalog_id
        session_id = str(uuid.uuid4())[:8]
        if _otel_baggage:
            _otel_baggage.set_baggage("session.id", context.session_id if context and hasattr(context, "session_id") else session_id)
        reset_agent_state(session_id)


        question = payload.get('question', '')
        if not question:
            return {'error': 'question is required in payload'}

        id = payload.get('id', '')
        config = get_latest_metadata_item(id)
        if not config:
            raise ValueError(f"metadata config not found: {id}")

        ontology_name = config.get('name', '')
        catalog_id_hint = payload.get('catalog_id', '')
        agent = create_query_agent(callback_handler=_make_neptune_tool_logger(session_id))
        hint = f"[ontology: {id}]"
        if catalog_id_hint:
            hint += f"[catalog: {catalog_id_hint}]"
        user_input = f"{hint}\n{question}"

        logger.info(f"Agent session {session_id} — ontology name: {ontology_name}, question: {question[:80]}")
        logger.info(f"Starting agent session {session_id} with input: {user_input[:100]}...")

        try:
            response = agent(user_input)
        except Exception as soe:
            if StructuredOutputException and isinstance(soe, StructuredOutputException):
                # Structured output validation failed — fall back to raw text answer
                logger.warning(f"Structured output validation failed: {soe}. Using raw text fallback.")
                response = soe.agent_result  # AgentResult is attached to the exception
            else:
                raise

        logger.info(f"Agent session {session_id} completed successfully")
        logger.info(f"Final state: {_agent_state}")

        # Prefer the validated structured output; fall back to raw message text
        structured: QueryAnswer | None = None
        if response.structured_output and isinstance(response.structured_output, QueryAnswer):
            structured = response.structured_output
            logger.info("Using structured_output for response")
        else:
            logger.warning("structured_output unavailable — using raw message text fallback")

        # --- Clarification response: return immediately, no SQL/RDF data ---
        if structured and structured.needs_clarification:
            logger.info("Agent requests clarification — returning needs_clarification response")
            return {
                "needs_clarification": True,
                "clarification_question": structured.clarification_question,
                "options": structured.options,
                "answer": "",
                "sql_query": "",
                "results": [],
                "n_quads": [],
                "reasoning": {},
            }

        # --- Normal answer path ---
        if structured:
            result_text = structured.answer
        else:
            try:
                result_text = response.message['content'][0]['text']
            except (KeyError, IndexError, TypeError):
                result_text = str(response)

        # Build structured response from cached tool results so the query service
        # can store sql_query, results[], n_quads[], and reasoning{} in S3.
        query_result: dict = {}
        try:
            raw = _agent_state['cached_results'].get('query_result', '{}')
            query_result = json.loads(raw) if raw else {}
        except Exception as _e:
            logger.debug("Failed to parse query_result from cached_results: %s", _e)  # nosec B110

        rdf_result: dict = {}
        try:
            raw = _agent_state['cached_results'].get('rdf_result', '{}')
            rdf_result = json.loads(raw) if raw else {}
        except Exception as _e:
            logger.debug("Failed to parse rdf_result from cached_results: %s", _e)  # nosec B110

        disambiguation_result: dict = {}
        try:
            raw = _agent_state['cached_results'].get('disambiguation_result', '{}')
            disambiguation_result = json.loads(raw) if raw else {}
        except Exception as _e:
            logger.debug("Failed to parse disambiguation_result from cached_results: %s", _e)  # nosec B110

        # Retrieve full row data — either from S3 (preferred) or inline fallback.
        # execute_sql_query stores the result set in S3 to keep row data out of
        # the LLM token stream; we fetch it here for the structured response only.
        columns: List[str] = []
        rows: List[list] = []
        if query_result.get('s3_key'):
            try:
                s3 = get_boto_session().client('s3', region_name=get_region())
                obj = s3.get_object(Bucket=query_result['s3_bucket'], Key=query_result['s3_key'])
                full_data = json.loads(obj['Body'].read().decode('utf-8'))
                columns = full_data.get('columns', [])
                rows = full_data.get('rows', [])
                logger.info(f"invoke: loaded {len(rows)} rows from S3 for structured response")
            except Exception as s3_err:
                logger.warning(f"invoke: could not fetch result from S3: {s3_err}")
        else:
            # Inline fallback (S3 write failed during execute_sql_query)
            columns = query_result.get('columns', query_result.get('column_names', []))
            rows = query_result.get('rows', [])

        # Convert to list-of-dicts (frontend-ready)
        results_list: List[dict] = [
            {col: (row[i] if i < len(row) else '') for i, col in enumerate(columns)}
            for row in rows
        ]

        sql_query: str = query_result.get('sql_query', '')
        n_quads: list = rdf_result.get('n_quads', rdf_result.get('sample_n_quads', []))

        # Build reasoning summary from disambiguation mappings and execution metadata
        mappings: dict = disambiguation_result.get('mappings', {})
        graph_traversal_parts = [
            f"{term} → {info.get('class', '').split('/')[-1]} ({info.get('table', '')})"
            for term, info in mappings.items()
            if info.get('status') == 'CLEAR'
        ]
        graph_traversal = ', '.join(graph_traversal_parts) if graph_traversal_parts else 'Ontology mappings applied'

        execution_id = query_result.get('query_execution_id', '')
        data_source = f"Athena execution: {execution_id}" if execution_id else "Athena"

        return {
            "answer": result_text,
            "sql_query": sql_query,
            "results": results_list,
            "n_quads": n_quads,
            "reasoning": {
                "interpretation": f"Analyzed {disambiguation_result.get('terms_analyzed', 0)} query terms against ontology mappings",
                "graphTraversal": graph_traversal,
                "dataSourceSelection": data_source,
                "sqlQuery": sql_query,
                "summarization": f"Query returned {len(rows)} row(s) across {len(columns)} column(s)",
            },
            "metadata": {
                "executionTimeMs": query_result.get('execution_time_ms', 0),
                "dataScannedBytes": query_result.get('data_scanned_bytes', 0),
            },
        }

    except Exception as e:
        logger.error(f"Error in invoke: {str(e)}")
        return {"error": f"Agent execution failed: {str(e)}"}

if __name__ == '__main__':
    try:
        app.run()
    except Exception as e:
        import traceback
        logger.warning(f"STARTUP FATAL: app.run() failed: {e}\n{traceback.format_exc()}", exc_info=True)
        raise
