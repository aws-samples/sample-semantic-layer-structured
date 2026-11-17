"""Tier 1 admission gate — the single place that decides whether a governed metric
may answer, and with what scope.

Both query agents call this. Before it existed, each agent inlined its own Tier 1
block and passed ``filters={}`` unconditionally, so a metric declaring
``supported_filters=["region"]`` answered "net revenue in EMEA" with the GLOBAL
number under a green "Governed Metric" badge. The two inlined copies had already
drifted apart; keeping the decision here means the fix can't drift again.

The invariant: **Tier 1 answers only when its scope is fully determined**, and it
always states the scope it used. When scope can't be determined the metric is
declined and surfaced as a *related* metric with the reason — the honest version of
"this governed metric exists but couldn't answer as asked".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Dual-import: repo-root callers use ``agents.shared``; the agent container has
# ``shared`` directly on PYTHONPATH (no top-level ``agents`` package).
try:
    from agents.shared.metric_binding import bind_parameters
    from agents.shared.metric_lookup import MetricMatch
    from agents.shared.provenance import metric_ref
except ImportError:  # pragma: no cover — container layout has no `agents` package
    from shared.metric_binding import bind_parameters  # type: ignore
    from shared.metric_lookup import MetricMatch  # type: ignore
    from shared.provenance import metric_ref  # type: ignore

logger = logging.getLogger(__name__)


@dataclass
class Tier1Decision:
    """What the caller should do with a governed-metric lookup result.

    Attributes:
        metric: The metric to execute, or None to fall through to Tier 2. This is the
            ONLY field the routing decision should read — a non-None metric means the
            scope in ``filters``/``group_by`` is fully determined.
        filters: WHERE bindings to pass to ``metric_executor.execute_metric``.
        group_by: Dimensions to break the result down by (Branch 3 executes these).
        provenance_metric: ``metric_ref`` + ``binding``, for ``provenance["metric"]``.
            None when declined.
        related: Refs for metrics that did NOT answer, most relevant first, for
            ``provenance["related_metrics"]``.
    """

    metric: Optional[Any] = None
    filters: Dict[str, Any] = field(default_factory=dict)
    group_by: List[str] = field(default_factory=list)
    provenance_metric: Optional[Dict[str, object]] = None
    related: List[Dict[str, object]] = field(default_factory=list)


def gate_tier1(
    *,
    question: str,
    match: MetricMatch,
    group_by_supported: bool = False,
) -> Tier1Decision:
    """Decide whether the matched governed metric may answer, and with what scope.

    Args:
        question: The natural-language user question.
        match: The result of ``metric_lookup.lookup`` — a matched metric above
            threshold (or None) plus up to two sub-threshold near-misses.
        group_by_supported: Whether the caller can execute a GROUP BY. False in
            Branch 1 (``metric_executor._apply_group_by`` doesn't exist yet), so a
            requested breakdown declines Tier 1 instead of silently ignoring it.
            Branch 3 flips this to True.

    Returns:
        A ``Tier1Decision``. Never raises: a binding error is a *decline*, not a
        crash, because Tier 2 can still answer the question.
    """
    decision = Tier1Decision()

    if match.matched is not None:
        binding = bind_parameters(
            question=question,
            metric=match.matched,
            group_by_supported=group_by_supported,
        )
        if binding.is_fully_bound:
            decision.metric = match.matched
            decision.filters = binding.bound
            decision.group_by = binding.group_by
            decision.provenance_metric = {
                **metric_ref(
                    metric=match.matched, score=match.matched_score, reason="used",
                ),
                "binding": {"bound": binding.bound, "group_by": binding.group_by},
            }
        else:
            # The bug fix. A metric matched the question semantically but its scope
            # could not be determined, so answering would produce a plausible WRONG
            # number under a trust badge. Decline, and say why.
            reason = (
                "dimension_unsupported"
                if any(u["kind"] == "dimension" for u in binding.unbound)
                else "unbound_parameter"
            )
            logger.info(
                "gate_tier1: declining %s (score %.3f) — unbound %s",
                match.matched.metric_id, match.matched_score,
                [u["name"] for u in binding.unbound],
            )
            # Listed first: the metric that almost answered is the most relevant
            # thing to show the user.
            decision.related.append(
                metric_ref(
                    metric=match.matched, score=match.matched_score, reason=reason,
                ),
            )

    for nm in match.near_misses:
        decision.related.append(
            metric_ref(metric=nm.metric, score=nm.score, reason="below_threshold"),
        )

    return decision
