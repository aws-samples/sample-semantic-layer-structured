"""Level 1 — structural validation via SHACL shapes."""
from agents.shared.rule_graph import rule_to_graph
from agents.shared.rule_models import (
    Condition, Conclusion, Rule, RuleScope, Threshold,
)
from agents.shared.rule_validator import validate_structural


def _rule(**over) -> Rule:
    base = dict(
        rule_id="RULE-004", namespace="ont-1", name="n", description="d",
        rule_type="diagnostic", level="contextualised",
        scope=RuleScope(subject_class="https://ex.org/vkg#Holding"),
        conditions=[Condition(binding="https://ex.org/vkg#days",
                              operator=">", threshold_id="THR-011")],
        conclusion=Conclusion(outcome="https://ex.org/vkg#LapseRisk"),
        thresholds=[Threshold(threshold_id="THR-011", value=90.0, unit="days",
                              source_span="after 90 days")],
    )
    base.update(over)
    return Rule(**base)


def test_wellformed_rule_passes() -> None:
    report = validate_structural(rule_to_graph(_rule()))
    assert report.passed is True
    assert report.violations == []


def test_rule_missing_a_conclusion_fails() -> None:
    """Hand-edit the graph — pydantic would never let this construct."""
    from rdflib import URIRef
    from agents.shared.rule_vocab import VKGR
    g = rule_to_graph(_rule())
    g.remove((URIRef(f"{VKGR}RULE-004"), VKGR.conclusion, None))
    report = validate_structural(g)
    assert report.passed is False
    assert any("conclusion" in v.lower() for v in report.violations)


def test_condition_referencing_an_absent_threshold_node_fails() -> None:
    from agents.shared.rule_graph import threshold_uri
    g = rule_to_graph(_rule())
    # Address the node through the builder: the IRI is rule-scoped, so a
    # hardcoded vkgr:THR-011 would remove nothing and the test would pass for
    # the wrong reason.
    g.remove((threshold_uri(rule_id="RULE-004", threshold_id="THR-011"),
              None, None))
    report = validate_structural(g)
    assert report.passed is False


def test_epistemic_status_outside_the_closed_set_fails() -> None:
    """The sh:in shape is the same mechanism enum_shapes.py already emits."""
    from rdflib import URIRef
    from agents.shared.rule_vocab import VKGR
    g = rule_to_graph(_rule())
    for _s, _p, o in list(g.triples((None, VKGR.epistemicStatus, None))):
        g.remove((_s, _p, o))
        g.add((_s, _p, URIRef(f"{VKGR}definitelyCauses")))
    report = validate_structural(g)
    assert report.passed is False


def test_report_is_fail_closed_on_an_unparseable_shapes_graph() -> None:
    """Structural validation is a GATE — unlike the query-side enum injector
    it must fail CLOSED, never silently pass."""
    from agents.shared import rule_validator
    report = rule_validator.validate_structural(
        rule_to_graph(_rule()), shapes_ttl="@@@ not turtle @@@")
    assert report.passed is False
