"""
Metadata Service for Semantic Layer

This service handles:
- Starting metadata enrichment jobs
- Tracking enrichment progress
- Retrieving enrichment status
- Submitting and polling metadata natural-language queries (async, DynamoDB-backed)

Metadata query architecture (mirrors query_service.py):
  submit_metadata_query()
    → stores {status: processing} in QUERY_RESULTS_TABLE (queryType=metadata)
    → self-invokes Lambda async (_metadata_worker event)
    → returns {queryId, status: processing} immediately

  process_metadata_worker_event()
    → called by async self-invocation
    → invokes metadata query AgentCore runtime
    → writes full result JSON to S3: metadata-query-results/<queryId>/result.json
    → updates DynamoDB: {status: completed, result_s3_key}

  get_metadata_query_status()  → DynamoDB read only (fast)
  get_metadata_query_result()  → DynamoDB → S3 fetch
"""

import json
import logging
import os
import re
import uuid
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List

import boto3
from boto3.dynamodb.conditions import Key

from services.agentcore_service import AgentCoreService
from services.guardrail_service import GuardrailService
from services.ontology_service import convert_decimals

logger = logging.getLogger(__name__)

_RESULT_S3_PREFIX = "metadata-query-results"
_TTL_DAYS = 7


def _version_num(version_str: str) -> int:
    """
    Parse the integer from a version string like 'v1', 'v10'.

    Args:
        version_str: Version string to parse (e.g., 'v1', 'v10', 'v100')

    Returns:
        Integer version number, or 0 if no number found
    """
    m = re.search(r'\d+', version_str or 'v0')
    return int(m.group()) if m else 0


class MetadataService:
    """Service for metadata enrichment and retrieval"""

    def __init__(self):
        """Initialize metadata service with DynamoDB and AgentCore clients"""
        self.region = os.environ.get('AWS_REGION', 'us-east-1')
        self.dynamodb = boto3.resource('dynamodb', region_name=self.region)
        self.metadata_table_name = os.getenv('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
        self.query_table_name = os.getenv('QUERY_RESULTS_TABLE', 'semantic-layer-query-results')
        self.artifacts_bucket = os.getenv('ARTIFACTS_BUCKET', '')
        self._metadata_table = self.dynamodb.Table(self.metadata_table_name)
        self._query_table = None  # lazy init
        self.agentcore_service = AgentCoreService()
        self.guardrail = GuardrailService()

    # ------------------------------------------------------------------
    # Metadata table helpers
    # ------------------------------------------------------------------

    def _get_latest_metadata_item(self, id: str) -> Optional[dict]:
        """Return the metadata item with the highest version for the given id.

        Versions are stored as 'v1', 'v2', … The sort key is a string, so we
        fetch all versions and pick the one whose numeric suffix is largest
        (handles v10, v11, … correctly, unlike a raw lexicographic sort).
        """
        resp = self._metadata_table.query(
            KeyConditionExpression=Key('id').eq(id),
        )
        items = resp.get('Items', [])
        if not items:
            return None

        return max(items, key=lambda item: _version_num(item.get('version', 'v0')))

    def get_metadata_versions(self, id: str) -> list:
        """Return all version records for id, sorted newest-first."""
        resp = self._metadata_table.query(KeyConditionExpression=Key('id').eq(id))
        items = resp.get('Items', [])

        items.sort(key=lambda item: _version_num(item.get('version', 'v0')), reverse=True)
        return convert_decimals([
            {
                'version': i.get('version'),
                'status': i.get('status'),
                'updatedAt': i.get('updatedAt', ''),
            }
            for i in items
        ])

    # ------------------------------------------------------------------
    # DynamoDB helpers (query tracking)
    # ------------------------------------------------------------------

    def _get_query_table(self):
        if self._query_table is None:
            self._query_table = self.dynamodb.Table(self.query_table_name)
        return self._query_table

    @staticmethod
    def _ttl_epoch() -> int:
        return int((datetime.now(timezone.utc) + timedelta(days=_TTL_DAYS)).timestamp())

    def _put_query_item(self, query_id: str, item: dict):
        self._get_query_table().put_item(Item={'queryId': query_id, **item})

    def _get_query_item(self, query_id: str) -> Optional[dict]:
        resp = self._get_query_table().get_item(Key={'queryId': query_id})
        return resp.get('Item')

    def _update_query_item(self, query_id: str, updates: dict):
        set_expr = ', '.join(f'#{k} = :{k}' for k in updates)
        names = {f'#{k}': k for k in updates}
        values = {f':{k}': v for k, v in updates.items()}
        self._get_query_table().update_item(
            Key={'queryId': query_id},
            UpdateExpression=f'SET {set_expr}',
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    # ------------------------------------------------------------------
    # S3 helpers (result payload)
    # ------------------------------------------------------------------

    def _result_s3_key(self, query_id: str) -> str:
        return f"{_RESULT_S3_PREFIX}/{query_id}/result.json"

    def _write_result_to_s3(self, query_id: str, result: dict) -> str:
        s3 = boto3.client('s3', region_name=self.region)
        key = self._result_s3_key(query_id)
        s3.put_object(
            Bucket=self.artifacts_bucket,
            Key=key,
            Body=json.dumps(result, ensure_ascii=False).encode('utf-8'),
            ContentType='application/json',
        )
        logger.info(f"Metadata query result written to s3://{self.artifacts_bucket}/{key}")
        return key

    def _read_result_from_s3(self, s3_key: str) -> dict:
        s3 = boto3.client('s3', region_name=self.region)
        obj = s3.get_object(Bucket=self.artifacts_bucket, Key=s3_key)
        return json.loads(obj['Body'].read().decode('utf-8'))

    def start_metadata_enrichment(
        self, id: str,
        target_tables: Optional[List[str]] = None,
        annotations: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Start a metadata enrichment job driven by an metadata config.

        Reads the full dataSources list (tableName + catalogId per table) from
        the metadata config record in DynamoDB, mirrors the same pattern as the
        ontology agent, and uses the id as the job_id so status can be
        polled with the same ID used throughout the admin workflow.

        Args:
            id: metadata config ID (PK: id / SK: v1 in DynamoDB).
            target_tables: Optional list of 'database.table' names to filter.
            annotations: Optional list of enrichment annotation instructions.

        Returns:
            Dictionary with jobId and status.
        """
        # Read the metadata config to get the explicit table list
        config = self._get_latest_metadata_item(id)
        if not config:
            raise ValueError(f"metadata config not found for id: {id}")

        data_sources = config.get('dataSources', [])
        if not data_sources:
            raise ValueError(f"No dataSources configured for metadata: {id}")

        # Build explicit table list: [{databaseName, tableName, catalogId}]
        # Each entry in dataSources already has these fields from SelectDataSources
        tables = []
        for ds in data_sources:
            if ds.get('databaseName') and ds.get('tableName'):
                table_entry = {
                    'databaseName': ds.get('databaseName', ''),
                    'tableName': ds.get('tableName', ''),
                    'catalogId': ds.get('catalogId') or 'AWSDataCatalog',
                }
                # Preserve optional Glue writeback coords (set for DynamoDB connector tables)
                if ds.get('glueDatabaseName'):
                    table_entry['glueDatabaseName'] = ds['glueDatabaseName']
                if ds.get('glueTableName'):
                    table_entry['glueTableName'] = ds['glueTableName']
                tables.append(table_entry)
        if not tables:
            raise ValueError(f"No valid tables found in dataSources for metadata: {id}")

        if target_tables:
            tables = [
                t for t in tables
                if f"{t['databaseName']}.{t['tableName']}" in target_tables
            ]
            if not tables:
                raise ValueError(f"None of target_tables {target_tables} matched dataSources")

        now = datetime.now(timezone.utc).isoformat()

        # Use the id as job_id (it's a UUID v4 = 36 chars, satisfies ≥33 requirement)
        job_id = id

        # Update the existing metadata config record with enrichment tracking fields
        # (avoids creating a duplicate record — same pattern as metadata agent)
        update_expr = (
            'SET #status = :status, tablesProcessed = :zero, '
            'totalTables = :total, currentTable = :empty, '
            'progressPercent = :zero, enrichmentStartedAt = :now'
        )
        expr_values = {
            ':status': 'processing',
            ':zero': 0,
            ':total': len(tables),
            ':empty': '',
            ':now': now,
        }
        if annotations:
            update_expr += ', enrichmentAnnotations = :annotations'
            expr_values[':annotations'] = annotations

        self._metadata_table.update_item(
            Key={'id': job_id, 'version': config['version']},
            UpdateExpression=update_expr,
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues=expr_values,
        )

        # Invoke metadata agent — reads all config (including enrichmentAnnotations)
        # from DynamoDB using job_id, matching the ontology agent's pattern.
        self.agentcore_service.invoke_metadata_agent(id=job_id)

        return {'jobId': job_id, 'status': 'processing'}

    def start_metadata_revision(
        self, id: str, base_version: str, annotations: list
    ) -> dict:
        """
        Start a versioned revision run.

        Mirrors ontology_service.start_revision_async():
        1. Computes next version from existing records.
        2. Reads v1 (mutable current pointer).
        3. Stamps v1 with revisionMode=True, targetVersion, revisionInstructions.
        4. Invokes the metadata agent asynchronously.
        5. Returns immediately with status 'building'.
        """
        all_versions = self.get_metadata_versions(id)
        highest = max((int(v['version'].lstrip('v')) for v in all_versions
                       if v['version'].startswith('v')), default=1)
        next_version = f'v{highest + 1}'

        # Get current active record (highest version)
        current_config = self._get_latest_metadata_item(id)
        if not current_config:
            raise ValueError(f"metadata config not found: {id}")

        current_config.update({
            'revisionMode': True,
            'revisionBaseVersion': base_version,
            'revisionInstructions': annotations,
            'targetVersion': next_version,
            'status': 'pending',
            'updatedAt': datetime.now(timezone.utc).isoformat(),
        })
        self._metadata_table.put_item(Item=current_config)

        try:
            self.agentcore_service.invoke_metadata_agent(id=id)
        except Exception as agent_error:
            current_config['status'] = 'failed'
            current_config['error'] = str(agent_error)
            current_config['updatedAt'] = datetime.now(timezone.utc).isoformat()
            self._metadata_table.put_item(Item=current_config)
            raise ValueError(f"Failed to start revision agent: {agent_error}") from agent_error

        return {
            'id': id,
            'status': 'building',
            'currentVersion': base_version,
            'nextVersion': next_version,
            'message': f'Revision started. New version {next_version} pending.',
        }

    def get_enrichment_status(self, job_id: str) -> Dict[str, Any]:
        """
        Get the current status of a metadata enrichment job

        Args:
            job_id: The job ID to query

        Returns:
            Dictionary with job status, progress, and metadata

        Raises:
            Exception: If DynamoDB query fails
        """
        item = self._get_latest_metadata_item(job_id) or {}

        def _to_int(val, default=0):
            return int(val) if isinstance(val, Decimal) else (val if val is not None else default)

        return {
            'jobId': item.get('id', job_id),
            'status': item.get('status', 'not_found'),
            'tablesProcessed': _to_int(item.get('tablesProcessed')),
            'totalTables': _to_int(item.get('totalTables')),
            'currentTable': item.get('currentTable', ''),
            'progressPercent': _to_int(item.get('progressPercent')),
            'error': item.get('error'),
        }

    def submit_metadata_query(
        self, question: str, id: str
    ) -> Dict[str, Any]:
        """
        Accept a metadata query and return immediately.
        Processing happens asynchronously via Lambda self-invocation.

        dataSources (tables + catalogIds) are resolved here from the metadata
        config in DynamoDB so that the caller never needs to pass database/catalog
        details — a single metadata layer can span multiple databases.

        Args:
            question: The natural language question
            id: metadata config ID; dataSources are read from DynamoDB

        Returns:
            Dictionary with queryId, question, and status='processing'
        """
        # Resolve dataSources from the metadata config
        config = self._get_latest_metadata_item(id)
        if not config:
            raise ValueError(f"metadata config not found: {id}")

        data_sources = config.get('dataSources', [])
        if not data_sources:
            raise ValueError(f"No dataSources configured for metadata: {id}")

        query_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        self._put_query_item(query_id, {
            'queryType': 'metadata',
            'status': 'processing',
            'question': question,
            'id': id,
            'createdAt': now,
            'updatedAt': now,
            'expires': self._ttl_epoch(),
        })

        function_name = os.environ.get('AWS_LAMBDA_FUNCTION_NAME')
        if function_name:
            boto3.client('lambda', region_name=self.region).invoke(
                FunctionName=function_name,
                InvocationType='Event',
                Payload=json.dumps({
                    '_metadata_worker': True,
                    'query_id': query_id,
                    'question': question,
                    'id': id,
                }).encode(),
            )
            logger.info(f"Async metadata worker invoked for query {query_id}")
        else:
            logger.error("AWS_LAMBDA_FUNCTION_NAME not set — metadata worker cannot be invoked")

        return {
            'queryId': query_id,
            'question': question,
            'status': 'processing',
        }

    def process_metadata_worker_event(self, event: dict):
        """
        Called by the async Lambda self-invocation (_metadata_worker event).
        Runs the AgentCore metadata query, writes result to S3, updates DynamoDB.
        """
        query_id = event['query_id']
        question = event['question']
        id = event['id']

        logger.info(f"Metadata worker started for query {query_id}")
        now = datetime.now(timezone.utc).isoformat()

        # --- INPUT guardrail pre-screen ---
        input_check = self.guardrail.apply(question, source='INPUT')
        if input_check['blocked']:
            logger.warning(f"Metadata query {query_id} blocked by input guardrail")
            self._update_query_item(query_id, {
                'status': 'blocked',
                'error': input_check['message'],
                'updatedAt': datetime.now(timezone.utc).isoformat(),
            })
            return

        try:
            resp = self.agentcore_service.invoke_metadata_query_agent(
                question=question,
                id=id,
            )
            agent_data = resp.get('data', {})

            # agent_data is already parsed JSON from the agent response;
            # if the agent returned plain text it will be in agent_data['result']
            if 'result' in agent_data and len(agent_data) == 1:
                raw = agent_data['result']
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, ValueError):
                    parsed = {'answer': raw}
            else:
                parsed = agent_data

            result = {
                'queryId': query_id,
                'question': question,
                'answer': parsed.get('answer', ''),
                'sql_query': parsed.get('sql_query', ''),
                'results': parsed.get('results', []),
                'n_quads': parsed.get('n_quads', []),
                'reasoning': parsed.get('reasoning', {}),
            }

            # --- OUTPUT guardrail post-screen ---
            answer_text = result.get('answer', '')
            if answer_text:
                output_check = self.guardrail.apply(answer_text, source='OUTPUT')
                if output_check['blocked']:
                    logger.warning(f"Metadata query {query_id} answer blocked by output guardrail")
                    result['answer'] = output_check['message']
                    result['results'] = []
                    result['sql_query'] = ''

            s3_key = self._write_result_to_s3(query_id, result)

            self._update_query_item(query_id, {
                'status': 'completed',
                'result_s3_key': s3_key,
                'updatedAt': datetime.now(timezone.utc).isoformat(),
            })
            logger.info(f"Metadata query {query_id} completed")

        except Exception as e:
            logger.error(f"Metadata worker error for query {query_id}: {e}", exc_info=True)
            self._update_query_item(query_id, {
                'status': 'failed',
                'error': str(e),
                'updatedAt': datetime.now(timezone.utc).isoformat(),
            })

    def get_metadata_query_status(self, query_id: str) -> Dict[str, Any]:
        """
        Fast status check — reads DynamoDB only, no S3 call.

        Args:
            query_id: The query ID to check

        Returns:
            Dictionary with queryId and status
        """
        item = self._get_query_item(query_id)
        if not item:
            return {'queryId': query_id, 'status': 'NOT_FOUND'}
        return {
            'queryId': query_id,
            'status': item.get('status', 'processing'),
            'question': item.get('question', ''),
            'updatedAt': item.get('updatedAt'),
        }

    def get_table_kb_metadata(self, database_name: str, table_name: str, catalog_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Read the AI-enriched metadata document from S3 (written by the metadata agent)
        and return a structured payload: description + columns list.

        S3 key: metadata/{catalog_id}/{database_name}/{table_name}.md
        Falls back to metadata/{database_name}/{table_name}.md for legacy documents.
        """
        if not self.artifacts_bucket:
            return {'success': False, 'error': 'ARTIFACTS_BUCKET not configured'}

        # Primary key matches the path the agent writes (includes catalog_id)
        key = f'metadata/{catalog_id}/{database_name}/{table_name}.md' if catalog_id else f'metadata/{database_name}/{table_name}.md'
        fallback_key = f'metadata/{database_name}/{table_name}.md' if catalog_id else None
        s3 = boto3.client('s3', region_name=self.region)

        def _fetch_key(s3_key: str) -> Optional[str]:
            """Return decoded content or None if the key does not exist."""
            try:
                obj = s3.get_object(Bucket=self.artifacts_bucket, Key=s3_key)
                return obj['Body'].read().decode('utf-8')
            except Exception as e:
                error_code = (e.response.get('Error', {}).get('Code', '')
                              if hasattr(e, 'response') and isinstance(e.response, dict)
                              else '')
                if error_code == 'NoSuchKey' or 'NoSuchKey' in type(e).__name__:
                    return None
                raise

        try:
            content = _fetch_key(key)
            if content is None and fallback_key:
                logger.info(f"Primary key not found, trying fallback: {fallback_key}")
                content = _fetch_key(fallback_key)
            if content is None:
                return {'success': False, 'error': 'Metadata not yet generated for this table'}
            parsed = self._parse_metadata_markdown(content)
            return {
                'success': True,
                'databaseName': database_name,
                'tableName': table_name,
                **parsed,
            }
        except Exception as e:
            logger.error(f"Error reading KB metadata for {database_name}.{table_name}: {e}")
            return {'success': False, 'error': str(e)}

    def _parse_metadata_markdown(self, content: str) -> Dict[str, Any]:
        """
        Parse the structured markdown produced by the metadata agent and return
        a dict with 'description' (str) and 'columns' (list of {name, type, description}).
        """
        import re

        result: Dict[str, Any] = {'description': '', 'columns': []}

        # Extract Overview section (between ## Overview and the next ##)
        # Allow an optional blank line after the heading (LLMs commonly emit one)
        overview = re.search(r'##\s+Overview\s*\n\s*\n?(.*?)(?=\n##\s|\Z)', content, re.DOTALL)
        if overview:
            result['description'] = overview.group(1).strip()

        # Extract Columns markdown table rows
        # Allow an optional blank line between the heading and the table header row
        cols_block = re.search(
            r'##\s+Columns\s*\n\s*\n?\|.*?\|\n\|[-|: ]+\|\n(.*?)(?=\n##\s|\Z)',
            content,
            re.DOTALL,
        )
        if cols_block:
            for line in cols_block.group(1).strip().split('\n'):
                line = line.strip()
                if not line or not line.startswith('|'):
                    continue
                parts = [p.strip() for p in line.strip('|').split('|')]
                if len(parts) >= 2:
                    result['columns'].append({
                        'name': parts[0],
                        'type': parts[1] if len(parts) > 1 else '',
                        'description': parts[2] if len(parts) > 2 else '',
                    })

        return result

    def get_metadata_query_result(self, query_id: str) -> Dict[str, Any]:
        """
        Return the full structured result.
        Fetches from S3 when completed; returns error info when failed.

        Args:
            query_id: The query ID to retrieve

        Returns:
            Dictionary with queryId, status, sql, columns, rows, kbSources,
            kbContext, and error (if any)
        """
        item = self._get_query_item(query_id)
        if not item:
            return {'queryId': query_id, 'status': 'not_found'}

        base = {
            'queryId': query_id,
            'status': item.get('status'),
            'question': item.get('question', ''),
        }

        if item.get('status') == 'completed':
            s3_key = item.get('result_s3_key')
            if s3_key:
                full_result = self._read_result_from_s3(s3_key)
                return {**base, **full_result}
            return {**base, 'answer': '', 'sql_query': '', 'results': [], 'n_quads': [], 'reasoning': {}}

        if item.get('status') == 'failed':
            return {**base, 'error': item.get('error', 'Unknown error'),
                    'answer': '', 'sql_query': '', 'results': [], 'n_quads': [], 'reasoning': {}}

        if item.get('status') == 'blocked':
            return {**base, 'blocked': True, 'error': item.get('error', 'Content blocked by safety policy.'),
                    'answer': '', 'sql_query': '', 'results': [], 'n_quads': [], 'reasoning': {}}

        # still processing
        return {**base, 'answer': '', 'sql_query': '', 'results': [], 'n_quads': [], 'reasoning': {}}
