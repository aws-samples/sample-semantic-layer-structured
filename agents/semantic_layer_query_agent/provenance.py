"""Answer provenance — a uniform, machine-readable source label.

Copied-and-owned (per the design's copy-not-import rule) from
``agents/shared/provenance.py``. Every query-agent response (Tier 1 governed
metric, Tier 2 hybrid semantic SQL, or advisory) carries a ``provenance`` object
built here, threaded into the chat ``totals`` builders. The shape is
intentionally small and stable so the UI can render a per-tier trust badge and
the eval harness can assert a question routed to the expected tier.

Isolation note: standard library ONLY — no ``agents.shared`` / sibling-agent
imports (the Hybrid copy-and-own isolation rule).
"""
from __future__ import annotations

from typing import Dict, List, Optional

# The answer tiers. ``grounded`` is True for all of them today: even the
# advisory tier grounds its answer in KB metadata + the governed-metric catalog
# (it never answers from the model's parametric world-knowledge). The field is
# kept so the contract stays stable if a gated, ungoverned tier is ever added.
#
# MUST stay set-equal to ``agents/shared/provenance.py:VALID_TIERS`` — the
# copy-and-own rule copies the code, not a licence for the two lists to diverge.
# ``build_provenance`` below RAISES on an unrecognised tier, so a tier the shared
# module has and this one lacks is a 500 on the hybrid path the moment that tier
# is emitted. A test pins the two tuples equal without importing across the
# isolation boundary.
VALID_TIERS = (
    "governed_metric",
    "governed_rule",
    "semantic_sql",
    "vkg",
    "agentic",
    "advisory",
    "document",
)


def build_provenance(
    *,
    tier: str,
    sources: List[str],
    degraded: Optional[str] = None,
) -> Dict[str, object]:
    """Build the uniform provenance object for a query-agent response.

    Args:
        tier: The answer tier — one of ``VALID_TIERS`` (``governed_metric`` |
            ``semantic_sql`` | ``vkg`` | ``advisory``).
        sources: Machine-readable source ids, e.g. ``["metric:revenue_ttm"]``,
            ``["table:coverage", "table:holding"]``, or ``["kb"]``. May be empty.
        degraded: The Tier 2 degraded terminal reason (e.g.
            ``"grounding_unresolved"``) when the run did not produce a grounded
            answer, otherwise ``None``. Mirrors ``WorkflowContext.degraded``.

    Returns:
        A dict ``{"tier", "grounded", "sources", "degraded"}``.

    Raises:
        ValueError: if ``tier`` is not a recognized tier — fail loud rather than
            emit an unrenderable badge.
    """
    if tier not in VALID_TIERS:
        raise ValueError(
            f"unknown provenance tier {tier!r}; expected one of {VALID_TIERS}"
        )
    return {
        "tier": tier,
        # Hard-coded True: all current tiers are grounded (advisory in metadata,
        # the rest in governed metrics / executed SQL).
        "grounded": True,
        "sources": list(sources),
        "degraded": degraded,
    }
