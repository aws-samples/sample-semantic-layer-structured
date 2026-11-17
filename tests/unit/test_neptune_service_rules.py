"""Rules-graph adapter methods on NeptuneService."""
import os
import sys
from unittest.mock import MagicMock

import pytest

# `services.*` lives under the REST-API package root, not the repo root.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

from agents.shared.rule_models import (  # noqa: E402
    Condition, Conclusion, Rule, RuleScope, Threshold,
)

# The gateway namespaces every tool as `<target>___<tool>`; NeptuneService calls
# them by that full name (see get_graph_summary et al.), so the rules adapters
# must too. Asserting on the suffix keeps the test honest about which tool is
# used without pinning the target-name prefix.
SPARQL_TOOL_SUFFIX = "execute_sparql_query"


def _rule() -> Rule:
    return Rule(
        rule_id="RULE-004", namespace="ont-1", name="n", description="d",
        rule_type="diagnostic", level="contextualised",
        scope=RuleScope(subject_class="https://ex.org/vkg#Holding"),
        conditions=[Condition(binding="https://ex.org/vkg#days", operator=">",
                              threshold_id="THR-011")],
        conclusion=Conclusion(outcome="https://ex.org/vkg#LapseRisk"),
        thresholds=[Threshold(threshold_id="THR-011", value=90.0, unit="days",
                              source_span="after 90 days")],
    )


def _svc():
    """Build a NeptuneService with the gateway call stubbed out."""
    from services.neptune_service import NeptuneService
    svc = NeptuneService.__new__(NeptuneService)   # bypass __init__/env
    svc.gateway_client = MagicMock()
    svc._call_gateway_tool = MagicMock(return_value={"results": {"bindings": []}})
    return svc


def test_write_rule_issues_an_update_against_the_rules_graph() -> None:
    svc = _svc()
    svc.write_rule(rule=_rule())
    args = svc._call_gateway_tool.call_args
    assert args[0][0].endswith(SPARQL_TOOL_SUFFIX)
    payload = args[0][1]
    assert payload["query_type"] == "UPDATE"
    assert "/rules" in payload["sparql_query"]


def test_write_rule_deletes_the_old_version_before_inserting() -> None:
    """Upsert semantics: without the DELETE, editing a rule would leave the
    old conditions in the graph and the rule would read back doubled."""
    svc = _svc()
    svc.write_rule(rule=_rule())
    query = svc._call_gateway_tool.call_args[0][1]["sparql_query"]
    assert query.index("DELETE") < query.index("INSERT DATA")


def test_read_rules_uses_select_and_returns_models() -> None:
    svc = _svc()
    out = svc.read_rules(namespace="ont-1")
    assert out == []
    assert svc._call_gateway_tool.call_args[0][1]["query_type"] == "SELECT"


def test_delete_rule_targets_only_the_named_rule() -> None:
    svc = _svc()
    svc.delete_rule(namespace="ont-1", rule_id="RULE-004")
    query = svc._call_gateway_tool.call_args[0][1]["sparql_query"]
    assert "RULE-004" in query
    assert "/rules" in query


def test_ask_returns_a_boolean_from_the_gateway_payload() -> None:
    svc = _svc()
    svc._call_gateway_tool = MagicMock(return_value={"boolean": True})
    assert svc.ask(query="ASK { ?s ?p ?o }", graph_uri="https://g/ont-1") is True


def test_ask_treats_a_missing_boolean_key_as_false() -> None:
    """Fail closed: an unparseable ASK response must not read as True."""
    svc = _svc()
    svc._call_gateway_tool = MagicMock(return_value={})
    assert svc.ask(query="ASK { ?s ?p ?o }", graph_uri="https://g/ont-1") is False


def test_ask_scopes_the_pattern_to_the_named_graph() -> None:
    """A validation ASK that leaked outside its graph would answer from another
    ontology's triples — the wrong answer, silently."""
    svc = _svc()
    svc._call_gateway_tool = MagicMock(return_value={"boolean": True})
    svc.ask(query="ASK { ?s a <https://ex.org/vkg#Holding> }",
            graph_uri="https://g/ont-1")
    query = svc._call_gateway_tool.call_args[0][1]["sparql_query"]
    assert "GRAPH <https://g/ont-1>" in query


def test_select_in_graph_flattens_bindings_to_plain_dicts() -> None:
    svc = _svc()
    svc._call_gateway_tool = MagicMock(return_value={"results": {"bindings": [
        {"binding": {"type": "uri", "value": "https://ex.org/vkg#days"},
         "column": {"type": "literal", "value": "h.days"}},
    ]}})
    rows = svc.select_in_graph(
        query="SELECT ?binding ?column WHERE { ?binding ?p ?column }",
        graph_uri="https://g/ont-1")
    assert rows == [{"binding": "https://ex.org/vkg#days", "column": "h.days"}]


def test_select_in_graph_returns_empty_when_there_are_no_results() -> None:
    """Level 3 reads an empty result as "unmapped", which is fail-closed."""
    svc = _svc()
    svc._call_gateway_tool = MagicMock(return_value={})
    assert svc.select_in_graph(query="SELECT ?x WHERE { ?x ?p ?o }",
                               graph_uri="https://g/ont-1") == []


# --- Ontology graph resolution -------------------------------------------
# The validators need the ONTOLOGY graph, whose URI is not derivable from the
# layer id: the creation agent writes under `http://<name-slug>/ontology/<id>`
# and the slug carries a build timestamp.

_GRAPHS = {"named_graphs": [
    "http://bird-cards-ontology-20260804-101500/ontology/ont-1",
    "http://bird-cards-ontology-20260809-084812/ontology/ont-1",
    "https://semantic-layer.aws/ontologies/ont-1/rules",
    "http://other-layer-ontology-20260810-120000/ontology/ont-2",
]}


def test_resolve_ontology_graph_uri_returns_the_newest_build() -> None:
    """A rebuild mints a NEW graph and the old one survives, so resolution must
    order explicitly — otherwise an arbitrary stale build answers."""
    svc = _svc()
    svc._call_gateway_tool = MagicMock(return_value=_GRAPHS)
    assert svc.resolve_ontology_graph_uri(id="ont-1") == (
        "http://bird-cards-ontology-20260809-084812/ontology/ont-1"
    )


def test_resolve_ontology_graph_uri_never_returns_the_rules_graph() -> None:
    """The rules graph URI also contains the id; returning it would make
    ontology validation read rule triples."""
    svc = _svc()
    svc._call_gateway_tool = MagicMock(return_value=_GRAPHS)
    assert not svc.resolve_ontology_graph_uri(id="ont-1").endswith("/rules")


def test_resolve_ontology_graph_uri_ignores_other_layers() -> None:
    svc = _svc()
    svc._call_gateway_tool = MagicMock(return_value=_GRAPHS)
    assert svc.resolve_ontology_graph_uri(id="ont-2").endswith("/ontology/ont-2")


def test_resolve_ontology_graph_uri_raises_when_the_layer_was_never_built() -> None:
    """Fail loudly: validating against a nonexistent graph would report
    "property not declared" for every level instead of the real cause."""
    svc = _svc()
    svc._call_gateway_tool = MagicMock(return_value={"named_graphs": []})
    with pytest.raises(ValueError, match="no ontology named graph"):
        svc.resolve_ontology_graph_uri(id="ont-9")
