"""Answer provenance — a uniform, machine-readable source label.

Every query-agent response (Tier 1 governed metric, Tier 2 semantic SQL, VKG,
Tier 3 agentic, or advisory) carries a ``provenance`` object built here, and it is
threaded into BOTH chat ``totals`` builders (the fallback path in each agent's
``main`` and the live streaming path in ``shared/streaming_runner.py``). The shape
is intentionally small and stable so the UI can render a per-tier trust badge and
the eval harness can assert a question routed to the expected tier.
"""
from typing import Any, Dict, List, Optional

# The answer tiers. ``grounded`` is True for all of them today: even the
# advisory tier grounds its answer in KB metadata + the governed-metric catalog
# (it never answers from the model's parametric world-knowledge), and a governed
# rule grounds in executed SQL PLUS an approved rule. The field is kept so the
# contract stays stable if a gated, ungoverned tier is ever added.
#
# ``governed_rule`` is an answer produced by a PUBLISHED governed rule — a human
# approved both the rule and each numeric bound in it, and the compiled SPARQL
# was smoke-executed at publish time. It is a tier of its own rather than a flag
# on ``governed_metric`` because "an approved conditional rule fired" is a
# materially different operational story from "a governed metric was computed",
# and the monitoring breakdown exists to tell those apart.
#
# ``agentic`` is Tier 3 — the supervisor-loop fallback that runs only after Tier 2
# degrades and whose answer the supervisor judge accepted. It is a tier of its own
# rather than a flag on ``semantic_sql``/``vkg`` because a Tier-3 answer was
# produced by a different mechanism (a free-flowing worker agent, not the
# deterministic graph) and the monitoring breakdown must be able to report how
# much traffic the escalation path is actually rescuing. Note that a recovered
# Tier 3 turn carries ``degraded=None`` — the escalation is recorded by the tier,
# not by the degrade reason, which is cleared once an answer is accepted.
#
# ``document`` is an answer read out of the layer's UPLOADED DOCUMENTS rather than
# out of its data (see ``shared/document_answer.py``). It is distinct from
# ``advisory`` — which answers *about* a layer from its schema metadata — because
# this answers *from* unstructured prose the user supplied, and it carries
# ``doc:<docId>#<n>`` sources naming the passage that grounded each claim. Like
# ``agentic`` it fires only after the SQL path could not serve the question, and
# it likewise carries ``degraded=None`` with ``executed=False``: the tier, not the
# degrade reason, records that no SQL ran. All three layer types emit it.
VALID_TIERS = (
    "governed_metric",
    "governed_rule",
    "semantic_sql",
    "vkg",
    "agentic",
    "advisory",
    "document",
)

# Why a governed metric was surfaced but not used to answer. These strings reach
# the UI verbatim, so they are part of the cross-language contract.
RELATED_REASONS = ("below_threshold", "unbound_parameter", "dimension_unsupported")


def metric_ref(*, metric: Any, score: float, reason: str) -> Dict[str, Any]:
    """Build the display reference for one governed metric.

    Single definition of the metric-card contract, shared by the used-metric
    block and the near-miss list so the two can never drift.

    :param metric: A ``Metric`` (or duck-typed stand-in) exposing ``metric_id``,
        ``name``, ``description``, ``lifecycle``, ``version``, and optionally
        ``owner``.
    :param score: The cosine similarity this metric scored against the question.
    :param reason: Why it is being shown — one of ``RELATED_REASONS`` for a
        near-miss, or ``"used"`` when it answered the question.
    :returns: A JSON-serializable dict. ``owner`` is omitted entirely when the
        metric author did not set one — never filled with a placeholder.
    """
    out: Dict[str, Any] = {
        "metric_id": metric.metric_id,
        "name": metric.name,
        "description": metric.description,
        # ``lifecycle`` may be a ``MetricLifecycle`` enum, which json.dumps
        # cannot serialize — that would break the whole SSE frame over a
        # provenance detail. Unwrap to the plain string value.
        "lifecycle": getattr(metric.lifecycle, "value", metric.lifecycle),
        "version": metric.version,
        "score": score,
        "reason": reason,
    }
    owner = getattr(metric, "owner", None)
    if owner:
        out["owner"] = owner
    return out


def build_provenance(
    *,
    tier: str,
    sources: List[str],
    degraded: Optional[str] = None,
    metric: Optional[Dict[str, Any]] = None,
    related_metrics: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, object]:
    """Build the uniform provenance object for a query-agent response.

    :param tier: The answer tier — one of ``VALID_TIERS``
        (``governed_metric`` | ``governed_rule`` | ``semantic_sql`` | ``vkg`` |
        ``agentic`` | ``advisory`` | ``document``).
    :param sources: Machine-readable source ids, e.g. ``["metric:revenue_ttm"]``,
        ``["rule:RULE-004"]``, ``["table:coverage", "table:holding"]``, or
        ``["kb"]``. May be empty.
    :param degraded: The Tier 2/VKG degraded terminal reason (e.g.
        ``"phase3_max_rounds"``) when the run did not produce a grounded answer,
        otherwise ``None``. Mirrors ``WorkflowContext.degraded``.
    :param metric: The ``metric_ref`` of the governed metric that answered, plus
        the scope it was executed with. Tier 1 only.
    :param related_metrics: ``metric_ref`` entries for governed metrics that were
        surfaced but NOT used, each carrying its ``reason``.
    :returns: A dict ``{"tier", "grounded", "sources", "degraded"}``, plus
        ``metric`` and/or ``related_metrics`` only when those are non-empty. Both
        keys are omitted rather than set to None so legacy turns keep their exact
        historical shape and the UI's null-guards stay valid.
    :raises ValueError: if ``tier`` is not a recognized tier — fail loud rather
        than emit an unrenderable badge.
    """
    if tier not in VALID_TIERS:
        raise ValueError(
            f"unknown provenance tier {tier!r}; expected one of {VALID_TIERS}"
        )
    out: Dict[str, object] = {
        "tier": tier,
        # Hard-coded True: all current tiers are grounded (advisory in metadata,
        # the rest in governed metrics / governed rules / executed SQL / VKG).
        "grounded": True,
        "sources": list(sources),
        "degraded": degraded,
    }
    # Truthiness, not `is not None`: an empty related list would render an empty
    # card, which reads to the user as "we found nothing" rather than "we didn't
    # look" — omit the key so the UI renders no card at all.
    if metric:
        out["metric"] = metric
    if related_metrics:
        out["related_metrics"] = list(related_metrics)
    return out
