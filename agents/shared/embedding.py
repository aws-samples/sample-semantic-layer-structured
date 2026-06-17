"""Bedrock Titan v2 embedding helper shared by Tier 1 metric KNN lookup
and Tier 2 Phase 1 topic router.

Single source of truth for the embedding model id, request shape, and
vector dimension so the index-build path (REST API authoring + the
``topic-router-rebuild`` Lambda) and the query-time path (agent runtime)
can never drift. If the dimension or modelId ever has to change, change
it here once and rebuild every index.
"""

from __future__ import annotations

import json
import os
from typing import List

import boto3

EMBEDDING_MODEL_ID: str = 'amazon.titan-embed-text-v2:0'
"""Canonical Bedrock model id for both Tier 1 and Tier 2 Phase 1."""

_EMBED_DIM: int = 1024
"""Titan v2 default vector dimension. KNN indexes are sized to this."""


def _bedrock_client():
    """Return a fresh ``bedrock-runtime`` boto3 client.

    Lazy module-level constructor so import never reaches the network and
    so unit tests can monkeypatch this single seam.
    """
    return boto3.client(
        'bedrock-runtime',
        region_name=os.getenv('AWS_REGION', 'us-east-1'),
    )


def embed_text(text: str) -> List[float]:
    """Return the Titan v2 embedding for ``text``.

    Args:
        text: The natural-language input to embed. Must be non-empty
            after stripping whitespace.

    Returns:
        A 1024-dimension list of floats representing the Titan v2
        embedding vector for ``text``.

    Raises:
        ValueError: If ``text`` is empty or whitespace-only — silently
            embedding the empty string would land a degenerate vector
            in the KNN index and corrupt every subsequent search.
    """
    if not text or not text.strip():
        raise ValueError('embed_text: input must be non-empty')
    body = json.dumps({'inputText': text, 'dimensions': _EMBED_DIM}).encode()
    resp = _bedrock_client().invoke_model(
        modelId=EMBEDDING_MODEL_ID,
        body=body,
        contentType='application/json',
        accept='application/json',
    )
    payload = json.loads(resp['body'].read())
    return list(payload['embedding'])
