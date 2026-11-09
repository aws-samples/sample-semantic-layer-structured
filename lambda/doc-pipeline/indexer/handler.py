"""Doc-pipeline indexer (item #3).

Final stage — renders the document into a Knowledge-Base-shaped markdown object
and kicks off a Bedrock KB ingestion job so the chunks become retrievable.

Two things are written:

1. ``supplementary-docs/{ontologyId}/chunks/{docId}/chunks.jsonl`` — the raw
   pipeline output (chunk text, entities, linked classes). This is a debugging /
   provenance artifact; it is deliberately NOT what the KB ingests, because no
   data source covers the ``supplementary-docs/`` prefix.
2. ``metadata/{layerId}/_documents/{docId}.md`` plus its ``.md.metadata.json``
   sidecar — the object the **existing semantic-rag KB** ingests. That prefix is
   already inside its data source's ``inclusionPrefixes``, so documents become
   retrievable with no new infrastructure and no data-source replacement.

The sidecar carries ``doc_type='document'`` and deliberately carries **no**
``semantic_layer_version``. The table-retrieval path filters on
``andAll[semantic_layer_id, semantic_layer_version]``, so a chunk without a
version attribute cannot satisfy that filter: document chunks are provably
invisible to the SQL path rather than merely tested to be. See
``agents/shared/document_retrieval.py`` for the read side.

One ``.md`` per document (not one object per chunk — a 50 MB PDF would be
~25 000 objects) with each pipeline chunk as a ``## [file — location]`` section.
The KB's proven HIERARCHICAL chunking splits it, and the heading keeps the
citation recoverable from the retrieved chunk text itself.

The handler returns ``{ ingestionJobId, status }`` so Step Functions can
include the job id in the document status row for the admin UI.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Bedrock Knowledge Base hard limit on the companion ``.metadata.json`` sidecar.
# A sidecar over this size is SILENTLY dropped at ingestion (surfaced only as a
# job-level "Ignored N files …" failureReason), taking the whole document out of
# the KB. Copied from kb_builder.KB_METADATA_MAX_BYTES.
KB_METADATA_MAX_BYTES: int = 1024

# The sidecar discriminator that marks a chunk as free-text document prose
# rather than a per-table metadata document. Classification is on READ: the 24
# pre-existing table documents carry no ``doc_type`` at all, so they are
# untouched and need no re-ingestion. Must stay in step with
# ``agents/shared/document_retrieval.DOC_TYPE_DOCUMENT``.
DOC_TYPE_DOCUMENT: str = 'document'


def kb_document_key(*, ontology_id: str, doc_id: str) -> str:
    """Build the S3 key of a document's KB markdown object.

    The ``metadata/`` root is required: it is what the semantic-rag data source's
    ``inclusionPrefixes`` covers. ``_documents/`` sits where a version segment
    would be for a table document, which keeps documents from ever colliding
    with a ``metadata/{id}/{version}/…`` table key.

    Args:
        ontology_id: The semantic-layer id scoping the document.
        doc_id: The uploaded document's id.

    Returns:
        The S3 object key for the ``.md`` document.
    """
    return f"metadata/{ontology_id}/_documents/{doc_id}.md"


def format_source_label(*, source_location: Dict[str, Any], chunk_index: int) -> str:
    """Render a chunk's ``source_location`` as a human-readable citation label.

    Args:
        source_location: The chunker's provenance dict (``{'type': 'pdf',
            'page': 12}`` and friends). May be empty for older chunks.
        chunk_index: The chunk's ordinal, used as the fallback label so every
            chunk is citable even without provenance.

    Returns:
        A short label such as ``'page 12'``, ``'Exclusions'`` or ``'part 3'``.
    """
    loc_type = source_location.get('type', '')
    if loc_type == 'pdf' and source_location.get('page'):
        return f"page {source_location['page']}"
    # Markdown and DOCX both carry a section heading when the document has one.
    if source_location.get('section'):
        return str(source_location['section'])
    if loc_type == 'docx' and source_location.get('block'):
        return f"block {source_location['block']}"
    return f"part {chunk_index + 1}"


def render_document_markdown(
    *, chunks: List[Dict[str, Any]], document_name: str
) -> str:
    """Render the pipeline's chunks as one KB-ingestable markdown document.

    Each chunk becomes a section whose heading carries its citation, so a
    retrieved chunk names its own source file and page without a second lookup.

    Args:
        chunks: Chunk records from the pipeline (``text`` + ``sourceLocation``).
        document_name: The original upload filename, used in every heading and
            as the document title.

    Returns:
        The markdown body to write to S3.

    Raises:
        ValueError: When no chunk carries any text. An empty KB document would
            ingest cleanly and answer nothing, which is the silent failure this
            whole path exists to remove.
    """
    sections: List[str] = [f"# {document_name}", '']
    wrote_any = False
    for index, chunk in enumerate(chunks):
        text = (chunk.get('text') or '').strip()
        if not text:
            continue
        label = format_source_label(
            source_location=chunk.get('sourceLocation') or {}, chunk_index=index
        )
        sections.append(f"## [{document_name} — {label}]")
        sections.append('')
        sections.append(text)
        sections.append('')
        wrote_any = True

    if not wrote_any:
        raise ValueError(
            f"no chunk of {document_name!r} carries any text; refusing to write "
            f"an empty KB document that would ingest cleanly and answer nothing"
        )
    return '\n'.join(sections)


def build_document_sidecar(
    *, ontology_id: str, doc_id: str, document_name: str
) -> bytes:
    """Build the ``.md.metadata.json`` sidecar body for a document.

    Args:
        ontology_id: The semantic-layer id scoping the document (the retrieval
            filter key).
        doc_id: The uploaded document's id, used in citations.
        document_name: The original upload filename.

    Returns:
        The encoded sidecar JSON.

    Raises:
        ValueError: When required scope fields are missing, or when the sidecar
            exceeds the Bedrock KB size limit — over the limit the KB drops the
            whole document silently, so this fails loudly instead.
    """
    if not ontology_id or not doc_id:
        raise ValueError(
            "ontology_id and doc_id are required to scope the KB document"
        )
    attributes = {
        'semantic_layer_id': ontology_id,
        # NO semantic_layer_version — see the module docstring. This omission is
        # what makes document chunks invisible to the table-retrieval filter.
        'doc_type': DOC_TYPE_DOCUMENT,
        'document_id': doc_id,
        'document_name': document_name,
    }
    body = json.dumps({'metadataAttributes': attributes}).encode('utf-8')
    if len(body) > KB_METADATA_MAX_BYTES:
        raise ValueError(
            f"KB metadata sidecar for {document_name!r} is {len(body)} bytes, "
            f"over the Bedrock KB limit of {KB_METADATA_MAX_BYTES}; the document "
            f"would be silently dropped at ingestion. Shorten the filename."
        )
    return body


def write_kb_document(
    *,
    chunks: List[Dict[str, Any]],
    ontology_id: str,
    doc_id: str,
    document_name: str,
    bucket: str,
    s3_client: Any,
) -> str:
    """Write the document's KB markdown object and sidecar to S3.

    The sidecar is built and size-validated BEFORE either object is written, so
    a pathological filename can never orphan a ``.md`` with no sidecar (a
    sidecar-less object is ingested unfiltered and would leak across layers).

    Args:
        chunks: Chunk records from the pipeline.
        ontology_id: The semantic-layer id scoping the document.
        doc_id: The uploaded document's id.
        document_name: The original upload filename.
        bucket: The artifacts bucket the semantic-rag data source reads.
        s3_client: A boto3 S3 client (injected so tests need no AWS).

    Returns:
        The S3 key of the written ``.md`` document.

    Raises:
        ValueError: Propagated from the sidecar guard or an empty render.
    """
    sidecar_body = build_document_sidecar(
        ontology_id=ontology_id, doc_id=doc_id, document_name=document_name
    )
    markdown = render_document_markdown(chunks=chunks, document_name=document_name)
    key = kb_document_key(ontology_id=ontology_id, doc_id=doc_id)

    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=markdown.encode('utf-8'),
        ContentType='text/markdown',
    )
    s3_client.put_object(
        Bucket=bucket,
        Key=f"{key}.metadata.json",
        Body=sidecar_body,
        ContentType='application/json',
    )
    logger.info(
        'Wrote KB document s3://%s/%s (%d chars, layer=%s doc=%s)',
        bucket, key, len(markdown), ontology_id, doc_id,
    )
    return key


def write_chunks_to_s3(
    *,
    chunks: List[Dict[str, Any]],
    bucket: str,
    prefix: str,
    s3_client: Any,
) -> str:
    """Write all chunks for one document to a single JSONL object.

    This is the pipeline's provenance artifact (chunk text, NER entities, linked
    classes) — it is NOT what the KB ingests; see :func:`write_kb_document`.
    Returns the S3 key written.
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
    """Step Functions handler.

    Writes the JSONL provenance bundle, the KB markdown document + sidecar, and
    starts the KB ingestion job. Failures bubble up so the state machine retries
    per its retry policy.

    Args:
        event: Previous stage's output; must carry ``docId``, ``ontologyId`` and
            ``chunks``. ``filename`` is used as the document title when present.
        context: Unused Lambda context.

    Returns:
        The event plus ``jsonlS3Key``, ``kbDocumentS3Key``, ``ingestionJobId``
        and ``indexed``.

    Raises:
        RuntimeError: When the KB id or data source id is unset. This was a
            warning-and-skip, which is exactly what hid the fact that no
            document has ever reached a Knowledge Base.
    """
    import boto3

    chunks = event.get('chunks', [])
    doc_id = event['docId']
    ontology_id = event['ontologyId']
    document_name = event.get('filename') or doc_id
    bucket = os.environ['SUPPLEMENTARY_DOCS_BUCKET']
    prefix = f"supplementary-docs/{ontology_id}/chunks/{doc_id}"

    kb_id = os.environ.get('SUPPLEMENTARY_DOCS_KB_ID', '')
    ds_id = os.environ.get('SUPPLEMENTARY_DOCS_DS_ID', '')
    if not kb_id or not ds_id:
        raise RuntimeError(
            "SUPPLEMENTARY_DOCS_KB_ID and SUPPLEMENTARY_DOCS_DS_ID must both be "
            "set; without them the document is written to S3 but never ingested, "
            "so it is silently unretrievable. Check that cdk/bin/app.ts passes "
            "supplementaryDocsKbId / supplementaryDocsDataSourceId to "
            "DocPipelineStack."
        )

    s3 = boto3.client('s3', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
    key = write_chunks_to_s3(
        chunks=chunks, bucket=bucket, prefix=prefix, s3_client=s3
    )
    kb_key = write_kb_document(
        chunks=chunks,
        ontology_id=ontology_id,
        doc_id=doc_id,
        document_name=document_name,
        bucket=bucket,
        s3_client=s3,
    )

    bedrock_agent = boto3.client(
        'bedrock-agent',
        region_name=os.environ.get('AWS_REGION', 'us-east-1'),
    )
    ingestion_job_id = kick_off_ingestion(
        knowledge_base_id=kb_id,
        data_source_id=ds_id,
        bedrock_agent=bedrock_agent,
    )
    return {
        **event,
        'jsonlS3Key': key,
        'kbDocumentS3Key': kb_key,
        'ingestionJobId': ingestion_job_id,
        'indexed': True,
    }
