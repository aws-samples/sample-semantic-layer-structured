from rdflib import Graph
from agents.ontology_query_agent.tier2.vkg_slice_builder import _truncate_by_centrality

def _big_slice_with_shape():
    B = "http://x/abc"
    g = Graph()
    g.parse(data=f'''
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{B}/Holding> a owl:Class .
<{B}/Holding/holding_status> a owl:DatatypeProperty ; rdfs:domain <{B}/Holding> .
<{B}/Holding/holding_statusShape> a sh:NodeShape ; sh:targetClass <{B}/Holding> ;
    sh:property <{B}/Holding/holding_statusShape/prop> .
<{B}/Holding/holding_statusShape/prop> sh:path <{B}/Holding/holding_status> ;
    sh:in ( "Active" "Inactive" "Closed" ) .
''' + "".join(
        f'<{B}/Pad{i}> a owl:Class ; rdfs:comment "{"x"*200}" .\n' for i in range(50)),
        format="turtle")
    return g, [f"{B}/Holding"]

def test_shape_survives_truncation():
    g, cands = _big_slice_with_shape()
    out = _truncate_by_centrality(g, candidates=cands, budget_chars=1500)
    ttl = out.serialize(format="turtle")
    assert "NodeShape" in ttl
    assert "Active" in ttl and "Inactive" in ttl and "Closed" in ttl

def test_truncation_still_drops_padding():
    # Sanity: truncation still happens (not everything kept).
    g, cands = _big_slice_with_shape()
    out = _truncate_by_centrality(g, candidates=cands, budget_chars=1500)
    ttl = out.serialize(format="turtle")
    assert ttl.count("Pad") < 50  # some padding evicted
