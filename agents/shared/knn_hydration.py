"""Lazy hydration helpers for the in-memory KNN index.

Replaces the OSS ``topic-router-rebuild`` EventBridge Lambda with on-demand,
per-namespace hydration triggered by the lookup paths themselves. This keeps
agent cold-start cheap (no pre-warming every namespace) while ensuring a
namespace's first lookup pays the price of building its index once.

The metrics index is a single global ``"metrics"`` index hydrated on first
metric-lookup of any namespace; subsequent calls add new namespaces by
scanning their DDB partition. Per-namespace ``topic-router-<ns>`` indexes
are hydrated on first ``find_candidates`` for that namespace.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from . import knn_index
from .embedding import embed_text

logger = logging.getLogger(__name__)

EMBED_DIM: int = 1024
"""Titan v2 embedding dimension; both indexes are sized to this."""

METRICS_INDEX: str = "metrics"
"""Single shared index for governed metrics across all namespaces."""

_HYDRATED_METRIC_NAMESPACES: set[str] = set()
_HYDRATED_TOPIC_NAMESPACES: set[str] = set()
_HYDRATION_LOCK = threading.RLock()


def reset_for_tests() -> None:
    """Clear hydration tracking. Test-only — production code never calls."""
    with _HYDRATION_LOCK:
        _HYDRATED_METRIC_NAMESPACES.clear()
        _HYDRATED_TOPIC_NAMESPACES.clear()


def hydrate_metrics_namespace(*, namespace: str, ddb_table: Any) -> int:
    """Populate the in-memory ``metrics`` index from DDB for ``namespace``.

    Idempotent — subsequent calls for the same namespace are a no-op. Reads
    every PUBLISHED metric in the namespace, takes the embedding stored on
    the DDB row (written there by the REST API on publish), and upserts.

    Args:
        namespace: Ontology namespace whose metrics to hydrate.
        ddb_table: boto3 DynamoDB Table resource for ``semantic-layer-metrics``.

    Returns:
        Number of metrics indexed (0 if already hydrated or namespace empty).
    """
    from boto3.dynamodb.conditions import Key

    with _HYDRATION_LOCK:
        if namespace in _HYDRATED_METRIC_NAMESPACES:
            return 0
        knn_index.ensure_index(endpoint="", index=METRICS_INDEX, dim=EMBED_DIM)

        resp = ddb_table.query(
            KeyConditionExpression=Key("pk").eq(f"NS#{namespace}")
            & Key("sk").begins_with("METRIC#"),
        )
        indexed = 0
        for item in resp.get("Items", []):
            if item.get("lifecycle") != "PUBLISHED":
                continue
            embedding: Optional[List[float]] = item.get("embedding")
            if embedding is None:
                # PUBLISHED row missing embedding — log and skip; the row
                # will be re-indexed next time it's republished.
                logger.warning(
                    "hydrate_metrics: %s/%s PUBLISHED but embedding missing",
                    namespace, item.get("metric_id"),
                )
                continue
            knn_index.upsert(
                endpoint="", index=METRICS_INDEX,
                doc_id=f"{namespace}:{item['metric_id']}",
                doc={
                    "id": item["metric_id"],
                    "namespace": namespace,
                    "embedding": [float(x) for x in embedding],
                    "text": item.get("name", ""),
                    "metadata": {
                        "name": item.get("name", ""),
                        "version": item.get("version", 1),
                    },
                },
            )
            indexed += 1
        _HYDRATED_METRIC_NAMESPACES.add(namespace)
        logger.info(
            "hydrate_metrics: namespace=%s indexed=%d", namespace, indexed,
        )
        return indexed


def hydrate_topic_router_namespace(
    *,
    namespace: str,
    fetch_iri_metadata: Callable[[str], List[Dict[str, Any]]],
) -> int:
    """Build ``topic-router-<namespace>`` from Neptune class/property metadata.

    Idempotent. Caller injects ``fetch_iri_metadata`` so this module stays
    decoupled from the Neptune SPARQL client — agent runtime passes the
    same SPARQL CONSTRUCT used by ``get_ontology_from_neptune``.

    Args:
        namespace: Ontology namespace.
        fetch_iri_metadata: Callable taking namespace, returning a list of
            ``{iri, label, comment, synonyms, kind}`` dicts.

    Returns:
        Number of IRIs indexed (0 if already hydrated or no IRIs).
    """
    with _HYDRATION_LOCK:
        if namespace in _HYDRATED_TOPIC_NAMESPACES:
            return 0
        index_name = f"topic-router-{namespace}"
        knn_index.ensure_index(endpoint="", index=index_name, dim=EMBED_DIM)

        items = fetch_iri_metadata(namespace)
        indexed = 0
        for it in items:
            text_parts = [it.get("label", ""), it.get("comment", "")] + list(
                it.get("synonyms", []),
            )
            text = " | ".join(p for p in text_parts if p)
            if not text:
                continue
            vec = embed_text(text)
            knn_index.upsert(
                endpoint="", index=index_name,
                doc_id=it["iri"],
                doc={
                    "id": it["iri"],
                    "namespace": namespace,
                    "embedding": vec,
                    "text": text,
                    "metadata": {
                        "kind": it.get("kind", ""),
                        "label": it.get("label", ""),
                        "comment": it.get("comment", ""),
                    },
                },
            )
            indexed += 1
        _HYDRATED_TOPIC_NAMESPACES.add(namespace)
        logger.info(
            "hydrate_topic_router: namespace=%s indexed=%d",
            namespace, indexed,
        )
        return indexed
