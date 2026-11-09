"""Governed-rule lookup, sitting between Tier 1 (metrics) and Tier 2.

Embeds the question, searches the shared ``rules`` KNN index, and returns the
matched PUBLISHED ``Rule`` when the top hit clears ``threshold``. Returns None
whenever the question does not clearly match a published rule.

FAIL-SOFT BY DESIGN — and deliberately the opposite of ``rule_validator``. A
validator is an approval gate, so an error there must fail CLOSED. This is a
query-path optimisation: an error here must fall THROUGH to Tier 2, never 500 and
never block a question. Every ``except`` in this module returns None on purpose;
do not "fix" them into raises.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from .embedding import embed_text

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD: float = 0.85
"""Cosine-similarity threshold, matching ``metric_lookup.DEFAULT_THRESHOLD``."""

TOP_K: int = 3
"""Neighbours requested. Only the top hit can answer; the rest bound the query."""


def lookup(
    *,
    question: str,
    namespace: str,
    knn: Any,
    knn_endpoint: str,
    knn_index: str,
    fetch_rules: Callable[[str], List[Dict[str, Any]]],
    threshold: float = DEFAULT_THRESHOLD,
) -> Optional[Any]:
    """Return the matched PUBLISHED ``Rule``, or None on any miss or error.

    :param question: The user's natural-language question.
    :param namespace: Ontology namespace, used as a server-side KNN filter so
        cross-namespace hits in the top-K cannot mask a real match.
    :param knn: The ``agents.shared.knn_index`` module, or a stand-in with the
        same ``knn_search`` signature. Injected so tests can fake it.
    :param knn_endpoint: OpenSearch Serverless endpoint (empty for in-memory).
    :param knn_index: Index name, normally ``knn_hydration.RULES_INDEX``.
    :param fetch_rules: Callable returning this namespace's rule dicts from
        Neptune. Used for both hydration and rehydrating the matched rule, so a
        cold namespace reads it twice — one extra Neptune SELECT, in exchange for
        Neptune staying the single source of truth with no drift case to reconcile.
    :param threshold: Minimum cosine similarity for a hit to count.
    :returns: A ``Rule`` instance, or None — the caller must fall through.
    """
    # Imported here (not at module scope) to mirror metric_lookup: keeps this
    # module importable in contexts that only need its constants.
    from .rule_models import Rule

    try:
        from . import knn_hydration
        knn_hydration.hydrate_rules_namespace(
            namespace=namespace, fetch_rules=fetch_rules,
        )
    except Exception as exc:  # noqa: BLE001 — fall through, never block
        logger.warning("rule_lookup: hydration failed (%s) — falling through", exc)
        return None

    try:
        vec = embed_text(question)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rule_lookup: embedding failed (%s) — falling through", exc)
        return None

    try:
        hits = knn.knn_search(
            endpoint=knn_endpoint, index=knn_index, vector=vec, k=TOP_K,
            filter_terms={"namespace": namespace},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("rule_lookup: KNN unavailable (%s) — falling through", exc)
        return None
    if not hits:
        return None
    top = hits[0]
    if top["score"] < threshold:
        logger.info(
            "rule_lookup: top score %.3f < threshold %.3f — falling through",
            top["score"], threshold,
        )
        # Unlike metric_lookup there is no near-miss surface: a sub-threshold
        # RULE is not something to show the user. A metric near-miss is a number
        # they might have meant; a rule near-miss is a conditional claim that
        # would read as an assertion about their data.
        return None

    try:
        rows = fetch_rules(namespace)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rule_lookup: rule fetch failed (%s) — falling through", exc)
        return None
    matched = next((r for r in rows if r.get("rule_id") == top["id"]), None)
    if matched is None:
        # Index drift — KNN held a rule the graph no longer returns.
        logger.warning("rule_lookup: KNN hit %s not in graph — drift", top["id"])
        return None
    # Re-check lifecycle at read time. The index only holds PUBLISHED rules, but
    # a rule can be unpublished between hydration and this question, and an
    # unpublished rule must never answer.
    if matched.get("lifecycle") != "PUBLISHED":
        logger.info("rule_lookup: %s no longer PUBLISHED — falling through", top["id"])
        return None
    try:
        return Rule.model_validate(matched)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rule_lookup: %s failed validation (%s)", top["id"], exc)
        return None
