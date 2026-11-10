"""Reading a corrupt stored rule must fail loudly and locally.

Two behaviours are covered, and they are two halves of one fix:

* a missing required field raises ``RuleReadError`` NAMING the rule — the
  threshold-IRI incident surfaced as ``AttributeError: 'NoneType' object has no
  attribute 'toPython'``, which named neither the rule nor the field, and three
  of the four threshold fields did not even raise: they produced the literal
  string ``"None"`` and stored it as governance data;
* one unreadable rule does not take the others down with it, because a stricter
  reader without per-rule isolation would widen the blast radius rather than
  narrow it.
"""
from typing import List, Optional

import pytest
from rdflib import Graph, URIRef

from agents.shared.rule_graph import (
    RuleReadError, graph_to_rules, partition_rules, rule_to_graph,
)
from agents.shared.rule_models import (
    Condition, Conclusion, Provenance, Rule, RuleScope, TestCase, Threshold,
)
from agents.shared.rule_vocab import VKGR

NAMESPACE = "ont-1"
THRESHOLD_ID = "THR-1"


def _rule(*, rule_id: str = "RULE-A", rich: bool = False) -> Rule:
    """Build a rule bound to one threshold.

    :param rule_id: Identifier for the rule.
    :param rich: When True, populate the optional fields too, so a round-trip
        exercises both the required and the optional readers.
    :returns: The rule.
    """
    extra = {}
    if rich:
        extra = {
            "specialises": "RULE-PARENT",
            "reviewed_by": "reviewer@example.com",
            "compiled_sparql": "SELECT * WHERE { ?s ?p ?o }",
            "synonyms": ["lapse"],
            "missing_inputs": ["policy_state"],
            "provenance": Provenance(
                document_id="doc-1", source_sentence="after 90 days",
                model_id="model-1", confidence=0.9, page=4,
            ),
            "test_cases": [TestCase(
                kind="positive",
                subject_class="https://ex.org/vkg#Holding",
                inputs={"days": 120}, expected_fires=True,
                rationale="over the bound",
            )],
        }
    return Rule(
        rule_id=rule_id, namespace=NAMESPACE, name=f"n-{rule_id}",
        description="d", rule_type="diagnostic", level="general",
        scope=RuleScope(subject_class="https://ex.org/vkg#Holding"),
        conditions=[Condition(binding="https://ex.org/vkg#days",
                              operator=">", threshold_id=THRESHOLD_ID)],
        conclusion=Conclusion(outcome="https://ex.org/vkg#LapseRisk"),
        thresholds=[Threshold(threshold_id=THRESHOLD_ID, value=90.0,
                              unit="days", source_span="after 90 days")],
        **extra,
    )


def _without(*, graph: Graph, predicate: URIRef,
             subject: Optional[URIRef] = None) -> Graph:
    """Copy a graph with one predicate's triples removed.

    Simulates the corruption a shared-node delete used to cause: the node is
    still referenced, but its triples are gone.

    :param graph: Graph to copy.
    :param predicate: Predicate to drop.
    :param subject: Restrict the drop to this subject, or drop all when None.
    :returns: The reduced graph.
    """
    out = Graph()
    for triple in graph:
        if triple[1] == predicate and (subject is None or triple[0] == subject):
            continue
        out.add(triple)
    return out


# --- fix 1: fail loudly, naming the rule ----------------------------------


@pytest.mark.parametrize("predicate,field", [
    (VKGR.thresholdId, "thresholdId"),
    (VKGR.unit, "unit"),
    (VKGR.sourceSpan, "sourceSpan"),
    (VKGR.value, "value"),
])
def test_missing_threshold_field_names_the_rule_and_the_threshold(
    predicate: URIRef, field: str,
) -> None:
    """All four threshold fields must raise, not just ``value``.

    Before this fix only ``value`` failed (via ``.toPython()`` on None); the
    other three passed through ``str(None)`` and became ``"None"``.
    """
    graph = _without(graph=rule_to_graph(_rule()), predicate=predicate)
    with pytest.raises(RuleReadError) as caught:
        graph_to_rules(graph)
    message = str(caught.value)
    assert field in message
    # Naming the rule is the point: "which rule" was the information missing
    # from the original AttributeError.
    assert "RULE-A" in message
    assert THRESHOLD_ID in message
    assert caught.value.rule_id == "RULE-A"


def test_no_readable_rule_can_carry_the_literal_string_none() -> None:
    """Regression for the ``"None"`` manufacturing, stated as an invariant.

    ``source_span`` declares ``min_length=1`` specifically so a bound can never
    be invented — and ``"None"`` satisfied it, defeating the check.
    """
    for predicate in (VKGR.unit, VKGR.sourceSpan, VKGR.thresholdId):
        graph = _without(graph=rule_to_graph(_rule()), predicate=predicate)
        with pytest.raises(RuleReadError):
            graph_to_rules(graph)


@pytest.mark.parametrize("predicate", [
    VKGR.ruleId, VKGR.namespace, VKGR.name, VKGR.description, VKGR.ruleType,
    VKGR.level, VKGR.lifecycle, VKGR.version, VKGR.scope, VKGR.conclusion,
    VKGR.outcome, VKGR.epistemicStatus, VKGR.subjectClass, VKGR.binding,
    VKGR.operator, VKGR.conditionIndex,
])
def test_every_unconditional_predicate_is_required(predicate: URIRef) -> None:
    """The same defect sat at ~20 sites, so the whole deserializer is covered.

    Fixing only the threshold reads would have left identical landmines two
    lines away.
    """
    graph = _without(graph=rule_to_graph(_rule()), predicate=predicate)
    with pytest.raises(RuleReadError):
        graph_to_rules(graph)


@pytest.mark.parametrize("predicate", [
    VKGR.documentId, VKGR.sourceSentence, VKGR.modelId, VKGR.confidence,
    VKGR.kind, VKGR.inputsJson, VKGR.expectedFires, VKGR.rationale,
    VKGR.testCaseIndex,
])
def test_provenance_and_test_case_fields_are_required(predicate: URIRef) -> None:
    """These nodes are optional, but every field inside them is written."""
    graph = _without(graph=rule_to_graph(_rule(rich=True)),
                     predicate=predicate)
    with pytest.raises(RuleReadError):
        graph_to_rules(graph)


@pytest.mark.parametrize("rich", [False, True])
def test_round_trip_still_succeeds(rich: bool) -> None:
    """Guard against over-requiring: a rule with NO optional field set must read.

    ``rich=False`` omits provenance, test cases, synonyms, ``specialises``,
    ``reviewed_by``, ``compiled_sparql``, ``process_stage`` and ``approved_by`` —
    if any of those had been made required, this fails.
    """
    original = _rule(rich=rich)
    assert graph_to_rules(rule_to_graph(original))[0] == original


def test_rule_read_error_is_not_a_value_error() -> None:
    """A corrupt stored rule is a 5xx fault, not a client 422.

    The routers map ``ValueError`` to 422; inheriting from it would tell the
    caller to fix a payload they never sent.
    """
    assert not issubclass(RuleReadError, ValueError)


def test_unconvertible_literal_raises_rule_read_error() -> None:
    """A present-but-garbage numeric literal is the same class of fault.

    Without this, a bare ``ValueError`` would escape and the router would
    mislabel it 422.
    """
    from rdflib import Literal
    graph = _without(graph=rule_to_graph(_rule()), predicate=VKGR.version)
    graph.add((URIRef(f"{VKGR}RULE-A"), VKGR.version, Literal("not-a-number")))
    with pytest.raises(RuleReadError) as caught:
        graph_to_rules(graph)
    assert "version" in str(caught.value)


# --- fix 2: one unreadable rule must not take down the list ----------------


def _two_rule_graph(*, broken: str, predicate: URIRef) -> Graph:
    """Build a graph of two rules, one of them corrupt.

    :param broken: Rule id to corrupt.
    :param predicate: Predicate to strip from the corrupt rule's closure.
    :returns: The graph.
    """
    graph = rule_to_graph(_rule(rule_id="RULE-A"))
    for triple in rule_to_graph(_rule(rule_id="RULE-B")):
        graph.add(triple)
    # Strip the predicate only from the target's own threshold node, so the
    # sibling's copy survives — the shape the shared-node delete produced.
    victim = [
        node for node in graph.objects(URIRef(f"{VKGR}{broken}"), VKGR.threshold)
    ]
    return _without(graph=graph, predicate=predicate, subject=victim[0])


def test_partition_isolates_the_unreadable_rule() -> None:
    """The healthy sibling reads; the corrupt one is reported, not dropped."""
    graph = _two_rule_graph(broken="RULE-A", predicate=VKGR.unit)
    rules, failures = partition_rules(graph)
    assert [r.rule_id for r in rules] == ["RULE-B"]
    assert [f["rule_id"] for f in failures] == ["RULE-A"]
    assert "unit" in failures[0]["error"]


def test_graph_to_rules_stays_strict() -> None:
    """The migration and the agent runtime must NOT silently skip a rule.

    A migration that skips a rule reports success while leaving it unmigrated,
    which is worse than failing.
    """
    graph = _two_rule_graph(broken="RULE-A", predicate=VKGR.unit)
    with pytest.raises(RuleReadError):
        graph_to_rules(graph)


def test_partition_reports_a_rule_whose_own_id_is_unreadable() -> None:
    """The id fallback must work, or the failure entry names nothing.

    ``_rule_id_for_report`` may not raise: an exception there would turn a
    one-rule report back into a total failure.
    """
    graph = _without(graph=rule_to_graph(_rule(rule_id="RULE-A")),
                     predicate=VKGR.ruleId)
    rules, failures = partition_rules(graph)
    assert rules == []
    # Falls back to the subject IRI's local name.
    assert failures[0]["rule_id"] == "RULE-A"


def test_partition_on_a_healthy_graph_reports_no_failures() -> None:
    """The common case must not gain a phantom failure entry."""
    rules, failures = partition_rules(rule_to_graph(_rule(rich=True)))
    assert [r.rule_id for r in rules] == ["RULE-A"]
    assert failures == []


def test_partition_of_an_empty_graph() -> None:
    """A namespace with no rules is not a failure."""
    assert partition_rules(Graph()) == ([], [])


def test_partition_preserves_subject_order() -> None:
    """Ordering must match ``graph_to_rules`` so the two cannot disagree."""
    graph = rule_to_graph(_rule(rule_id="RULE-B"))
    for triple in rule_to_graph(_rule(rule_id="RULE-A")):
        graph.add(triple)
    rules, _ = partition_rules(graph)
    expected: List[str] = [r.rule_id for r in graph_to_rules(graph)]
    assert [r.rule_id for r in rules] == expected
