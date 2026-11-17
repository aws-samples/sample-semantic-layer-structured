"""Unit tests for Tier 1 metric parameter binding.

The Tier 1 gate must never execute a metric's compiled SQL unscoped when the
question implicates one of the metric's declared filters/dimensions — that
returns a global number under a governed-metric trust badge (silently wrong).
These tests pin the detection + binding contract.
"""
from unittest.mock import MagicMock

from agents.shared import metric_binding


def _metric(**kw) -> MagicMock:
    """Build a duck-typed Metric with sensible parameter defaults.

    :param kw: Field overrides applied on top of the defaults.
    :return: A MagicMock quacking like a ``Metric``.
    """
    defaults = {
        "metric_id": "net_revenue", "name": "Net Revenue (TTM)",
        "description": "Gross minus returns and discounts.",
        "synonyms": [], "supported_filters": [], "supported_dimensions": [],
        "allowed_values": None,
        # Real strings, NOT MagicMock auto-attributes: from Task 16 on,
        # bind_parameters probes the SQL shape via sqlglot, and a MagicMock there
        # raises TypeError rather than the ParseError the probe catches. This
        # shape is group-by-safe.
        "compiled_sql": "SELECT sum(amt) AS total FROM t",
        "dialect": "athena",
    }
    defaults.update(kw)
    return MagicMock(**defaults)


def test_no_declared_parameters_binds_nothing():
    """A metric with no filters/dimensions always executes as-is."""
    out = metric_binding.bind_parameters(
        question="what was net revenue", metric=_metric(),
    )
    assert out.bound == {}
    assert out.group_by == []
    assert out.unbound == []
    assert out.is_fully_bound is True


def test_filter_named_in_question_without_value_is_unbound():
    """'by region' names the filter but supplies no value — must not execute."""
    out = metric_binding.bind_parameters(
        question="what was net revenue by region",
        metric=_metric(supported_filters=["region"]),
    )
    assert out.is_fully_bound is False
    assert [u["name"] for u in out.unbound] == ["region"]


def test_declared_filter_not_referenced_is_not_implicated():
    """An unmentioned filter must NOT block execution."""
    out = metric_binding.bind_parameters(
        question="what was net revenue last quarter",
        metric=_metric(supported_filters=["region"]),
    )
    assert out.is_fully_bound is True
    assert out.unbound == []


def test_dimension_grouping_idiom_is_detected():
    """'by X' where X is a supported dimension requests a GROUP BY."""
    out = metric_binding.bind_parameters(
        question="show net revenue by product_line",
        metric=_metric(supported_dimensions=["product_line"]),
    )
    assert out.group_by == ["product_line"]


def test_dimension_without_groupby_support_is_unbound():
    """Until GROUP BY execution exists, a dimension request declines Tier 1."""
    out = metric_binding.bind_parameters(
        question="show net revenue by product_line",
        metric=_metric(supported_dimensions=["product_line"]),
        group_by_supported=False,
    )
    assert out.is_fully_bound is False
    assert out.unbound[0]["reason"] == "dimension_unsupported"


def test_underscore_name_matches_spaced_phrase():
    """'product line' in prose must match a declared 'product_line' parameter."""
    out = metric_binding.bind_parameters(
        question="net revenue for the enterprise product line",
        metric=_metric(supported_filters=["product_line"]),
    )
    assert [u["name"] for u in out.unbound] == ["product_line"]


def test_substring_does_not_falsely_implicate():
    """'regional' must NOT match a declared 'region' filter (word-boundary)."""
    out = metric_binding.bind_parameters(
        question="what was net revenue for regionalization",
        metric=_metric(supported_filters=["region"]),
    )
    assert out.is_fully_bound is True


def test_quoted_value_after_filter_name_is_bound():
    """`region = "EMEA"`-style phrasing binds without needing allowed_values."""
    out = metric_binding.bind_parameters(
        question='net revenue for region "EMEA"',
        metric=_metric(supported_filters=["region"]),
    )
    assert out.bound == {"region": "EMEA"}
    assert out.is_fully_bound


def test_allowed_value_present_in_question_is_bound():
    """A value from the metric's authored allowed_values is matched anywhere."""
    out = metric_binding.bind_parameters(
        question="net revenue in EMEA last quarter",
        metric=_metric(
            supported_filters=["region"],
            allowed_values={"region": ["EMEA", "AMER"]},
        ),
    )
    assert out.bound == {"region": "EMEA"}
    assert out.is_fully_bound


def test_allowed_value_match_returns_authored_casing():
    """The literal sent to Athena must be the authored value, not the user's casing."""
    out = metric_binding.bind_parameters(
        question="net revenue in emea",
        metric=_metric(
            supported_filters=["region"], allowed_values={"region": ["EMEA"]},
        ),
    )
    assert out.bound == {"region": "EMEA"}


def test_ambiguous_allowed_values_stay_unbound():
    """Two candidate values is a real ambiguity — never pick one."""
    out = metric_binding.bind_parameters(
        question="compare EMEA and AMER net revenue",
        metric=_metric(
            supported_filters=["region"],
            allowed_values={"region": ["EMEA", "AMER"]},
        ),
    )
    assert out.bound == {}
    assert out.unbound == [
        {"name": "region", "kind": "filter", "reason": "value_ambiguous"},
    ]


def test_filter_mentioned_with_no_resolvable_value_stays_unbound():
    """No allowed_values and no quoted literal ⇒ decline, do not answer unscoped."""
    out = metric_binding.bind_parameters(
        question="net revenue by customer region",
        metric=_metric(supported_filters=["region"]),
    )
    assert out.bound == {}
    assert [u["reason"] for u in out.unbound] == ["value_not_extracted"]


def test_allowed_value_not_in_question_stays_unbound():
    """A named filter whose authored values are all absent must not execute."""
    out = metric_binding.bind_parameters(
        question="net revenue for the Atlantis region",
        metric=_metric(
            supported_filters=["region"],
            allowed_values={"region": ["EMEA", "AMER"]},
        ),
    )
    assert out.bound == {}
    assert [u["reason"] for u in out.unbound] == ["value_not_extracted"]


def test_value_alone_implicates_the_filter_without_naming_the_column():
    """The flagship case: users name the value, never the column.

    "net revenue in EMEA" never says "region", so implication cannot depend on
    the filter NAME appearing. This is the exact question from the original bug
    report — if it binds nothing, Tier 1 returns a global number under a
    governed badge.
    """
    out = metric_binding.bind_parameters(
        question="what was net revenue in EMEA last quarter",
        metric=_metric(
            supported_filters=["region"],
            allowed_values={"region": ["EMEA", "AMER", "APAC"]},
        ),
    )
    assert out.bound == {"region": "EMEA"}
    assert out.is_fully_bound is True


def test_neither_name_nor_value_present_runs_unscoped():
    """A filter the question does not touch must not block execution.

    The complement of the test above: over-declining is also a defect, it just
    degrades quietly (every question falls to Tier 2) instead of loudly.
    """
    out = metric_binding.bind_parameters(
        question="what was total net revenue",
        metric=_metric(
            supported_filters=["region"],
            allowed_values={"region": ["EMEA", "AMER"]},
        ),
    )
    assert out.bound == {}
    assert out.is_fully_bound is True
