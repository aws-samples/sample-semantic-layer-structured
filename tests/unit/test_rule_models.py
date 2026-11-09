"""Pydantic shape of the governed Rule record."""
import pytest
from pydantic import ValidationError

from agents.shared.rule_models import (
    Condition, Conclusion, Provenance, Rule, RuleScope, TestCase, Threshold,
)


def _prov() -> Provenance:
    return Provenance(
        document_id="doc-1", page=12, source_sentence="Lapse risk rises after 90 days.",
        model_id="global.anthropic.claude-sonnet-5", confidence=0.82,
    )


def _rule(**over) -> Rule:
    base = dict(
        rule_id="RULE-004", namespace="ont-1", name="Elevated lapse risk",
        description="Active holdings unpaid beyond the approved window.",
        rule_type="diagnostic", level="contextualised",
        scope=RuleScope(subject_class="vkg:Holding", process_stage="renewal"),
        conditions=[Condition(binding="vkg:Holding/daysSinceLastPayment",
                              operator=">", threshold_id="THR-011")],
        conclusion=Conclusion(outcome="vkg:LapseRisk",
                              epistemic_status="candidateContributorTo"),
        thresholds=[Threshold(threshold_id="THR-011", value=90.0, unit="days",
                              source_span="after 90 days")],
        provenance=_prov(),
    )
    base.update(over)
    return Rule(**base)


def test_new_rule_defaults_to_draft_and_unpublishable() -> None:
    r = _rule()
    assert r.lifecycle == "DRAFT"
    assert r.compiled_sparql is None
    assert r.missing_inputs == []
    assert r.reviewed_by is None


def test_epistemic_status_outside_closed_set_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _rule(conclusion=Conclusion(outcome="vkg:LapseRisk",
                                    epistemic_status="definitelyCauses"))


def test_condition_needs_either_a_threshold_or_a_literal_value() -> None:
    """A condition with neither is unevaluable — fail loudly at construction."""
    with pytest.raises(ValidationError):
        _rule(conditions=[Condition(binding="vkg:Holding/status", operator="=")])


def test_condition_rejects_both_threshold_and_value() -> None:
    with pytest.raises(ValidationError):
        _rule(conditions=[Condition(binding="vkg:Holding/status", operator="=",
                                    threshold_id="THR-011", value="Active")])


def test_threshold_without_source_span_is_rejected() -> None:
    """Design §5: a threshold the document never stated cannot be invented."""
    with pytest.raises(ValidationError):
        Threshold(threshold_id="THR-012", value=30.0, unit="days", source_span="")


def test_threshold_is_unapproved_until_someone_approves_it() -> None:
    t = _rule().thresholds[0]
    assert t.approved_by is None
    assert t.is_approved() is False
    t.approved_by = "felix@example.com"
    assert t.is_approved() is True


def test_unknown_lifecycle_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _rule(lifecycle="LIVE")


def test_test_case_kind_is_constrained_to_the_five_mandatory_kinds() -> None:
    with pytest.raises(ValidationError):
        TestCase(kind="smoke", subject_class="http://ex.org/C", inputs={},
                 expected_fires=True, rationale="x")


# --- is_evaluable decision table -------------------------------------------
# This is the predicate the whole query path branches on, so it gets its own
# exhaustive table rather than riding on the constructor tests above.


def _publishable() -> Rule:
    """A rule that has cleared every gate — the only shape that executes."""
    r = _rule()
    r.thresholds[0].approved_by = "felix@example.com"
    r.compiled_sparql = "SELECT ?h WHERE { ?h a vkg:Holding }"
    return r


def test_evaluable_when_every_gate_is_cleared() -> None:
    assert _publishable().is_evaluable() is True


def test_not_evaluable_when_a_threshold_is_unapproved() -> None:
    """The human gate — an unapproved bound must never execute."""
    r = _publishable()
    r.thresholds[0].approved_by = None
    assert r.is_evaluable() is False


def test_not_evaluable_when_threshold_approver_is_blank() -> None:
    """Whitespace is not an approval."""
    r = _publishable()
    r.thresholds[0].approved_by = "   "
    assert r.is_evaluable() is False


def test_not_evaluable_when_inputs_are_missing() -> None:
    r = _publishable()
    r.missing_inputs = ["vkg:Holding/coolingCapacity"]
    assert r.is_evaluable() is False


def test_not_evaluable_without_compiled_sparql() -> None:
    r = _publishable()
    r.compiled_sparql = None
    assert r.is_evaluable() is False


def test_not_evaluable_when_a_referenced_threshold_is_dangling() -> None:
    """A condition pointing at a threshold_id that isn't on the record must
    not silently evaluate as if unconstrained."""
    r = _publishable()
    r.thresholds = []
    assert r.is_evaluable() is False


def test_literal_only_conditions_need_no_threshold_approval() -> None:
    """A rule made only of literal comparisons has no bound to approve."""
    r = _rule(
        conditions=[Condition(binding="vkg:Holding/status", operator="=",
                              value="Active")],
        thresholds=[],
    )
    r.compiled_sparql = "SELECT ?h WHERE { ?h a vkg:Holding }"
    assert r.is_evaluable() is True
