from rdflib import Graph
from agents.semantic_layer_query_agent.grounding import verify_sql_grounding
# slice: predicate state_tc has rdfs:domain Address
TTL='''@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://x/Address/state_tc> rdfs:domain <http://x/Address> .'''
def _g():
    g=Graph(); g.parse(data=TTL, format="turtle"); return g
def test_valid_domain_grounds():
    m = verify_sql_grounding(triples=[("http://x/Address","http://x/Address/state_tc")], slice_graph=_g())
    assert m == []
def test_predicate_on_wrong_class_flagged():
    m = verify_sql_grounding(triples=[("http://x/Party","http://x/Address/state_tc")], slice_graph=_g())
    assert any("state_tc" in x for x in m)
