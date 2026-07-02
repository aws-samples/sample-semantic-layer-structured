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


def test_unique_shortest_path_resolves_not_clarifies():
    # Shortest-path-wins (gt-03 fix): Customer connects to Policy two ways — a
    # DIRECT edge (hasPolicy, 2 nodes) and a longer detour via Agent (hasAgent +
    # agentSells, 3 nodes). The direct path is strictly shortest, so it is the
    # intended join: resolve it, do NOT ask the user to disambiguate.
    ttl = _ttl(
        "ex:Customer a rdfs:Class . ex:Policy a rdfs:Class . ex:Agent a rdfs:Class . "
        "ex:hasPolicy rdfs:domain ex:Customer . ex:hasPolicy rdfs:range ex:Policy . "
        "ex:hasAgent rdfs:domain ex:Customer . ex:hasAgent rdfs:range ex:Agent . "
        "ex:agentSells rdfs:domain ex:Agent . ex:agentSells rdfs:range ex:Policy ."
    )
    res = find_slice_ambiguities(question="customer policy", slice_graph=ttl)
    assert res["ambiguous"] is False, res["items"]
    # The unique shortest path is recorded as a heuristic resolution
    # (local names are lower-cased by _local_name).
    assert res["resolved"].get("customer…policy") == "customer→policy"


def test_tied_shortest_paths_resolve_deterministically_not_clarify():
    # gt-03 fix: even a GENUINE tie (Customer reaches Policy via two equal-length
    # bridges, via Agent and via Broker) must NOT escalate — a join-path choice is
    # never surfaced to the user (the flat-KB metadata agent never asks). Pick one
    # deterministically (lexicographically-smallest path) and record it; the
    # generator + grounding gate own the final decision.
    ttl = _ttl(
        "ex:Customer a rdfs:Class . ex:Policy a rdfs:Class . "
        "ex:Agent a rdfs:Class . ex:Broker a rdfs:Class . "
        "ex:hasAgent rdfs:domain ex:Customer . ex:hasAgent rdfs:range ex:Agent . "
        "ex:agentSells rdfs:domain ex:Agent . ex:agentSells rdfs:range ex:Policy . "
        "ex:hasBroker rdfs:domain ex:Customer . ex:hasBroker rdfs:range ex:Broker . "
        "ex:brokerSells rdfs:domain ex:Broker . ex:brokerSells rdfs:range ex:Policy ."
    )
    res = find_slice_ambiguities(question="customer policy", slice_graph=ttl)
    assert res["ambiguous"] is False, res["items"]
    # Path runs from anchor a (customer); lexicographically-smallest tie-break
    # picks the agent bridge over the broker bridge.
    assert res["resolved"].get("customer…policy") == "customer→agent→policy"


def test_head_noun_term_collision_does_not_clarify():
    # gt-07 fix: a term that NAMES a class — exactly OR as a name-component (head
    # noun, e.g. 'product' → CoverageProduct) — is an entity reference, not an
    # attribute choice. A predicate collision on such a term must NOT escalate.
    # 'product' is NOT an inflection of 'coverageproduct' but IS a name-component,
    # so _term_names_class (not _term_matches) is what makes this defer.
    ttl = _ttl(
        "ex:CoverageProduct a rdfs:Class . ex:Holding a rdfs:Class . "
        "ex:productCode rdfs:domain ex:CoverageProduct . "
        "ex:productCode rdfs:domain ex:Holding ."
    )
    res = find_slice_ambiguities(
        question="coverage product names", slice_graph=ttl)
    assert res["ambiguous"] is False, res["items"]


def test_term_names_class_component_and_stem():
    # Unit-level guard for the head-noun matcher: component + stem matches that
    # plain inflection misses (the gt-07 root cause).
    from agents.ontology_query_agent.tier2.slice_disambiguation import (
        _term_names_class,
    )
    assert _term_names_class("product", "coverageproduct") is True   # component
    assert _term_names_class("product", "policyproduct") is True     # component
    assert _term_names_class("hold", "holding") is True              # stem prefix
    assert _term_names_class("party", "party") is True               # exact
    assert _term_names_class("id", "holding") is False               # too short
    assert _term_names_class("market", "party") is False             # unrelated


def test_single_class_path_resolves():
    # Only one path Customer→Policy → not a choice to surface.
    ttl = _ttl(
        "ex:Customer a rdfs:Class . ex:Policy a rdfs:Class . "
        "ex:hasPolicy rdfs:domain ex:Customer . ex:hasPolicy rdfs:range ex:Policy ."
    )
    res = find_slice_ambiguities(question="customer policy", slice_graph=ttl)
    assert res["ambiguous"] is False
