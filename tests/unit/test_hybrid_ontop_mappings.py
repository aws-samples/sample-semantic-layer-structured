from rdflib import Graph
from agents.semantic_layer_query_agent.ontop_mappings import build_inverse_index
VOCAB = "https://example.org/vkg#"
TTL = f'''
@prefix vkg: <{VOCAB}> .
<http://x/Address> vkg:mapsToTable "address" .
<http://x/Address/state_tc> vkg:mapsToColumn "address.state_tc" .
'''
def test_inverse_index():
    g = Graph(); g.parse(data=TTL, format="turtle")
    idx = build_inverse_index(g, vocab=VOCAB)
    assert idx.class_by_table["address"] == "http://x/Address"
    assert idx.pred_by_column["address.state_tc"] == "http://x/Address/state_tc"
