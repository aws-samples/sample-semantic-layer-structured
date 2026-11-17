"""Tier 1 governed-metric lookup.

Embeds the question, searches the KNN metrics index, and returns a
``MetricMatch`` describing the outcome: the hydrated Metric when the top hit
clears ``threshold``, otherwise the sub-threshold near-misses so the caller can
tell the user a governed metric exists and why it wasn't used.

Never returns None and never raises — every failure mode (hydration error, KNN
unavailable, empty index, below threshold, DDB index drift) yields an empty
``MetricMatch`` and the caller falls through to Tier 2.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List

from .embedding import embed_text

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD: float = 0.85
"""Cosine-similarity threshold above which a KNN hit is treated as a match."""

RELATED_FLOOR: float = 0.60
"""Floor below which a sub-threshold metric is noise.

Showing a 0.42-scoring metric as "related" damages trust rather than building
it — the user reads it as the system not understanding the question.
"""

NEAR_MISS_LIMIT: int = 2
"""Most near-misses carried on a MetricMatch. Bounded so the card stays scannable."""


@dataclass(frozen=True)
class NearMiss:
    """A governed metric that scored below threshold — visible but not used.

    :param metric: The hydrated ``Metric`` model.
    :param score: The cosine similarity it actually scored.
    """

    metric: Any
    score: float


@dataclass(frozen=True)
class MetricMatch:
    """The outcome of a Tier 1 lookup.

    :param matched: The hydrated ``Metric`` when the top hit cleared the
        threshold and exists in DDB; None on every miss path.
    :param matched_score: Cosine similarity of ``matched``; 0.0 when no match.
    :param near_misses: Sub-threshold metrics at or above ``RELATED_FLOOR``,
        best first, capped at ``NEAR_MISS_LIMIT``. Empty when ``matched`` is
        set — an answered metric's runners-up are not surfaced.
    """

    matched: Any = None
    matched_score: float = 0.0
    near_misses: List[NearMiss] = field(default_factory=list)


def lookup(*, question: str, namespace: str, ddb_table, knn,
           knn_endpoint: str, knn_index: str,
           threshold: float = DEFAULT_THRESHOLD) -> MetricMatch:
    """Return a MetricMatch describing the Tier 1 lookup outcome.

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
        A ``MetricMatch``. ``matched`` is set only when the top hit clears
        ``threshold`` and exists in DDB. On every miss path ``matched`` is None
        and ``near_misses`` carries whatever governed metrics scored at or above
        ``RELATED_FLOOR``, so the caller can show the user that a governed
        metric exists without using it. Never None, never raises.
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
        return MetricMatch()

    def _hydrate(metric_id: str) -> Any:
        """Load one metric from DDB, or None on index drift.

        :param metric_id: The metric id from the KNN hit.
        :return: A hydrated ``Metric``, or None when the table has no such item.
        """
        resp = ddb_table.get_item(Key={
            "pk": f"NS#{namespace}",
            "sk": f"METRIC#{metric_id}",
        })
        item = resp.get("Item")
        if item is None:
            # Index drift — KNN had a doc the table doesn't.
            logger.warning(
                "metric_lookup: KNN hit %s not in DDB — index drift", metric_id,
            )
            return None
        return Metric.from_ddb_item(item)

    def _near_misses(candidate_hits: List[dict]) -> List[NearMiss]:
        """Hydrate sub-threshold hits worth showing the user.

        :param candidate_hits: KNN hits that did not clear the threshold.
        :return: Up to ``NEAR_MISS_LIMIT`` NearMiss entries, best score first.
            A hit that fails to hydrate is dropped — a display concern must
            never break the answer.
        """
        out: List[NearMiss] = []
        for hit in candidate_hits:
            if len(out) >= NEAR_MISS_LIMIT:
                break
            if hit["score"] < RELATED_FLOOR:
                continue
            try:
                metric = _hydrate(hit["id"])
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "metric_lookup: near-miss %s failed to hydrate (%s) — dropped",
                    hit["id"], e,
                )
                continue
            if metric is not None:
                out.append(NearMiss(metric=metric, score=float(hit["score"])))
        return out

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
        # KNN unavailable must NOT 500. Return an empty match and let the
        # caller fall through to Tier 2.
        logger.warning("metric_lookup: KNN unavailable (%s) — falling through", e)
        return MetricMatch()
    if not hits:
        return MetricMatch()
    top = hits[0]
    if top["score"] < threshold:
        logger.info(
            "metric_lookup: top score %.3f < threshold %.3f — falling through",
            top["score"], threshold,
        )
        # Below threshold: keep the hits so the caller can say a governed
        # metric exists and wasn't used. This is the whole point of MetricMatch.
        return MetricMatch(near_misses=_near_misses(hits))
    matched = _hydrate(top["id"])
    if matched is None:
        # Index drift on the winner. Everything after it is still a candidate.
        return MetricMatch(near_misses=_near_misses(hits[1:]))
    return MetricMatch(matched=matched, matched_score=float(top["score"]))
