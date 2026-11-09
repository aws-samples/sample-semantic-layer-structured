"""Deterministic compilation of a governed rule to SPARQL."""
import pytest

from agents.shared.rule_compiler import CompileRefused, compile_rule
from agents.shared.rule_models import (
    Condition, Conclusion, Rule, RuleScope, Threshold,
)


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


def test_compiles_a_select_over_the_scope_class() -> None:
    sparql = compile_rule(rule=_rule())
    assert sparql.startswith("SELECT")
    assert "<https://ex.org/vkg#Holding>" in sparql


def test_every_condition_becomes_a_filter() -> None:
    sparql = compile_rule(rule=_rule())
    assert "> 90" in sparql.replace("90.0", "90")
    assert '"Active"' in sparql


def test_compilation_is_byte_identical_across_calls() -> None:
    """Determinism is what makes the compiled artefact reviewable."""
    assert compile_rule(rule=_rule()) == compile_rule(rule=_rule())


def test_condition_order_drives_filter_order() -> None:
    rule = _rule()
    swapped = _rule(conditions=list(reversed(rule.conditions)))
    assert compile_rule(rule=rule) != compile_rule(rule=swapped)


def test_it_refuses_a_rule_with_an_unapproved_threshold() -> None:
    rule = _rule(thresholds=[Threshold(threshold_id="THR-011", value=90.0,
                                       unit="days", source_span="after 90 days")])
    with pytest.raises(CompileRefused, match="THR-011"):
        compile_rule(rule=rule)


def test_it_refuses_a_rule_with_missing_inputs() -> None:
    rule = _rule(missing_inputs=["https://ex.org/vkg#coolingCapacity"])
    with pytest.raises(CompileRefused, match="coolingCapacity"):
        compile_rule(rule=rule)


def test_it_refuses_a_condition_referencing_an_unknown_threshold_id() -> None:
    rule = _rule(conditions=[Condition(binding="https://ex.org/vkg#days",
                                       operator=">", threshold_id="THR-999")])
    with pytest.raises(CompileRefused, match="THR-999"):
        compile_rule(rule=rule)


def test_it_rejects_an_operator_outside_the_allowlist() -> None:
    """Operators are never interpolated raw — an allowlist prevents injection."""
    rule = _rule()
    object.__setattr__(rule.conditions[0], "operator", "; DROP")
    with pytest.raises(CompileRefused):
        compile_rule(rule=rule)


def test_string_literals_are_escaped() -> None:
    rule = _rule(conditions=[Condition(binding="https://ex.org/vkg#status",
                                       operator="=", value='He said "hi"')])
    sparql = compile_rule(rule=_rule(conditions=rule.conditions))
    assert '\\"hi\\"' in sparql


def test_output_passes_through_the_ontop_envelope() -> None:
    """apply() is idempotent, so re-applying must not change the output."""
    from agents.shared import ontop_envelope
    sparql = compile_rule(rule=_rule())
    assert ontop_envelope.apply(sparql) == sparql
