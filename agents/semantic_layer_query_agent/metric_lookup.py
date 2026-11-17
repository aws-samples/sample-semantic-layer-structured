"""Tier 1 governed-metric lookup.

Embeds the question, searches the KNN metrics index, and — when the top
hit is above ``threshold`` — returns the hydrated Metric record from DDB.
Returns None when the question doesn't clearly match a published metric;
caller must fall through to Tier 2.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .embedding import embed_text

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD: float = 0.85
"""Cosine-similarity threshold above which a KNN hit is treated as a match."""


def lookup(*, question: str, namespace: str, ddb_table, knn,
           knn_endpoint: str, knn_index: str,
           threshold: float = DEFAULT_THRESHOLD) -> Optional[Any]:
    """Return the matched Metric (Pydantic model) or None on no clear match.

    Args:
        question: User's natural-language question to embed and match.
        namespace: Ontology namespace; used as an OS server-side filter so
            cross-namespace hits in the top-K cannot mask a real match.
        ddb_table: boto3 ``Table`` resource for ``semantic-layer-metrics``.
        knn: The ``agents.shared.knn_index`` module (or a stand-in with the
            same ``knn_search(endpoint, index, vector, k, filter_terms)``
            signature). Passed in so tests can inject a MagicMock.
        knn_endpoint: OpenSearch Serverless collection endpoint.
        knn_index: Name of the metrics KNN index (e.g. ``"metrics"``).
        threshold: Minimum cosine-similarity score for a hit to count.

    Returns:
        A hydrated ``Metric`` instance when the top hit clears ``threshold``
        and exists in DDB. ``None`` on KNN error, empty index, below-threshold
        scores, or DDB index drift — every Tier-1 miss must fall through.
    """
    # F1 fix: import the shared model — NOT from lambda_rest_api, which is
    # not deployed inside the agent runtime container.
    from .metric_models import Metric

    # Hydrate the in-memory KNN index for this namespace from DDB on first
    # lookup (no-op on subsequent calls). Cheap on cold start because each
    # namespace has at most a handful of PUBLISHED metrics.
    try:
        from . import knn_hydration
        knn_hydration.hydrate_metrics_namespace(
            namespace=namespace, ddb_table=ddb_table,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("metric_lookup: hydration failed (%s) — falling through", e)
        return None

    vec = embed_text(question)
    # F5 fix: pre-filter by namespace inside the KNN query rather than
    # post-filtering top-K. Otherwise cross-namespace hits in the top-K
    # can hide a real namespace match.
    try:
        hits = knn.knn_search(
            endpoint=knn_endpoint, index=knn_index, vector=vec, k=3,
            filter_terms={"namespace": namespace},
        )
    except Exception as e:
        # KNN unavailable must NOT 500. Return None and let the caller
        # fall through to Tier 2.
        logger.warning("metric_lookup: KNN unavailable (%s) — falling through", e)
        return None
    if not hits:
        return None
    top = hits[0]
    if top["score"] < threshold:
        logger.info(
            "metric_lookup: top score %.3f < threshold %.3f — falling through",
            top["score"], threshold,
        )
        return None
    resp = ddb_table.get_item(Key={
        "pk": f"NS#{namespace}",
        "sk": f"METRIC#{top['id']}",
    })
    item = resp.get("Item")
    if item is None:
        # Index drift — KNN had a doc the table doesn't. Treat as miss.
        logger.warning(
            "metric_lookup: KNN hit %s not in DDB — index drift", top["id"],
        )
        return None
    return Metric.from_ddb_item(item)
