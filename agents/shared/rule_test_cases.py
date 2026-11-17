"""Deterministically generate the five mandatory test cases for a rule.

The article's validation stage requires positive, negative, boundary,
missing-data and out-of-scope coverage for every rule. These are generated at
draft time from the rule's own structure — no LLM, so the cases are stable and
a reviewer can reason about them.

The boundary case is the important one: it pins the exact comparison semantics
of the operator at the threshold value, which is where numeric rules silently
go wrong.
"""
from typing import Any, Dict, List, Optional

from agents.shared.rule_models import Condition, Rule, TestCase

# Sentinel subject class for the out-of-scope case. Deliberately not a real
# ontology IRI so it can never accidentally match production data.
OUT_OF_SCOPE_CLASS = "https://semantic-layer.aws/virtual-kg/rules#OutOfScopeProbe"

# Operators for which the threshold value itself satisfies the comparison.
_INCLUSIVE_OPERATORS = frozenset({"=", ">=", "<="})


def _satisfying_value(
    *, condition: Condition, threshold_value: Optional[float],
) -> Any:
    """Return a value that makes ``condition`` true.

    :param condition: The condition to satisfy.
    :param threshold_value: Numeric threshold, or None for literal conditions.
    :returns: A value satisfying the condition.
    """
    if threshold_value is None:
        return condition.value
    if condition.operator in (">", ">="):
        return threshold_value + 1
    if condition.operator in ("<", "<="):
        return threshold_value - 1
    if condition.operator == "!=":
        return threshold_value + 1
    return threshold_value


def _violating_value(
    *, condition: Condition, threshold_value: Optional[float],
) -> Any:
    """Return a value that makes ``condition`` false.

    :param condition: The condition to violate.
    :param threshold_value: Numeric threshold, or None for literal conditions.
    :returns: A value violating the condition.
    """
    if threshold_value is None:
        return f"NOT-{condition.value}"
    if condition.operator in (">", ">="):
        return threshold_value - 1
    if condition.operator in ("<", "<="):
        return threshold_value + 1
    if condition.operator == "!=":
        return threshold_value
    return threshold_value + 1


def _threshold_value(*, condition: Condition, rule: Rule) -> Optional[float]:
    """Resolve a condition's numeric threshold value, if it has one.

    :param condition: The condition to resolve.
    :param rule: Owning rule.
    :returns: The threshold value, or None for literal comparands / unknown ids.
    """
    if condition.threshold_id is None:
        return None
    threshold = rule.threshold_by_id(threshold_id=condition.threshold_id)
    return None if threshold is None else float(threshold.value)


def generate_test_cases(*, rule: Rule) -> List[TestCase]:
    """Generate exactly five test cases for ``rule``, in a fixed order.

    :param rule: The rule to generate coverage for. Must have >= 1 condition.
    :returns: ``[positive, negative, boundary, missing_data, out_of_scope]``.
    :raises ValueError: If the rule has no conditions (a rule with no conditions
        is not a rule; fail loudly rather than emitting vacuous cases).
    """
    if not rule.conditions:
        raise ValueError(f"rule {rule.rule_id} has no conditions to test")
    scope = rule.scope.subject_class
    values = {
        c.binding: _threshold_value(condition=c, rule=rule) for c in rule.conditions
    }

    positive: Dict[str, Any] = {
        c.binding: _satisfying_value(condition=c, threshold_value=values[c.binding])
        for c in rule.conditions
    }
    negative = dict(positive)
    first = rule.conditions[0]
    negative[first.binding] = _violating_value(
        condition=first, threshold_value=values[first.binding]
    )

    boundary = dict(positive)
    first_threshold = values[first.binding]
    # The edge value is the threshold itself, or — for a literal condition —
    # the literal itself. Either way the rule fires only if the operator
    # includes its own boundary, which is exactly the off-by-one this case
    # exists to pin down.
    boundary[first.binding] = (
        first.value if first_threshold is None else first_threshold
    )
    boundary_fires = first.operator in _INCLUSIVE_OPERATORS

    missing = {k: v for k, v in positive.items() if k != first.binding}

    return [
        TestCase(kind="positive", subject_class=scope, inputs=positive,
                 expected_fires=True,
                 rationale="all conditions satisfied"),
        TestCase(kind="negative", subject_class=scope, inputs=negative,
                 expected_fires=False,
                 rationale=f"{first.binding} violates its condition"),
        TestCase(kind="boundary", subject_class=scope, inputs=boundary,
                 expected_fires=boundary_fires,
                 rationale=f"{first.binding} sits exactly on the threshold; "
                           f"operator {first.operator} decides"),
        TestCase(kind="missing_data", subject_class=scope, inputs=missing,
                 expected_fires=False,
                 rationale=f"{first.binding} absent — a rule must not fire on "
                           "an input it does not have"),
        TestCase(kind="out_of_scope", subject_class=OUT_OF_SCOPE_CLASS,
                 inputs=positive, expected_fires=False,
                 rationale="subject is not an instance of the rule's scope class"),
    ]
