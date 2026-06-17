"""Doc-pipeline indexer (item #3).

Final stage — kicks off a Bedrock KB ingestion job so the supplementary-docs
KB picks up the chunks the embedder just produced. Chunks live in S3 under
``supplementary-docs/<ontologyId>/chunks/<docId>/`` and are formatted as
JSONL with one chunk per line so KB's S3 data source can ingest them.

The handler returns ``{ ingestionJobId, status }`` so Step Functions can
include the job id in the document status row for the admin UI.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def write_chunks_to_s3(
    *,
    chunks: List[Dict[str, Any]],
    bucket: str,
    prefix: str,
    s3_client: Any,
) -> str:
    """Write all chunks for one document to a single JSONL object.

    JSONL is the format Bedrock KB's S3 data source ingests for
    "structured" documents. Returns the S3 key written.
    """
    key = f"{prefix.rstrip('/')}/chunks.jsonl"
    body = '\n'.join(json.dumps(c, ensure_ascii=False) for c in chunks)
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body.encode('utf-8'),
        ContentType='application/x-ndjson',
    )
    return key


def kick_off_ingestion(
    *,
    knowledge_base_id: str,
    data_source_id: str,
    bedrock_agent: Any,
) -> str:
    """Start a KB ingestion job. Returns the ingestionJobId."""
    response = bedrock_agent.start_ingestion_job(
        knowledgeBaseId=knowledge_base_id,
        dataSourceId=data_source_id,
    )
    return response.get('ingestionJob', {}).get('ingestionJobId') or ''


def handler(event: Dict[str, Any], context=None) -> Dict[str, Any]:
    """Step Functions handler. Writes the JSONL bundle to S3 and starts the KB
    ingestion. Both calls are best-effort; failures bubble up so the state
    machine retries per its retry policy."""
    import boto3

    chunks = event.get('chunks', [])
    doc_id = event['docId']
    ontology_id = event['ontologyId']
    bucket = os.environ['SUPPLEMENTARY_DOCS_BUCKET']
    prefix = f"supplementary-docs/{ontology_id}/chunks/{doc_id}"

    s3 = boto3.client('s3', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
    key = write_chunks_to_s3(
        chunks=chunks, bucket=bucket, prefix=prefix, s3_client=s3
    )

    kb_id = os.environ.get('SUPPLEMENTARY_DOCS_KB_ID', '')
    ds_id = os.environ.get('SUPPLEMENTARY_DOCS_DS_ID', '')
    ingestion_job_id = ''
    if kb_id and ds_id:
        bedrock_agent = boto3.client(
            'bedrock-agent',
            region_name=os.environ.get('AWS_REGION', 'us-east-1'),
        )
        ingestion_job_id = kick_off_ingestion(
            knowledge_base_id=kb_id,
            data_source_id=ds_id,
            bedrock_agent=bedrock_agent,
        )
    else:
        logger.warning(
            "SUPPLEMENTARY_DOCS_KB_ID / DS_ID not set; skipping ingestion kickoff"
        )
    return {
        **event,
        'jsonlS3Key': key,
        'ingestionJobId': ingestion_job_id,
        'indexed': True,
    }
