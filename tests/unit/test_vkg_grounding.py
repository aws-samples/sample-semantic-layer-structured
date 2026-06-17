"""Unit tests for the VKG (SPARQL) triple-context grounding gate.

Mirrors the RAG ``test_grounding`` suite, but for SPARQL/rdflib: a grounded
query returns ``[]``; an invented predicate / class is flagged; a predicate
valid on class A but used on class B is flagged (the §0.2 regression analog of
RAG's cross-table grounding bug); prefixed-name expansion works; property paths
degrade cleanly; and ``classify_missing`` routes out-of-slice→expand and
hallucinated→regenerate.
"""
from agents.ontology_query_agent.tier2.grounding import (
    check_grounding,
    classify_missing,
    extract_sparql_iris,
)

EX = "http://ex.com/"


def _slice_ttl() -> str:
    """Slice: Party + Policy classes; hasPremium domain=Policy; Policy SubClassOf Party? no.

    Party --hasName--> (domain Party). Policy --hasPremium--> (domain Policy).
    Customer SubClassOf Party. This lets us test the "valid on A, used on B"
    case and the subClassOf admission path.
    """
    return f"""
@prefix ex: <{EX}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

ex:Party a rdfs:Class .
ex:Policy a rdfs:Class .
ex:Customer a rdfs:Class ; rdfs:subClassOf ex:Party .
ex:hasName rdfs:domain ex:Party .
ex:hasPremium rdfs:domain ex:Policy .
"""


def test_grounded_sparql_returns_empty():
    sparql = (
        f"PREFIX ex: <{EX}> "
        "SELECT ?x WHERE { ?x a ex:Policy . ?x ex:hasPremium ?p }"
    )
    assert check_grounding(sparql=sparql, slice_graph_or_text=_slice_ttl()) == []


def test_invented_predicate_flagged_as_property():
    sparql = (
        f"PREFIX ex: <{EX}> "
        "SELECT ?x WHERE { ?x a ex:Policy . ?x ex:hasNonsense ?p }"
    )
    missing = check_grounding(sparql=sparql, slice_graph_or_text=_slice_ttl())
    assert f"property:{EX}hasNonsense" in missing


def test_invented_class_flagged_as_class():
    sparql = (
        f"PREFIX ex: <{EX}> "
        "SELECT ?x WHERE { ?x a ex:Spaceship . ?x ex:hasName ?n }"
    )
    missing = check_grounding(sparql=sparql, slice_graph_or_text=_slice_ttl())
    assert f"class:{EX}Spaceship" in missing


def test_predicate_valid_on_A_used_on_B_is_flagged():
    # hasPremium has domain Policy; using it on a Party subject must be flagged
    # (the §0.2 cross-class analog of RAG's cross-table false-negative bug).
    sparql = (
        f"PREFIX ex: <{EX}> "
        "SELECT ?x WHERE { ?x a ex:Party . ?x ex:hasPremium ?p }"
    )
    missing = check_grounding(sparql=sparql, slice_graph_or_text=_slice_ttl())
    assert any(m.startswith(f"property:{EX}hasPremium on ") for m in missing), missing


def test_subclass_admits_superclass_domain():
    # hasName has domain Party; Customer SubClassOf Party → using hasName on a
    # Customer subject is grounded (domain admission walks the subClassOf chain).
    sparql = (
        f"PREFIX ex: <{EX}> "
        "SELECT ?x WHERE { ?x a ex:Customer . ?x ex:hasName ?n }"
    )
    assert check_grounding(sparql=sparql, slice_graph_or_text=_slice_ttl()) == []


def test_unresolved_subject_falls_back_to_membership():
    # No rdf:type on ?x → can't qualify; hasPremium IS in the slice → grounds
    # (best-effort slice-wide membership, like an unqualified SQL column).
    sparql = f"PREFIX ex: <{EX}> SELECT ?x WHERE {{ ?x ex:hasPremium ?p }}"
    assert check_grounding(sparql=sparql, slice_graph_or_text=_slice_ttl()) == []


def test_prefixed_name_expansion_uses_slice_prefixes():
    # The query declares no PREFIX; the slice binds ex: → grounding still works.
    sparql = "SELECT ?x WHERE { ?x a ex:Policy . ?x ex:hasPremium ?p }"
    assert check_grounding(sparql=sparql, slice_graph_or_text=_slice_ttl()) == []


def test_property_path_degrades_cleanly():
    sparql = (
        f"PREFIX ex: <{EX}> "
        "SELECT ?x WHERE { ?x ex:hasName/ex:hasPremium ?p }"
    )
    # Property path → can't pin a single predicate IRI → degrade (return []).
    assert check_grounding(sparql=sparql, slice_graph_or_text=_slice_ttl()) == []


def test_unparseable_sparql_degrades_cleanly():
    assert check_grounding(sparql="NOT SPARQL AT ALL {{{",
                           slice_graph_or_text=_slice_ttl()) == []


def test_extract_sparql_iris_returns_triple_pairs():
    sparql = (
        f"PREFIX ex: <{EX}> "
        "SELECT ?x WHERE { ?x a ex:Policy . ?x ex:hasPremium ?p }"
    )
    out = extract_sparql_iris(sparql, prefixes={"ex": EX})
    assert f"{EX}hasPremium" in out["predicates"]
    assert f"{EX}Policy" in out["classes"]
    # hasPremium paired with its subject's class (Policy)
    assert (f"{EX}Policy", f"{EX}hasPremium") in out["triples"]


def test_classify_missing_routes_out_of_slice_to_expand():
    # hasPremium is a Phase-1 candidate but missing from the slice → expand.
    missing = [f"property:{EX}hasPremium"]
    out = classify_missing(missing, candidates=[f"{EX}hasPremium"])
    assert out["expand"] == [f"{EX}hasPremium"]
    assert out["regenerate"] == []


def test_classify_missing_routes_hallucinated_to_regenerate():
    missing = [f"property:{EX}hasNonsense"]
    out = classify_missing(missing, candidates=[f"{EX}hasPremium"])
    assert out["regenerate"] == [f"{EX}hasNonsense"]
    assert out["expand"] == []


def test_classify_missing_misused_predicate_always_regenerates():
    # A "valid on A, used on B" miss is a generation error — regenerate even if
    # the predicate IRI is a Phase-1 candidate (it already exists in the slice).
    missing = [f"property:{EX}hasPremium on {EX}Party"]
    out = classify_missing(missing, candidates=[f"{EX}hasPremium"])
    assert out["regenerate"] == [f"{EX}hasPremium"]
    assert out["expand"] == []


def test_classify_missing_uses_neptune_probe_when_no_candidate():
    missing = [f"property:{EX}hasPremium"]
    out = classify_missing(missing, candidates=[],
                           neptune_probe=lambda iri: True)
    assert out["expand"] == [f"{EX}hasPremium"]


def test_classify_missing_defaults_to_regenerate_without_probe():
    missing = [f"property:{EX}hasPremium"]
    out = classify_missing(missing, candidates=[])
    assert out["regenerate"] == [f"{EX}hasPremium"]
    assert out["expand"] == []
