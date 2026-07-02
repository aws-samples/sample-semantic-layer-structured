"""Doc-pipeline embedder (item #3).

Calls Bedrock Titan Embed Text v2 in a per-batch loop, attaching a
1024-dim vector to each chunk record. The handler is intentionally pure
once Bedrock is invoked so unit tests can drive ``embed_chunks`` with a
mocked client.

Step Functions invokes this stage with the chunker's output:
    { docId, ontologyId, chunks: [...] }

Returns the same shape with ``embedding`` populated on each chunk and
``embedded`` stage marker set so the indexer can skip embed-failed chunks.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# Titan Embed Text v2 — 1024-dim vectors, 8K-token input cap.
EMBED_MODEL_ID = os.environ.get(
    'EMBED_MODEL_ID', 'amazon.titan-embed-text-v2:0'
)


def embed_chunks(
    *,
    chunks: List[Dict[str, Any]],
    bedrock_runtime: Any,
    model_id: str = EMBED_MODEL_ID,
) -> List[Dict[str, Any]]:
    """Attach an ``embedding`` field to each chunk dict.

    Failed embeddings (Bedrock throttle, oversize input) are skipped — the
    chunk is returned with ``embedding=None`` and ``embedError`` set so the
    indexer can drop it without breaking the whole document.

    Args:
        chunks: List of chunk dicts emitted by the chunker.
        bedrock_runtime: ``boto3.client('bedrock-runtime')`` instance.
        model_id: Bedrock embedding model id.

    Returns:
        The same list with ``embedding`` (and possibly ``embedError``) set.
    """
    import json
    out: List[Dict[str, Any]] = []
    for chunk in chunks:
        text = chunk.get('text', '')
        if not text:
            chunk['embedding'] = None
            chunk['embedError'] = 'empty text'
            out.append(chunk)
            continue
        try:
            response = bedrock_runtime.invoke_model(
                modelId=model_id,
                body=json.dumps({'inputText': text}).encode('utf-8'),
                contentType='application/json',
                accept='application/json',
            )
            body = response.get('body')
            if hasattr(body, 'read'):
                body_bytes = body.read()
            else:
                body_bytes = body
            payload = json.loads(body_bytes.decode('utf-8'))
            chunk['embedding'] = payload.get('embedding')
        except Exception as exc:  # noqa: BLE001 — record per-chunk and continue
            logger.warning(
                "embed failed for chunk %s: %s", chunk.get('chunkId'), exc
            )
            chunk['embedding'] = None
            chunk['embedError'] = str(exc)
        out.append(chunk)
    return out


def handler(event: Dict[str, Any], context=None) -> Dict[str, Any]:
    """Step Functions handler — embeds the chunks in the input event."""
    import boto3

    chunks = event.get('chunks', [])
    bedrock_runtime = boto3.client(
        'bedrock-runtime',
        region_name=os.environ.get('AWS_REGION', 'us-east-1'),
    )
    embedded = embed_chunks(chunks=chunks, bedrock_runtime=bedrock_runtime)
    success = sum(1 for c in embedded if c.get('embedding') is not None)
    return {
        **event,
        'chunks': embedded,
        'embeddedCount': success,
        'embeddedFailedCount': len(embedded) - success,
    }
