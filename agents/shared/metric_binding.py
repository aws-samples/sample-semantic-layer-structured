"""Tier 1 governed-metric parameter binding.

Why this exists
---------------
A metric declares ``supported_filters`` (WHERE predicates) and
``supported_dimensions`` (GROUP BY keys), but before this module nothing ever
produced them: both Tier 1 call sites passed ``filters={}``. So a metric
``net_revenue`` with ``supported_filters=["region"]`` answering "net revenue in
EMEA" ran its unscoped ``compiled_sql`` and returned the GLOBAL number — under a
green "Governed Metric" badge that made the wrong answer look authoritative.

The contract this module enforces: **Tier 1 answers only when its scope is fully
determined.** When the question implicates a declared parameter that cannot be
confidently bound, ``is_fully_bound`` is False and the caller must decline Tier 1
(clarify, or fall through to Tier 2) rather than execute unscoped.

Detection is deterministic — parameter-name and synonym matching plus the
``by X`` / ``per X`` grouping idiom. Value extraction only runs when a name
matched but no literal value is present, so the common case spends no tokens.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Distinguishes "two candidate values found" from "none found". Both are
# unbound, but only the ambiguous case has options worth offering in a
# clarification, so the reason codes must stay distinguishable.
AMBIGUOUS = "__AMBIGUOUS__"

# "by region", "per product_line", "broken down by segment", "grouped by x".
_GROUPING_IDIOM = re.compile(
    r"\b(?:by|per|for\s+each|broken\s+down\s+by|grouped\s+by)\s+"
    r"(?:the\s+)?([a-z0-9_-]+)", re.IGNORECASE,
)


@dataclass
class MetricBinding:
    """Outcome of binding a question's parameters against a metric's declaration.

    :param bound: ``{filter_name: value}`` confidently extracted — becomes WHERE
        predicates via ``metric_executor._apply_filters``.
    :param group_by: Supported dimensions the question asked to break down by.
    :param unbound: ``[{name, kind, reason}]`` for each parameter that is
        implicated by the question but could not be resolved. Non-empty ⇒
        decline Tier 1.
    """

    bound: Dict[str, Any] = field(default_factory=dict)
    group_by: List[str] = field(default_factory=list)
    unbound: List[Dict[str, str]] = field(default_factory=list)

    @property
    def is_fully_bound(self) -> bool:
        """True when Tier 1 may execute: nothing implicated is left unresolved.

        :return: True when ``unbound`` is empty.
        """
        return not self.unbound


def _mentions(*, question_lc: str, name: str) -> bool:
    """Test whether a parameter name appears in the question as a whole word.

    :param question_lc: The user question, already lower-cased.
    :param name: Declared parameter name, e.g. ``"region"`` or ``"product_line"``.
    :return: True when the name (with ``_`` treated as a word gap) appears in
        the question. Word-boundary anchored so ``regionalization`` does not
        match a declared ``region``.
    """
    # "product_line" should match "product line" as well as "product_line".
    parts = [re.escape(p) for p in name.lower().split("_") if p]
    if not parts:
        return False
    pattern = r"\b" + r"[\s_]+".join(parts) + r"\b"
    return re.search(pattern, question_lc) is not None


def _asked_to_group_by(*, question_lc: str, dimension: str) -> bool:
    """Test whether the question used a grouping idiom naming this dimension.

    Only a grouping idiom counts: "revenue by region" requests a breakdown,
    while "EMEA region revenue" names the same word as a filter scope. Treating
    the latter as a GROUP BY would silently change the grain of the answer.

    :param question_lc: The user question, already lower-cased.
    :param dimension: A declared dimension name.
    :return: True when an idiom match resolves to this dimension.
    """
    target = dimension.lower().replace("-", "_")
    return any(
        m.group(1).lower().replace("-", "_") == target
        for m in _GROUPING_IDIOM.finditer(question_lc)
    )


def _extract_value(
    *, question: str, filter_name: str, allowed: Optional[List[str]],
) -> Any:
    """Resolve a literal value for one declared filter, or signal why it can't be.

    Two deterministic strategies, in order:

    1. Authored ``allowed_values`` — the only strategy that can *validate* what
       it found against a governed list.
    2. A quoted literal adjacent to the filter name, e.g. ``region "EMEA"``.

    :param question: The user question with original casing preserved — the
        returned literal must match the authored casing, not the user's, because
        it goes straight into a SQL predicate.
    :param filter_name: The declared filter being resolved.
    :param allowed: Authored allowed values for this filter, or None when the
        metric author did not declare any.
    :return: The resolved value, the ``AMBIGUOUS`` sentinel when more than one
        allowed value appears in the question, or None when nothing resolved.
        Callers MUST treat both None and the sentinel as unbound — never as
        "no filter needed", which is precisely the bug this module fixes.
    """
    if allowed:
        hits = [
            v for v in allowed
            if re.search(r"\b" + re.escape(str(v)) + r"\b", question, re.IGNORECASE)
        ]
        if len(hits) == 1:
            return hits[0]  # authored casing, not the user's
        if len(hits) > 1:
            return AMBIGUOUS
        return None

    # `region "EMEA"` / `region = 'EMEA'` / `region: 'EMEA'` — a value the user
    # quoted or assigned explicitly. Without allowed_values there is nothing to
    # validate against, so only an unambiguous delimited literal is accepted;
    # guessing an undelimited word here would reintroduce the silent-scope bug.
    pattern = (
        r"\b" + re.escape(filter_name) + r"\b\s*(?:=|:|is)?\s*"
        r"""(?:"([^"]+)"|'([^']+)')"""
    )
    m = re.search(pattern, question, re.IGNORECASE)
    if m:
        return m.group(1) or m.group(2)
    return None


def bind_parameters(
    *,
    question: str,
    metric: Any,
    group_by_supported: bool = True,
) -> MetricBinding:
    """Determine which of a metric's declared parameters the question implicates.

    :param question: The natural-language user question.
    :param metric: A ``Metric`` (or duck-typed stand-in) exposing
        ``supported_filters`` and ``supported_dimensions``.
    :param group_by_supported: False when the caller cannot execute a GROUP BY
        (Branch 1, before ``_apply_group_by`` exists). A referenced dimension
        then becomes ``unbound`` with reason ``dimension_unsupported`` rather
        than a silent no-op that returns an ungrouped total.
    :return: A ``MetricBinding``. Values are never guessed here — this pass only
        detects *which* parameters are implicated.
    """
    question_lc = question.lower()
    binding = MetricBinding()

    for dim in (getattr(metric, "supported_dimensions", None) or []):
        if not _asked_to_group_by(question_lc=question_lc, dimension=dim):
            continue
        if group_by_supported:
            binding.group_by.append(dim)
        else:
            binding.unbound.append(
                {"name": dim, "kind": "dimension",
                 "reason": "dimension_unsupported"},
            )

    for filt in (getattr(metric, "supported_filters", None) or []):
        if filt in binding.group_by:
            continue  # "by region" is a grouping request, not a filter request.
        # A filter is implicated when EITHER its name appears ("by customer
        # region") OR one of its authored values appears ("in EMEA"). The second
        # case is the common one — users name the value, not the column — so
        # extraction runs first and a name-mention is only the fallback that
        # turns "named but unresolvable" into an explicit decline.
        allowed = (getattr(metric, "allowed_values", None) or {}).get(filt)
        value = _extract_value(
            question=question, filter_name=filt, allowed=allowed,
        )
        if value == AMBIGUOUS:
            binding.unbound.append(
                {"name": filt, "kind": "filter", "reason": "value_ambiguous"},
            )
        elif value is not None:
            binding.bound[filt] = value
        elif _mentions(question_lc=question_lc, name=filt):
            binding.unbound.append(
                {"name": filt, "kind": "filter",
                 "reason": "value_not_extracted"},
            )
        # else: neither the name nor any value appears — not implicated at all,
        # so the metric runs unscoped, which is correct for this question.

    logger.info(
        "metric_binding: bound=%s group_by=%s unbound=%s",
        binding.bound, binding.group_by, [u["name"] for u in binding.unbound],
    )
    return binding
