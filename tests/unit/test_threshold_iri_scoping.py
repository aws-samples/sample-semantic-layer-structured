"""Threshold nodes must be scoped to their owning rule.

A threshold IRI keyed only on ``threshold_id`` is shared by every rule that
picks the same id, and ``vkgr:threshold`` is in the rule-delete child closure.
Deleting one rule therefore used to delete a node its siblings still referenced,
after which the survivors failed to deserialize and one dangling rule returned
500 for the whole namespace's rule list.

These tests drive the REAL ``delete_rule_update`` SPARQL through rdflib rather
than asserting on the query string, because the bug was in what the query
*matched*, not in how it was spelled.
"""
import pytest
from rdflib import Dataset, Graph, URIRef

from agents.shared.rule_graph import (
    delete_rule_update, graph_to_rules, rule_to_graph, threshold_id_from_uri,
    threshold_uri,
)
from agents.shared.rule_models import (
    Condition, Conclusion, Rule, RuleScope, Threshold,
)
from agents.shared.rule_vocab import VKGR

GRAPH_URI = "https://semantic-layer.aws/ontologies/ont-1/rules"
SHARED_ID = "THR-1"


def _rule(*, rule_id: str, threshold_id: str = SHARED_ID) -> Rule:
    """Build a minimal valid rule whose single condition uses a threshold.

    :param rule_id: Identifier for the rule.
    :param threshold_id: Threshold id the condition references. Two rules
        deliberately share the default, which is the collision under test.
    :returns: The rule.
    """
    return Rule(
        rule_id=rule_id, namespace="ont-1", name=f"n-{rule_id}",
        description="d", rule_type="diagnostic", level="general",
        scope=RuleScope(subject_class="https://ex.org/vkg#Holding"),
        conditions=[Condition(binding="https://ex.org/vkg#days",
                              operator=">", threshold_id=threshold_id)],
        conclusion=Conclusion(outcome="https://ex.org/vkg#LapseRisk"),
        thresholds=[Threshold(threshold_id=threshold_id, value=90.0,
                              unit="days", source_span="after 90 days",
                              approved_by="felix@example.com")],
    )


def _dataset(*rules: Rule) -> Dataset:
    """Load rules into one named rules graph.

    :param rules: Rules to serialize into the graph.
    :returns: A dataset holding them all in :data:`GRAPH_URI`.
    """
    dataset = Dataset()
    graph = dataset.graph(URIRef(GRAPH_URI))
    for rule in rules:
        for triple in rule_to_graph(rule):
            graph.add(triple)
    return dataset


def _to_legacy_threshold_iris(
    graph: Graph, *, rule_id: str, threshold_id: str,
) -> Graph:
    """Rewrite a rule's scoped threshold IRIs to the pre-fix unscoped shape.

    Reproduces what rules persisted before this fix actually look like, so the
    backward-compatible read path is tested against real legacy data rather
    than a guess at it.

    :param graph: Graph holding the freshly serialized rule.
    :param rule_id: Rule that owns the threshold.
    :param threshold_id: Threshold to un-scope.
    :returns: A new graph using the legacy IRI.
    """
    scoped = threshold_uri(rule_id=rule_id, threshold_id=threshold_id)
    legacy = URIRef(f"{VKGR}{threshold_id}")
    out = Graph()
    for subject, predicate, obj in graph:
        out.add((
            legacy if subject == scoped else subject,
            predicate,
            legacy if obj == scoped else obj,
        ))
    return out


def test_two_rules_sharing_a_threshold_id_get_distinct_nodes() -> None:
    """The collision that caused the corruption must not produce one node."""
    a = threshold_uri(rule_id="RULE-A", threshold_id=SHARED_ID)
    b = threshold_uri(rule_id="RULE-B", threshold_id=SHARED_ID)
    assert a != b
    assert threshold_id_from_uri(term=a) == SHARED_ID
    assert threshold_id_from_uri(term=b) == SHARED_ID


def test_deleting_one_rule_leaves_a_siblings_threshold_intact() -> None:
    """The regression: this used to strip RULE-B's threshold triples."""
    dataset = _dataset(_rule(rule_id="RULE-A"), _rule(rule_id="RULE-B"))
    dataset.update(delete_rule_update(rule_id="RULE-A", graph_uri=GRAPH_URI))

    survivors = graph_to_rules(dataset.graph(URIRef(GRAPH_URI)))
    # Positive control: if the delete matched nothing, the "survivor intact"
    # assertion below would pass for entirely the wrong reason.
    assert [r.rule_id for r in survivors] == ["RULE-B"]
    assert survivors[0].model_dump() == _rule(rule_id="RULE-B").model_dump()


def test_deleting_a_rule_still_removes_its_own_threshold() -> None:
    """Scoping must not leak the deleted rule's threshold node."""
    dataset = _dataset(_rule(rule_id="RULE-A"), _rule(rule_id="RULE-B"))
    dataset.update(delete_rule_update(rule_id="RULE-A", graph_uri=GRAPH_URI))

    graph = dataset.graph(URIRef(GRAPH_URI))
    gone = threshold_uri(rule_id="RULE-A", threshold_id=SHARED_ID)
    assert list(graph.triples((gone, None, None))) == []
    kept = threshold_uri(rule_id="RULE-B", threshold_id=SHARED_ID)
    assert len(list(graph.triples((kept, None, None)))) == 6


def test_legacy_unscoped_threshold_iri_still_deserializes() -> None:
    """Rules written before the fix must keep reading back unchanged."""
    original = _rule(rule_id="RULE-A")
    legacy = _to_legacy_threshold_iris(
        rule_to_graph(original), rule_id="RULE-A", threshold_id=SHARED_ID,
    )
    recovered = graph_to_rules(legacy)
    assert len(recovered) == 1
    assert recovered[0].model_dump() == original.model_dump()


def test_legacy_data_still_shares_one_node_across_rules() -> None:
    """Documents why a migration is needed, not just a code change.

    Legacy graphs keep the hazard until each rule is rewritten, so a namespace
    holding pre-fix rules must be migrated as a whole rather than relied on to
    heal itself.
    """
    dataset = Dataset()
    graph = dataset.graph(URIRef(GRAPH_URI))
    for rule_id in ("RULE-A", "RULE-B"):
        rule = _rule(rule_id=rule_id)
        for triple in _to_legacy_threshold_iris(
            rule_to_graph(rule), rule_id=rule_id, threshold_id=SHARED_ID,
        ):
            graph.add(triple)

    dataset.update(delete_rule_update(rule_id="RULE-A", graph_uri=GRAPH_URI))

    # RULE-B survives as a subject but its threshold node went with RULE-A's...
    assert list(graph.triples((URIRef(f"{VKGR}THR-1"), None, None))) == []
    assert (URIRef(f"{VKGR}RULE-B"), None, None) in graph
    # ...so reading the namespace now blows up, which is the 500 the UI showed.
    # The exception type is deliberately not pinned: the deserializer's handling
    # of a missing threshold is a separate open item (it manufactures the string
    # "None" for three of the four fields and only raises on `value`).
    with pytest.raises(Exception):
        graph_to_rules(graph)


def test_dangling_condition_threshold_reference_round_trips() -> None:
    """The rule miner leaves a threshold the document never stated dangling.

    That node has no triples, so the id survives only in the IRI — which is why
    the id is parsed from the IRI rather than read off the node.
    """
    rule = Rule(
        rule_id="RULE-C", namespace="ont-1", name="n", description="d",
        rule_type="advisory", level="general",
        scope=RuleScope(subject_class="https://ex.org/vkg#Holding"),
        conditions=[Condition(binding="https://ex.org/vkg#days",
                              operator=">", threshold_id="THR-MISSING")],
        conclusion=Conclusion(outcome="https://ex.org/vkg#LapseRisk"),
        thresholds=[],
        missing_inputs=["https://ex.org/vkg#days"],
    )
    recovered = graph_to_rules(rule_to_graph(rule))[0]
    assert recovered.conditions[0].threshold_id == "THR-MISSING"
    assert recovered.thresholds == []


def test_ids_containing_a_slash_stay_addressable() -> None:
    """Neither id field constrains its charset, so the IRI must encode them."""
    rule = _rule(rule_id="RULE/A", threshold_id="THR/1")
    uri = threshold_uri(rule_id="RULE/A", threshold_id="THR/1")
    # Encoded, so the IRI still splits into exactly three segments.
    assert uri == URIRef(f"{VKGR}RULE%2FA/threshold/THR%2F1")
    assert threshold_id_from_uri(term=uri) == "THR/1"
    recovered = graph_to_rules(rule_to_graph(rule))[0]
    assert recovered.model_dump() == rule.model_dump()
