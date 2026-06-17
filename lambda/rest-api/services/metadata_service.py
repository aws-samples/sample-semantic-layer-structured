"""
Metadata Service for Semantic Layer

This service handles:
- Starting metadata enrichment jobs
- Tracking enrichment progress
- Retrieving enrichment status
- Reading AI-enriched per-table metadata from S3 (Bedrock KB source of truth)

Natural-language metadata querying runs over the streaming AG-UI chat path
(chat AgentCore Gateway → metadata query AgentCore runtime). This service no
longer exposes an async submit/status/result surface.
"""

import logging
import os
import re
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import boto3
from boto3.dynamodb.conditions import Key

from services.agentcore_service import AgentCoreService
from services.guardrail_service import GuardrailService
from services.ontology_service import convert_decimals

logger = logging.getLogger(__name__)


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
        self.artifacts_bucket = os.getenv('ARTIFACTS_BUCKET', '')
        self._metadata_table = self.dynamodb.Table(self.metadata_table_name)
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

    def get_table_kb_metadata(
        self,
        database_name: str,
        table_name: str,
        catalog_id: str,
        semantic_layer_id: str,
        semantic_layer_version: str,
    ) -> Dict[str, Any]:
        """
        Read the AI-enriched metadata document from S3 (written by the metadata agent)
        and return a structured payload: description + columns list.

        S3 key (matches save_metadata_document_to_s3 in agents/metadata_agent/main.py):
          metadata/{semantic_layer_id}/{semantic_layer_version}/{catalog_id}/{database_name}/{table_name}.md
        """
        if not self.artifacts_bucket:
            raise ValueError('ARTIFACTS_BUCKET not configured')

        key = (
            f'metadata/{semantic_layer_id}/{semantic_layer_version}/'
            f'{catalog_id}/{database_name}/{table_name}.md'
        )
        s3 = boto3.client('s3', region_name=self.region)

        try:
            obj = s3.get_object(Bucket=self.artifacts_bucket, Key=key)
            content = obj['Body'].read().decode('utf-8')
        except Exception as e:
            error_code = (e.response.get('Error', {}).get('Code', '')
                          if hasattr(e, 'response') and isinstance(e.response, dict)
                          else '')
            if error_code == 'NoSuchKey' or 'NoSuchKey' in type(e).__name__:
                logger.info(f"KB metadata key not found: {key}")
                return {'success': False, 'error': 'Metadata not yet generated for this table'}
            logger.error(f"Error reading KB metadata for {database_name}.{table_name}: {e}")
            return {'success': False, 'error': str(e)}

        logger.info(f"Loaded KB metadata from {key}")
        parsed = self._parse_metadata_markdown(content)
        return {
            'success': True,
            'databaseName': database_name,
            'tableName': table_name,
            **parsed,
        }

    def _parse_metadata_markdown(self, content: str) -> Dict[str, Any]:
        """
        Parse the structured markdown produced by the metadata agent and return
        a dict with 'description' (str), 'columns' (list of {name, type,
        description}), and 'sections' — an ordered list of every ``## <Heading>``
        block ({title, body}) so the UI can render the FULL curated document
        (Business Purpose, Business Concepts, Reference Tables, Common Query
        Patterns, ACORD Source Path, Notes, …), not just Overview + Columns.
        """
        import re

        result: Dict[str, Any] = {'description': '', 'columns': [], 'sections': []}

        # Extract every "## Heading\n body" block in document order. The leading
        # "# <title>" H1 (the table id) is intentionally skipped (it's redundant
        # with the table name already shown in the UI).
        for m in re.finditer(r'^##\s+(.+?)\s*\n(.*?)(?=^##\s|\Z)', content,
                              re.DOTALL | re.MULTILINE):
            title = m.group(1).strip()
            body = m.group(2).strip()
            if title:
                result['sections'].append({'title': title, 'body': body})

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
