"""Deterministic generation of the five mandatory rule test cases."""
from agents.shared.rule_models import (
    Condition, Conclusion, Rule, RuleScope, Threshold,
)
from agents.shared.rule_test_cases import generate_test_cases


def _rule(**over) -> Rule:
    base = dict(
        rule_id="RULE-004", namespace="ont-1", name="n", description="d",
        rule_type="diagnostic", level="contextualised",
        scope=RuleScope(subject_class="https://ex.org/vkg#Holding"),
        conditions=[
            Condition(binding="https://ex.org/vkg#days", operator=">",
                      threshold_id="THR-011"),
            Condition(binding="https://ex.org/vkg#status", operator="=",
                      value="Active"),
        ],
        conclusion=Conclusion(outcome="https://ex.org/vkg#LapseRisk"),
        thresholds=[Threshold(threshold_id="THR-011", value=90.0, unit="days",
                              source_span="after 90 days", approved_by="alice@x")],
    )
    base.update(over)
    return Rule(**base)


def test_all_five_kinds_are_generated() -> None:
    cases = generate_test_cases(rule=_rule())
    assert sorted(c.kind for c in cases) == [
        "boundary", "missing_data", "negative", "out_of_scope", "positive"]


def test_the_positive_case_satisfies_every_condition() -> None:
    case = next(c for c in generate_test_cases(rule=_rule()) if c.kind == "positive")
    assert case.inputs["https://ex.org/vkg#days"] > 90
    assert case.inputs["https://ex.org/vkg#status"] == "Active"
    assert case.expected_fires is True


def test_the_boundary_case_sits_exactly_on_the_threshold_and_must_not_fire() -> None:
    """`days > 90` must be false AT 90 — this is the off-by-one guard."""
    case = next(c for c in generate_test_cases(rule=_rule()) if c.kind == "boundary")
    assert case.inputs["https://ex.org/vkg#days"] == 90.0
    assert case.expected_fires is False


def test_a_ge_operator_flips_the_boundary_expectation() -> None:
    rule = _rule(conditions=[Condition(binding="https://ex.org/vkg#days",
                                       operator=">=", threshold_id="THR-011")])
    case = next(c for c in generate_test_cases(rule=rule) if c.kind == "boundary")
    assert case.inputs["https://ex.org/vkg#days"] == 90.0
    assert case.expected_fires is True


def test_the_negative_case_violates_the_first_condition() -> None:
    case = next(c for c in generate_test_cases(rule=_rule()) if c.kind == "negative")
    assert case.expected_fires is False


def test_the_missing_data_case_omits_a_required_binding() -> None:
    case = next(c for c in generate_test_cases(rule=_rule())
                if c.kind == "missing_data")
    assert "https://ex.org/vkg#days" not in case.inputs
    assert case.expected_fires is False


def test_the_out_of_scope_case_names_a_different_subject_class() -> None:
    case = next(c for c in generate_test_cases(rule=_rule())
                if c.kind == "out_of_scope")
    assert case.subject_class != "https://ex.org/vkg#Holding"
    assert case.expected_fires is False


def test_generation_is_deterministic() -> None:
    a = [c.model_dump() for c in generate_test_cases(rule=_rule())]
    b = [c.model_dump() for c in generate_test_cases(rule=_rule())]
    assert a == b


def test_a_literal_only_rule_still_produces_a_boundary_case() -> None:
    """No numeric threshold → the boundary case degenerates to the literal
    equality edge, and is still emitted so the five-case contract holds."""
    rule = _rule(
        conditions=[Condition(binding="https://ex.org/vkg#status", operator="=",
                              value="Active")],
        thresholds=[])
    cases = generate_test_cases(rule=rule)
    assert len(cases) == 5
