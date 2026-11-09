"""Shared retrieval of unstructured-document chunks, for all three layer types.

This is the read side of the doc-pipeline indexer's write contract
(``lambda/doc-pipeline/indexer/handler.py``). Documents live in the **same**
Bedrock Knowledge Base as the per-table metadata documents and are separated by
a sidecar discriminator rather than by a separate KB:

    metadata/{layerId}/_documents/{docId}.md          ← doc_type = "document"
    metadata/{layerId}/{version}/{cat}/{db}/{tbl}.md  ← no doc_type at all

Two deliberate design points, both about not disturbing the SQL path:

1. **Separate retrieve call.** Documents are fetched by their own filtered
   ``retrieve`` rather than by widening the table retrieval's filter. A single
   call lets document chunks evict table chunks from the top-k — precisely the
   "small lexical-match docs out-rank the table that answers the question"
   failure the semantic-rag data source's chunking comment already records.
   Separate calls give each side an independent ``top_k`` budget.

2. **Classification on read.** A chunk is a document chunk **iff**
   ``doc_type == "document"``. The pre-existing table documents carry no
   ``doc_type``, so they were never re-stamped and need no re-ingestion. And
   because document sidecars carry no ``semantic_layer_version``, they cannot
   satisfy the table path's ``andAll[semantic_layer_id, semantic_layer_version]``
   filter: the SQL path is unchanged by construction.

Used identically by the metadata (Semantic RAG), ontology (VKG) and
semantic-layer (Hybrid) query agents — that identical use is the whole point.

FAIL-SOFT: a retrieval error returns no chunks and lets the caller keep its
existing abstain behaviour. This module sits on the query path *after* the SQL
attempt has already failed, so raising here would turn a graceful abstain into a
500 on a question that was never answerable anyway.

The Bedrock client is **injected, never constructed here**. Each of the three
agents already builds its own session (the Hybrid agent is copy-and-own isolated
and has no ``shared/`` package at all — see ``tests/unit/test_hybrid_isolation``),
so this module has no boto import and its copy inside
``agents/semantic_layer_query_agent/`` is byte-identical to this one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# The sidecar attribute value that marks free-text document prose. Must stay in
# step with ``lambda/doc-pipeline/indexer/handler.DOC_TYPE_DOCUMENT``.
DOC_TYPE_DOCUMENT: str = 'document'

# Default retrieval breadth. Smaller than the table path's 20: document chunks
# are prose passages fed straight to a prose-answer prompt, and an over-wide
# pool dilutes the answer with loosely-related pages.
DEFAULT_TOP_K: int = 8

# Bedrock KB API hard limit on numberOfResults.
MAX_TOP_K: int = 100


@dataclass
class DocumentChunk:
    """One retrieved passage of an uploaded document.

    Attributes:
        document_id: The uploaded document's id, used in provenance sources.
        document_name: Original filename, used in the citation text.
        text: The retrieved passage. Its ``## [file — page N]`` heading is part
            of the text, which is how a citation survives retrieval without a
            second lookup.
        score: The KB relevance score.
        citation: A short human-readable source label parsed out of the heading
            (e.g. ``'underwriting-guide.pdf — page 12'``), or the document name
            when the heading is absent.
    """

    document_id: str
    document_name: str
    text: str
    score: float
    citation: str


def extract_citation(*, text: str, document_name: str) -> str:
    """Parse the ``## [file — location]`` heading out of a retrieved passage.

    The indexer writes one such heading per pipeline chunk, so a retrieved
    passage normally opens with (or contains) one. The first heading in the
    passage is the passage's own source.

    Args:
        text: The retrieved chunk text.
        document_name: Fallback when the passage carries no heading (for
            instance when HIERARCHICAL chunking splits mid-section).

    Returns:
        The citation label, without the markdown or brackets.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('## [') and stripped.endswith(']'):
            return stripped[len('## [') : -1].strip()
    return document_name


def retrieve_document_chunks(
    *,
    kb_id: str,
    layer_id: str,
    question: str,
    client: Any,
    top_k: int = DEFAULT_TOP_K,
) -> List[DocumentChunk]:
    """Retrieve document passages for one semantic layer.

    Args:
        kb_id: Bedrock Knowledge Base id (the semantic-rag KB).
        layer_id: Semantic layer id — scopes retrieval to this layer's documents.
        question: The user's natural-language question.
        client: A ``bedrock-agent-runtime`` client. Required, not defaulted: each
            agent owns its session, and constructing one here would drag a boto
            dependency into the Hybrid agent's isolated package.
        top_k: Maximum passages to return (clamped to the API's 1..100).

    Returns:
        Matching passages, highest-scoring first. Empty when the layer has no
        documents, when nothing matches, or on any retrieval error.

    Raises:
        ValueError: When ``kb_id`` or ``layer_id`` is empty, or ``client`` is
            None. An unscoped retrieve would return another layer's documents,
            so these are programming errors rather than misses — they are raised
            even though retrieval errors are otherwise fail-soft.
    """
    if not kb_id:
        raise ValueError("kb_id is required to retrieve document chunks")
    if not layer_id:
        raise ValueError(
            "layer_id is required to retrieve document chunks; an unfiltered "
            "retrieve would return other layers' documents"
        )
    if client is None:
        raise ValueError("client (bedrock-agent-runtime) is required")

    vector_config: Dict[str, Any] = {
        'numberOfResults': min(max(1, top_k), MAX_TOP_K),
        'filter': {
            'andAll': [
                {'equals': {'key': 'semantic_layer_id', 'value': layer_id}},
                {'equals': {'key': 'doc_type', 'value': DOC_TYPE_DOCUMENT}},
            ]
        },
    }
    try:
        response = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={'text': question},
            retrievalConfiguration={'vectorSearchConfiguration': vector_config},
        )
    except Exception:  # noqa: BLE001 — fail-soft, see the module docstring
        logger.exception(
            'Document retrieval failed for layer %s; continuing without documents',
            layer_id,
        )
        return []

    chunks: List[DocumentChunk] = []
    for result in response.get('retrievalResults', []) or []:
        meta = result.get('metadata', {}) or {}
        # Defence in depth: the server-side filter should already exclude these,
        # but a sidecar-less object (ingested before this contract existed) would
        # otherwise be handed to the prose prompt as if it were a document.
        if meta.get('doc_type') != DOC_TYPE_DOCUMENT:
            continue
        text = (result.get('content', {}) or {}).get('text', '') or ''
        if not text.strip():
            continue
        document_name = str(meta.get('document_name') or '')
        chunks.append(
            DocumentChunk(
                document_id=str(meta.get('document_id') or ''),
                document_name=document_name,
                text=text,
                score=float(result.get('score') or 0.0),
                citation=extract_citation(text=text, document_name=document_name),
            )
        )
    logger.info(
        'Retrieved %d document chunk(s) for layer %s', len(chunks), layer_id
    )
    return chunks
