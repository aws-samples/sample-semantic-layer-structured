"""Tier 1 admission decision: execute scoped, or decline with a stated reason."""
from types import SimpleNamespace

from agents.shared import metric_gate
from agents.shared.metric_lookup import MetricMatch, NearMiss


def _m(metric_id: str = "net_revenue", **kw: object) -> SimpleNamespace:
    """Build a duck-typed Metric with sensible defaults for gate tests.

    :param metric_id: The metric id to report.
    :param kw: Field overrides applied on top of the defaults.
    :return: A SimpleNamespace quacking like a ``Metric``.
    """
    base = dict(
        metric_id=metric_id, name="Net Revenue (TTM)", description="gross - returns",
        lifecycle="PUBLISHED", version=3, supported_filters=[],
        supported_dimensions=[], allowed_values=None,
        # Group-by-safe SQL by default; Task 16's probe parses this for real.
        compiled_sql="SELECT sum(amt) AS total FROM t", dialect="athena",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_no_match_yields_no_decision_and_no_related() -> None:
    """An empty MetricMatch must behave exactly as today's `metric is None`."""
    d = metric_gate.gate_tier1(question="q", match=MetricMatch())
    assert d.metric is None and d.related == []


def test_unparameterized_hit_admits_with_empty_filters() -> None:
    """The common case still executes — this fix must not regress plain metrics."""
    d = metric_gate.gate_tier1(
        question="what is net revenue",
        match=MetricMatch(matched=_m(), matched_score=0.91),
    )
    assert d.metric is not None
    assert d.filters == {} and d.group_by == []
    assert d.provenance_metric["reason"] == "used"


def test_bound_filter_is_admitted_and_stated() -> None:
    """A resolvable filter executes SCOPED and the scope reaches provenance."""
    m = _m(supported_filters=["region"], allowed_values={"region": ["EMEA"]})
    d = metric_gate.gate_tier1(
        question="net revenue in EMEA",
        match=MetricMatch(matched=m, matched_score=0.91),
    )
    assert d.filters == {"region": "EMEA"}
    assert d.provenance_metric["binding"] == {
        "bound": {"region": "EMEA"}, "group_by": [],
    }


def test_unbound_filter_declines_tier1_and_reports_reason() -> None:
    """THE BUG: never return a global number for a scoped question."""
    m = _m(supported_filters=["region"])
    d = metric_gate.gate_tier1(
        question="net revenue for the EMEA region",
        match=MetricMatch(matched=m, matched_score=0.91),
    )
    assert d.metric is None, "declined — Tier 2 must answer instead"
    assert [r["reason"] for r in d.related] == ["unbound_parameter"]
    assert d.related[0]["score"] == 0.91


def test_near_misses_become_related_refs_below_threshold() -> None:
    """A sub-threshold governed metric is visible even though it wasn't used."""
    d = metric_gate.gate_tier1(
        question="q",
        match=MetricMatch(near_misses=[NearMiss(metric=_m(), score=0.83)]),
    )
    assert d.metric is None
    assert d.related == [
        {"metric_id": "net_revenue", "name": "Net Revenue (TTM)",
         "description": "gross - returns", "lifecycle": "PUBLISHED", "version": 3,
         "score": 0.83, "reason": "below_threshold"},
    ]


def test_declined_hit_is_listed_before_near_misses() -> None:
    """The metric that ALMOST answered is the most relevant thing to show first."""
    m = _m(supported_filters=["region"])
    d = metric_gate.gate_tier1(
        question="net revenue for region",
        match=MetricMatch(
            matched=m, matched_score=0.91,
            near_misses=[NearMiss(metric=_m("gross_revenue"), score=0.80)],
        ),
    )
    assert [r["metric_id"] for r in d.related] == ["net_revenue", "gross_revenue"]


def test_dimension_request_declines_with_dimension_reason() -> None:
    """A breakdown the executor cannot perform must decline, not silently flatten.

    With ``group_by_supported=False`` (Branch 1), "by product_line" would
    otherwise return an ungrouped total under a governed badge — the same class
    of silent-wrong-answer as the filter bug.
    """
    m = _m(supported_dimensions=["product_line"])
    d = metric_gate.gate_tier1(
        question="net revenue by product_line",
        match=MetricMatch(matched=m, matched_score=0.92),
        group_by_supported=False,
    )
    assert d.metric is None
    assert [r["reason"] for r in d.related] == ["dimension_unsupported"]


def test_group_by_supported_admits_the_breakdown() -> None:
    """With execution support, the same question is admitted and the grain stated.

    Pins the Branch 3 flip: only the flag changes, not the gate's logic.
    """
    m = _m(supported_dimensions=["product_line"])
    d = metric_gate.gate_tier1(
        question="net revenue by product_line",
        match=MetricMatch(matched=m, matched_score=0.92),
        group_by_supported=True,
    )
    assert d.metric is not None
    assert d.group_by == ["product_line"]
    assert d.provenance_metric["binding"]["group_by"] == ["product_line"]
