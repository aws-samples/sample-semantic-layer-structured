"""RDF round-trip + rules-graph SPARQL wrappers."""
from unittest.mock import MagicMock

from rdflib import BNode, Graph, URIRef

from agents.shared.rule_graph import (
    delete_rule_update, graph_to_rules, read_rules, rules_graph_uri,
    rule_to_graph, rule_to_insert_data, rule_to_upsert_update,
)
from agents.shared.rule_models import (
    Condition, Conclusion, Provenance, Rule, RuleScope, TestCase, Threshold,
)


def _rule() -> Rule:
    return Rule(
        rule_id="RULE-004", namespace="ont-1", name="Elevated lapse risk",
        description="Active holdings unpaid beyond the approved window.",
        rule_type="diagnostic", level="contextualised",
        scope=RuleScope(subject_class="https://ex.org/vkg#Holding",
                        process_stage="renewal"),
        conditions=[
            Condition(binding="https://ex.org/vkg#daysSinceLastPayment",
                      operator=">", threshold_id="THR-011"),
            Condition(binding="https://ex.org/vkg#status", operator="=",
                      value="Active"),
        ],
        conclusion=Conclusion(outcome="https://ex.org/vkg#LapseRisk",
                              epistemic_status="candidateContributorTo"),
        thresholds=[Threshold(threshold_id="THR-011", value=90.0, unit="days",
                              source_span="after 90 days",
                              approved_by="felix@example.com")],
        provenance=Provenance(document_id="doc-1", page=12,
                              source_sentence="Lapse risk rises after 90 days.",
                              model_id="m1", confidence=0.82),
        synonyms=["lapse exposure"],
        test_cases=[TestCase(kind="boundary",
                             subject_class="https://ex.org/vkg#Holding",
                             inputs={"https://ex.org/vkg#daysSince": 90.0},
                             expected_fires=False,
                             rationale="exactly 90 days; > excludes the edge")],
    )


def test_round_trip_preserves_every_field() -> None:
    """RDF is the source of truth — a lossy round-trip loses governance data."""
    original = _rule()
    graph = rule_to_graph(original)
    recovered = graph_to_rules(graph)
    assert len(recovered) == 1
    assert recovered[0].model_dump() == original.model_dump()


def test_round_trip_preserves_condition_order() -> None:
    """Compiled SPARQL clause order must be deterministic across reloads."""
    recovered = graph_to_rules(rule_to_graph(_rule()))[0]
    assert [c.binding for c in recovered.conditions] == [
        "https://ex.org/vkg#daysSinceLastPayment", "https://ex.org/vkg#status",
    ]


def test_round_trip_of_draft_without_provenance_or_thresholds() -> None:
    """Minimal DRAFT shape must survive too (hand-authored rules)."""
    r = Rule(
        rule_id="RULE-009", namespace="ont-1", name="n", description="d",
        rule_type="advisory", level="general",
        scope=RuleScope(subject_class="https://ex.org/vkg#Holding"),
        conditions=[Condition(binding="https://ex.org/vkg#status",
                              operator="=", value="Active")],
        conclusion=Conclusion(outcome="https://ex.org/vkg#Foo"),
        missing_inputs=["https://ex.org/vkg#coolingCapacity"],
    )
    assert graph_to_rules(rule_to_graph(r))[0].model_dump() == r.model_dump()


def test_graph_to_rules_returns_empty_for_a_graph_with_no_rules() -> None:
    assert graph_to_rules(Graph()) == []


def test_rules_graph_uri_appends_the_suffix() -> None:
    assert rules_graph_uri(
        namespace="abc-123",
        graph_uri_prefix="https://semantic-layer.aws/ontologies/",
    ) == "https://semantic-layer.aws/ontologies/abc-123/rules"


def test_insert_data_targets_the_rules_graph_only() -> None:
    """A write must never land in the ontology graph."""
    update = rule_to_insert_data(
        _rule(), graph_uri="https://semantic-layer.aws/ontologies/abc-123/rules")
    assert update.startswith("INSERT DATA")
    assert "GRAPH <https://semantic-layer.aws/ontologies/abc-123/rules>" in update
    assert "/rules>" in update


def test_string_literals_are_escaped_in_insert_data() -> None:
    """A quote or newline in a source sentence must not break the update."""
    r = _rule()
    r.provenance.source_sentence = 'He said "risk rises"\nafter 90 days\\'
    update = rule_to_insert_data(r, graph_uri="https://ex.org/g/rules")
    assert '\\"risk rises\\"' in update
    assert "\\n" in update
    assert '"""' not in update


# --- SPARQL wrappers with an injected executor -----------------------------
# Task 15's NeptuneService adapters call these, so the query_type keyword and
# the response shape are a contract, not an implementation detail.


def _cell(term) -> dict:
    """Render one rdflib term as a SPARQL-results JSON binding cell."""
    if isinstance(term, URIRef):
        return {"type": "uri", "value": str(term)}
    if isinstance(term, BNode):
        return {"type": "bnode", "value": str(term)}
    cell = {"type": "literal", "value": str(term)}
    # Datatype must survive the trip or ints/floats/bools come back as strings.
    if term.datatype is not None:
        cell["datatype"] = str(term.datatype)
    return cell


def _select_response(graph: Graph) -> dict:
    """Render a graph as the JSON a `SELECT ?s ?p ?o` over it would return."""
    return {"results": {"bindings": [
        {"s": _cell(s), "p": _cell(p), "o": _cell(o)} for s, p, o in graph
    ]}}


def test_read_rules_queries_the_rules_graph_with_select() -> None:
    """SELECT, not CONSTRUCT: the gateway wraps CONSTRUCT in a turtle envelope."""
    execute = MagicMock(return_value=_select_response(rule_to_graph(_rule())))
    rules = read_rules(
        namespace="abc-123", execute_sparql=execute,
        graph_uri_prefix="https://semantic-layer.aws/ontologies/",
    )
    assert execute.call_args.kwargs["query_type"] == "SELECT"
    assert ("GRAPH <https://semantic-layer.aws/ontologies/abc-123/rules>"
            in execute.call_args.kwargs["query"])
    assert [r.rule_id for r in rules] == ["RULE-004"]


def test_read_rules_returns_empty_when_the_graph_is_absent() -> None:
    """A namespace with no rules yet is normal, not an error."""
    execute = MagicMock(return_value={"results": {"bindings": []}})
    assert read_rules(namespace="abc-123", execute_sparql=execute,
                      graph_uri_prefix="https://ex.org/") == []


def test_delete_rule_update_is_scoped_to_the_rules_graph() -> None:
    update = delete_rule_update(rule_id="RULE-004",
                                graph_uri="https://ex.org/g/rules")
    assert update.startswith("DELETE")
    assert update.count("GRAPH <https://ex.org/g/rules>") == 2
    assert "RULE-004" in update


def test_upsert_deletes_before_inserting() -> None:
    """Without the DELETE, editing a rule would duplicate its condition nodes."""
    update = rule_to_upsert_update(rule=_rule(),
                                   graph_uri="https://ex.org/g/rules")
    assert update.index("DELETE") < update.index("INSERT DATA")
    assert " ;" in update
