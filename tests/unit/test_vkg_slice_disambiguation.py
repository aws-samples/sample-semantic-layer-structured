"""Unit tests for the VKG Phase 3b slice-level disambiguation guard."""
from agents.ontology_query_agent.tier2.slice_disambiguation import (
    find_slice_ambiguities,
)

EX = "http://ex.com/"


def _ttl(body: str) -> str:
    return f"@prefix ex: <{EX}> .\n@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n{body}"


def test_property_collision_two_unrelated_classes_clarifies():
    # ex:amount has domain on both Order and Payment (unrelated) → clarify.
    ttl = _ttl(
        "ex:Order a rdfs:Class . ex:Payment a rdfs:Class . "
        "ex:amount rdfs:domain ex:Order . ex:amount rdfs:domain ex:Payment ."
    )
    res = find_slice_ambiguities(question="total amount", slice_graph=ttl)
    assert res["ambiguous"] is True
    item = res["items"][0]
    assert item["term"] == "amount"
    assert len(item["matches"]) == 2


def test_property_collision_subclass_chain_resolves_heuristically():
    # ex:amount domain on Payment and CardPayment; CardPayment SubClassOf
    # Payment → pick the most-specific (CardPayment) without asking.
    ttl = _ttl(
        "ex:Payment a rdfs:Class . ex:CardPayment rdfs:subClassOf ex:Payment . "
        "ex:amount rdfs:domain ex:Payment . ex:amount rdfs:domain ex:CardPayment ."
    )
    res = find_slice_ambiguities(question="total amount", slice_graph=ttl)
    assert res["ambiguous"] is False
    assert res["resolved"].get("amount") == f"{EX}CardPayment"


def test_generic_name_attribute_does_not_clarify():
    # Regression (nb6 gt-row-04/06): "name" is a property on MANY classes
    # (Party.name, CoverageProduct.name) — a descriptive attribute, NOT an entity
    # choice. The user named the head entity elsewhere ("coverage products by
    # NAME"), so a 'name' collision must NOT escalate to "which interpretation of
    # 'name'?". Other generic label attrs (label/description) behave the same.
    ttl = _ttl(
        "ex:Party a rdfs:Class . ex:CoverageProduct a rdfs:Class . "
        "ex:name rdfs:domain ex:Party . ex:name rdfs:domain ex:CoverageProduct ."
    )
    res = find_slice_ambiguities(
        question="list coverage products by name", slice_graph=ttl)
    assert res["ambiguous"] is False, res["items"]
    # A genuine entity-discriminating measure (amount) on the SAME unrelated
    # classes still clarifies — the deferral is narrow to label attributes.
    ttl2 = _ttl(
        "ex:Party a rdfs:Class . ex:CoverageProduct a rdfs:Class . "
        "ex:amount rdfs:domain ex:Party . ex:amount rdfs:domain ex:CoverageProduct ."
    )
    res2 = find_slice_ambiguities(question="total amount", slice_graph=ttl2)
    assert res2["ambiguous"] is True


def test_single_domain_predicate_not_ambiguous():
    ttl = _ttl(
        "ex:Order a rdfs:Class . ex:amount rdfs:domain ex:Order ."
    )
    res = find_slice_ambiguities(question="total amount", slice_graph=ttl)
    assert res["ambiguous"] is False
    assert res["items"] == []


def test_two_class_paths_between_anchors_clarifies():
    # Customer connects to Policy two ways: directly (hasPolicy) and via Agent
    # (hasAgent + agentSells). Both anchors named in the question → clarify.
    ttl = _ttl(
        "ex:Customer a rdfs:Class . ex:Policy a rdfs:Class . ex:Agent a rdfs:Class . "
        "ex:hasPolicy rdfs:domain ex:Customer . ex:hasPolicy rdfs:range ex:Policy . "
        "ex:hasAgent rdfs:domain ex:Customer . ex:hasAgent rdfs:range ex:Agent . "
        "ex:agentSells rdfs:domain ex:Agent . ex:agentSells rdfs:range ex:Policy ."
    )
    res = find_slice_ambiguities(question="customer policy", slice_graph=ttl)
    assert res["ambiguous"] is True
    # the class-path item lists >1 traversal
    path_items = [i for i in res["items"] if "…" in i["term"]]
    assert path_items and len(path_items[0]["matches"]) > 1


def test_single_class_path_resolves():
    # Only one path Customer→Policy → not a choice to surface.
    ttl = _ttl(
        "ex:Customer a rdfs:Class . ex:Policy a rdfs:Class . "
        "ex:hasPolicy rdfs:domain ex:Customer . ex:hasPolicy rdfs:range ex:Policy ."
    )
    res = find_slice_ambiguities(question="customer policy", slice_graph=ttl)
    assert res["ambiguous"] is False
