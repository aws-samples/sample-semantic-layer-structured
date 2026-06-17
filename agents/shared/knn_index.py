"""In-memory cosine-similarity KNN used by Tier 1 metric lookup and
Tier 2 Phase 1 topic router.

Keeps the OpenSearch-Serverless contract that callers expect (``ensure_index``,
``upsert``, ``knn_search``, ``delete``) but stores everything in a process-local
dict. AgentCore runtime instances rebuild their index on cold start from the
authoritative source (DDB metrics table or Neptune SPARQL CONSTRUCT) — see
``agents.shared.knn_hydration``.

The ``endpoint`` parameter is accepted on every call for migration
compatibility with the OSS-era call sites and is intentionally ignored.
"""
from __future__ import annotations

import logging
import math
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class IndexNotFoundError(Exception):
    """Raised by ``knn_search`` when the index has not been created yet.

    Callers (e.g. ``VkgTopicRouter``) catch this to fall back to a lexical
    Neptune match during the cold-start window before hydration completes.
    """


# Module-level store: index_name -> {doc_id -> doc dict}.
# A lock guards concurrent upsert/delete from background hydration tasks
# while reads (search) are also synchronized — OK at this volume.
_STORE: Dict[str, Dict[str, Dict[str, Any]]] = {}
_INDEX_DIM: Dict[str, int] = {}
_LOCK = threading.RLock()


def reset_for_tests() -> None:
    """Clear all in-memory state. Test-only — production code never calls."""
    with _LOCK:
        _STORE.clear()
        _INDEX_DIM.clear()


def ensure_index(endpoint: str, index: str, *, dim: int) -> None:
    """Create the index if missing. Idempotent. ``endpoint`` is ignored."""
    del endpoint
    with _LOCK:
        if index not in _STORE:
            _STORE[index] = {}
            _INDEX_DIM[index] = dim
            logger.info("knn_index: created in-memory index '%s' dim=%d", index, dim)
        elif _INDEX_DIM[index] != dim:
            raise ValueError(
                f"index '{index}' already exists with dim={_INDEX_DIM[index]}, "
                f"cannot re-create with dim={dim}"
            )


def upsert(endpoint: str, index: str, *, doc_id: str, doc: Dict[str, Any]) -> None:
    """Insert or replace a single document.

    Required fields on ``doc``: ``embedding`` (list[float]). Other fields
    (``id``, ``namespace``, ``text``, ``metadata``, ...) are persisted
    verbatim and surfaced on search hits.
    """
    del endpoint
    if "embedding" not in doc:
        raise ValueError(f"upsert: doc missing 'embedding' field (doc_id={doc_id})")
    with _LOCK:
        if index not in _STORE:
            raise IndexNotFoundError(f"index not found: {index}")
        expected_dim = _INDEX_DIM[index]
        emb = doc["embedding"]
        if len(emb) != expected_dim:
            raise ValueError(
                f"upsert: embedding dim {len(emb)} != index dim {expected_dim}"
            )
        # Store a shallow copy so caller mutations don't bleed into the index.
        _STORE[index][doc_id] = dict(doc)


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity in pure Python; assumes equal length (validated upstream)."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom == 0.0:
        return 0.0
    return dot / denom


def knn_search(
    *,
    endpoint: str,
    index: str,
    vector: List[float],
    k: int = 5,
    filter_terms: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Top-K cosine search over the in-memory index.

    Returns ``[{score, ...source fields}]`` sorted by descending score.
    ``filter_terms`` is an exact-match pre-filter (``{field: value}``) used
    by Tier 1 metric lookup to scope KNN to a single namespace.

    Raises:
        IndexNotFoundError: when ``index`` has not been created. Callers
            interpret this as "cold-start window" and fall back appropriately.
    """
    del endpoint
    with _LOCK:
        if index not in _STORE:
            raise IndexNotFoundError(f"index not found: {index}")
        bucket = _STORE[index]
        if not bucket:
            return []

        scored: List[Dict[str, Any]] = []
        for doc in bucket.values():
            if filter_terms:
                if not all(doc.get(fk) == fv for fk, fv in filter_terms.items()):
                    continue
            score = _cosine(vector, doc["embedding"])
            item = {key: val for key, val in doc.items() if key != "embedding"}
            item["score"] = score
            scored.append(item)

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:k]


def delete(*, endpoint: str, index: str, doc_id: str) -> None:
    """Idempotent delete — missing doc_id is a no-op (matches OSS ignore=[404])."""
    del endpoint
    with _LOCK:
        if index not in _STORE:
            return
        _STORE[index].pop(doc_id, None)
