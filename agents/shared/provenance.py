"""Answer provenance — a uniform, machine-readable source label.

Every query-agent response (Tier 1 governed metric, Tier 2 semantic SQL, VKG, or
advisory) carries a ``provenance`` object built here, and it is threaded into BOTH
chat ``totals`` builders (the fallback path in each agent's ``main`` and the live
streaming path in ``shared/streaming_runner.py``). The shape is intentionally small
and stable so the UI can render a per-tier trust badge and the eval harness can
assert a question routed to the expected tier.
"""
from typing import Dict, List, Optional

# The four answer tiers. ``grounded`` is True for all of them today: even the
# advisory tier grounds its answer in KB metadata + the governed-metric catalog
# (it never answers from the model's parametric world-knowledge). The field is
# kept so the contract stays stable if a gated, ungoverned tier is ever added.
VALID_TIERS = ("governed_metric", "semantic_sql", "vkg", "advisory")


def build_provenance(
    *,
    tier: str,
    sources: List[str],
    degraded: Optional[str] = None,
) -> Dict[str, object]:
    """Build the uniform provenance object for a query-agent response.

    :param tier: The answer tier — one of ``VALID_TIERS``
        (``governed_metric`` | ``semantic_sql`` | ``vkg`` | ``advisory``).
    :param sources: Machine-readable source ids, e.g. ``["metric:revenue_ttm"]``,
        ``["table:coverage", "table:holding"]``, or ``["kb"]``. May be empty.
    :param degraded: The Tier 2/VKG degraded terminal reason (e.g.
        ``"phase3_max_rounds"``) when the run did not produce a grounded answer,
        otherwise ``None``. Mirrors ``WorkflowContext.degraded``.
    :returns: A dict ``{"tier", "grounded", "sources", "degraded"}``.
    :raises ValueError: if ``tier`` is not a recognized tier — fail loud rather
        than emit an unrenderable badge.
    """
    if tier not in VALID_TIERS:
        raise ValueError(
            f"unknown provenance tier {tier!r}; expected one of {VALID_TIERS}"
        )
    return {
        "tier": tier,
        # Hard-coded True: all current tiers are grounded (advisory in metadata,
        # the rest in governed metrics / executed SQL / VKG).
        "grounded": True,
        "sources": list(sources),
        "degraded": degraded,
    }
