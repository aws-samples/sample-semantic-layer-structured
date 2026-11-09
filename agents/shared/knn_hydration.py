"""Lazy hydration helpers for the in-memory KNN index.

Replaces the OSS ``topic-router-rebuild`` EventBridge Lambda with on-demand,
per-namespace hydration triggered by the lookup paths themselves. This keeps
agent cold-start cheap (no pre-warming every namespace) while ensuring a
namespace's first lookup pays the price of building its index once.

The metrics index is a single global ``"metrics"`` index hydrated on first
metric-lookup of any namespace; subsequent calls add new namespaces by
scanning their DDB partition. Per-namespace ``topic-router-<ns>`` indexes
are hydrated on first ``find_candidates`` for that namespace. The governed-rule
index is a single global ``"rules"`` index (like metrics, not like the
topic router) hydrated on first rule-lookup of a namespace; because rules have
no DDB projection it embeds at hydration time from an injected Neptune fetch.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from . import knn_index
from .embedding import embed_text
# Shared join helper. Imported rather than re-implemented so index-time text and
# the query-time text built by rule_lookup cannot drift apart (see
# hydrate_rules_namespace).
from .rule_models import embedding_text_for

logger = logging.getLogger(__name__)

EMBED_DIM: int = 1024
"""Titan v2 embedding dimension; both indexes are sized to this."""

METRICS_INDEX: str = "metrics"
"""Single shared index for governed metrics across all namespaces."""

RULES_INDEX: str = "rules"
"""Single shared index for governed rules across all namespaces.

Mirrors ``METRICS_INDEX`` rather than the per-namespace ``topic-router-<ns>``
convention, because rule lookup filters on ``namespace`` server-side (the same
pre-filter approach as ``metric_lookup``), so one index suffices and the lookup
call stays symmetric with Tier 1.
"""

_HYDRATED_METRIC_NAMESPACES: set[str] = set()
_HYDRATED_TOPIC_NAMESPACES: set[str] = set()
_HYDRATED_RULE_NAMESPACES: set[str] = set()
_HYDRATION_LOCK = threading.RLock()


def reset_for_tests() -> None:
    """Clear hydration tracking. Test-only — production code never calls."""
    with _HYDRATION_LOCK:
        _HYDRATED_METRIC_NAMESPACES.clear()
        _HYDRATED_TOPIC_NAMESPACES.clear()
        _HYDRATED_RULE_NAMESPACES.clear()


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


def hydrate_rules_namespace(
    *,
    namespace: str,
    fetch_rules: Callable[[str], List[Dict[str, Any]]],
) -> int:
    """Populate the shared ``rules`` index for ``namespace`` from Neptune.

    Idempotent. Unlike the metrics path there is no DDB projection to read a
    stored vector from — rules live only in Neptune (design §4) — so the caller
    injects ``fetch_rules`` and this function embeds ``name | description |
    synonyms`` at hydration time. Same shape as
    ``hydrate_topic_router_namespace``, and it keeps Neptune the single source of
    truth with no index-drift case to handle.

    Only PUBLISHED rules are indexed: an unpublished rule must be unreachable
    from a user question, which is what makes the review gate meaningful.

    :param namespace: Ontology namespace whose rules to hydrate.
    :param fetch_rules: Callable taking the namespace and returning dicts with
        ``rule_id``, ``name``, ``description``, ``synonyms``, ``lifecycle``.
    :returns: Number of rules indexed (0 if already hydrated or none published).
    """
    with _HYDRATION_LOCK:
        if namespace in _HYDRATED_RULE_NAMESPACES:
            return 0
        knn_index.ensure_index(endpoint="", index=RULES_INDEX, dim=EMBED_DIM)

        indexed = 0
        for row in fetch_rules(namespace):
            if row.get("lifecycle") != "PUBLISHED":
                continue
            # Shared with Rule.embedding_text — index-time and query-time text
            # MUST be built identically or every KNN score silently degrades.
            # Never re-join inline here.
            text = embedding_text_for(
                name=row.get("name", "") or "",
                description=row.get("description", "") or "",
                synonyms=row.get("synonyms", []) or [],
            )
            if not text:
                # No matchable text at all — indexing it would only produce
                # meaningless neighbours. Skip rather than embed an empty string.
                logger.warning(
                    "hydrate_rules: %s/%s PUBLISHED but has no text",
                    namespace, row.get("rule_id"),
                )
                continue
            vec = embed_text(text)
            knn_index.upsert(
                endpoint="", index=RULES_INDEX,
                doc_id=f"{namespace}:{row['rule_id']}",
                doc={
                    "id": row["rule_id"],
                    "namespace": namespace,
                    "embedding": vec,
                    "text": text,
                    "metadata": {"name": row.get("name", "")},
                },
            )
            indexed += 1
        # Marked hydrated even at 0 indexed: a namespace with no published rules
        # must not re-query Neptune on every question.
        _HYDRATED_RULE_NAMESPACES.add(namespace)
        logger.info("hydrate_rules: namespace=%s indexed=%d", namespace, indexed)
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
