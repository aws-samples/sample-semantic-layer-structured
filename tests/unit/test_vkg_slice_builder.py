"""Unit tests for the VKG Phase 2 slice builder + centrality truncation."""
from unittest.mock import MagicMock

from rdflib import Graph, Literal, RDFS, URIRef

from agents.ontology_query_agent.tier2.vkg_slice_builder import (
    VkgSliceBuilder,
    _truncate_by_centrality,
)


def _seed_graph(triples):
    g = Graph()
    for s, p, o in triples:
        g.add((URIRef(s), URIRef(p), o if isinstance(o, Literal) else URIRef(o)))
    return g


def test_build_calls_neptune_construct_and_returns_ttl():
    neptune = MagicMock()
    neptune.construct.return_value = _seed_graph([
        ("ex:Customer", str(RDFS.label), Literal("Customer")),
    ])
    judge = MagicMock()

    def token_count(s):
        return len(s) // 4

    b = VkgSliceBuilder(
        neptune=neptune, judge_fn=judge, token_counter=token_count,
        budget=12000, n_hops=2,
    )
    ttl = b.build(candidates=["ex:Customer"], namespace="ns")
    assert "Customer" in ttl
    neptune.construct.assert_called_once()


def test_truncation_drops_lowest_centrality_until_under_budget():
    g = _seed_graph([
        ("ex:A", "ex:p1", "ex:B"), ("ex:B", "ex:p1", "ex:C"),
        ("ex:Iso", str(RDFS.label), Literal("isolated")),
    ])
    out = _truncate_by_centrality(g, candidates=["ex:A"], budget_chars=80)
    serialized = out.serialize(format="turtle")
    assert len(serialized) <= 80
    assert "isolated" not in serialized


def test_truncation_force_keeps_candidate_even_when_low_centrality():
    # A large hub (ex:Hub) dominates centrality; the question's own candidate
    # class (ex:Target) is a low-degree leaf. Under a tight budget, centrality
    # ranking alone would evict ex:Target — but it MUST be force-kept, else the
    # slice judge rejects an answerable question forever (VKG analog of the RAG
    # _fit eviction bug).
    triples = [("ex:Hub", f"ex:p{i}", f"ex:N{i}") for i in range(12)]
    triples += [(f"ex:N{i}", str(RDFS.label), Literal(f"node{i}")) for i in range(12)]
    triples += [
        ("ex:Target", str(RDFS.label), Literal("target")),
        ("ex:Target/name", str(RDFS.domain), "ex:Target"),  # the property asked for
    ]
    g = _seed_graph(triples)
    full = len(g.serialize(format="turtle"))
    # Budget well under the full graph so truncation MUST drop something.
    out = _truncate_by_centrality(g, candidates=["ex:Target"], budget_chars=full // 2)
    serialized = out.serialize(format="turtle")
    assert "target" in serialized                 # the candidate class survived
    assert "ex:Target/name" in serialized or "name" in serialized  # its property too


def test_is_sufficient_consults_judge():
    judge = MagicMock(return_value={"sufficient": False, "missing": ["ex:Z"]})
    b = VkgSliceBuilder(
        neptune=MagicMock(), judge_fn=judge,
        token_counter=lambda s: 1, budget=12000, n_hops=2,
    )
    ok, missing = b.is_sufficient(slice_text="x", question="q?")
    assert ok is False and missing == ["ex:Z"]


def test_flatten_slice_for_judge_projects_classes_props_comments():
    """The judge view flattens Turtle into Class: [prop (range) — comment] blocks."""
    from agents.ontology_query_agent.tier2.vkg_slice_builder import flatten_slice_for_judge
    ttl = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://ex/Party> a owl:Class ; rdfs:comment "A person or org." .
<http://ex/Party/party_type> a owl:DatatypeProperty ;
    rdfs:domain <http://ex/Party> ; rdfs:range <http://ex/string> ;
    rdfs:comment "Readable category." .
<http://ex/Holding> a owl:Class .
<http://ex/Holding/hasParty> a owl:ObjectProperty ;
    rdfs:domain <http://ex/Holding> ; rdfs:range <http://ex/Party> .
"""
    out = flatten_slice_for_judge(ttl)
    # local names present (not full IRIs)
    assert "Party" in out and "Holding" in out
    assert "party_type" in out and "hasParty" in out
    # comment + range surfaced
    assert "Readable category." in out
    assert "range: Party" in out  # object property range local name
    # full IRIs are NOT in the judge view (generator keeps those, judge doesn't need them)
    assert "http://ex/Party/party_type" not in out


def test_flatten_slice_for_judge_bad_turtle_returns_empty():
    """Unparseable input returns '' so the caller falls back to the raw slice."""
    from agents.ontology_query_agent.tier2.vkg_slice_builder import flatten_slice_for_judge
    assert flatten_slice_for_judge("not turtle {{{") == ""
    assert flatten_slice_for_judge("") == ""


def test_is_sufficient_feeds_flattened_view_to_judge():
    """is_sufficient hands the judge the FLAT view, not the raw TTL."""
    from agents.ontology_query_agent.tier2.vkg_slice_builder import VkgSliceBuilder
    ttl = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://ex/Party> a owl:Class ; rdfs:comment "who" .
<http://ex/Party/name> a owl:DatatypeProperty ; rdfs:domain <http://ex/Party> .
"""
    seen = {}

    def judge(payload):
        seen["slice"] = payload["slice"]
        return {"sufficient": True, "missing": []}

    b = VkgSliceBuilder(neptune=None, judge_fn=judge,
                        token_counter=lambda s: len(s) // 4, budget=12000, n_hops=2)
    ok, missing = b.is_sufficient(slice_text=ttl, question="party names")
    assert ok is True
    # judge saw the flattened view (local-name block), not the @prefix Turtle
    assert "@prefix" not in seen["slice"]
    assert "Party" in seen["slice"] and "name" in seen["slice"]
