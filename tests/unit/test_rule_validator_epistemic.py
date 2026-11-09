"""Level 4 — epistemic validation, plus the four-level aggregate."""
import pytest

from agents.shared.rule_models import (
    Condition, Conclusion, Rule, RuleScope, Threshold,
)
from agents.shared.rule_validator import validate_epistemic, validate_rule


def _rule(**over) -> Rule:
    base = dict(
        rule_id="RULE-004", namespace="ont-1", name="n", description="d",
        rule_type="diagnostic", level="contextualised",
        scope=RuleScope(subject_class="https://ex.org/vkg#Holding"),
        conditions=[Condition(binding="https://ex.org/vkg#days",
                              operator=">", threshold_id="THR-011")],
        conclusion=Conclusion(outcome="https://ex.org/vkg#LapseRisk"),
        thresholds=[Threshold(threshold_id="THR-011", value=90.0, unit="days",
                              source_span="after 90 days", approved_by="alice@x")],
    )
    base.update(over)
    return Rule(**base)


def test_a_hedged_status_needs_no_attestation() -> None:
    report = validate_epistemic(rule=_rule())
    assert report.passed is True


def test_confirmed_cause_without_an_attestation_is_rejected() -> None:
    rule = _rule(conclusion=Conclusion(
        outcome="https://ex.org/vkg#LapseRisk",
        epistemic_status="confirmedCauseOf"))
    report = validate_epistemic(rule=rule)
    assert report.passed is False
    assert any("confirmedCauseOf" in v for v in report.violations)


def test_confirmed_cause_with_a_full_attestation_passes() -> None:
    rule = _rule(conclusion=Conclusion(
        outcome="https://ex.org/vkg#LapseRisk",
        epistemic_status="confirmedCauseOf",
        attested_by="alice@example.com",
        attestation_note="Confirmed by the 2026 actuarial study, table 4."))
    assert validate_epistemic(rule=rule).passed is True


def test_a_whitespace_only_attestation_note_is_not_an_attestation() -> None:
    rule = _rule(conclusion=Conclusion(
        outcome="https://ex.org/vkg#LapseRisk",
        epistemic_status="confirmedCauseOf",
        attested_by="alice@example.com", attestation_note="   "))
    assert validate_epistemic(rule=rule).passed is False


def test_validate_rule_returns_one_report_per_level_in_ladder_order() -> None:
    reports, missing = validate_rule(
        rule=_rule(),
        ask=lambda **_kw: True,
        select=lambda **_kw: [{"binding": "https://ex.org/vkg#days",
                               "column": "h.days"}],
        ontology_graph_uri="https://g/ont-1",
    )
    assert [r.level for r in reports] == [
        "structural", "semantic", "operational", "epistemic"]
    assert all(r.passed for r in reports)
    assert missing == []


def test_validate_rule_runs_every_level_even_after_a_failure() -> None:
    """Reviewers need the full picture in one pass, not one error at a time."""
    reports, missing = validate_rule(
        rule=_rule(),
        ask=lambda **_kw: False,          # semantic will fail
        select=lambda **_kw: [],          # operational will fail
        ontology_graph_uri="https://g/ont-1",
    )
    assert len(reports) == 4
    assert missing == ["https://ex.org/vkg#days"]


@pytest.mark.parametrize("status", [
    "candidateContributorTo", "indicatesRiskOf", "supportsHypothesis",
    "associatedWith", "requiresFurtherEvidence",
])
def test_every_non_causal_status_passes_unattested(status: str) -> None:
    rule = _rule(conclusion=Conclusion(
        outcome="https://ex.org/vkg#LapseRisk", epistemic_status=status))
    assert validate_epistemic(rule=rule).passed is True
