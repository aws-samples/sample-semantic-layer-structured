from agents.shared.candidate_retrieval import resolve_to_iri
_ONT = {"classes": {"http://x/ont/customers": {"label": "customers"}},
        "properties": {"http://x/ont/customers/Segment": {"label": "Segment"}}}
def test_resolve_label_to_iri():
    assert resolve_to_iri("Customers", _ONT) == "http://x/ont/customers"
    assert resolve_to_iri("Segment", _ONT) == "http://x/ont/customers/Segment"
def test_resolve_passthrough_iri_and_unknown():
    assert resolve_to_iri("http://x/ont/orders", _ONT) == "http://x/ont/orders"
    assert resolve_to_iri("Nope", _ONT) == "Nope"


from agents.shared import candidate_retrieval as cr
def test_semantic_union_surfaces_value_referenced_class(monkeypatch):
    ont={"classes":{"http://x/customers":{"label":"customers"},
                    "http://x/orders":{"label":"orders"}},"properties":{}}
    vecs={"orders for SME":[1.0,0.0],"customers":[1.0,0.0],"orders":[0.0,1.0]}
    monkeypatch.setattr(cr,"embed_text",lambda t:vecs.get(t,[0.0,0.0]))
    out=cr.retrieve_candidates(question="orders for SME",ontology_json=ont,
        lexical=["http://x/orders"],k=5)
    assert "http://x/customers" in out and "http://x/orders" in out
def test_fail_soft_to_lexical_when_embed_raises(monkeypatch):
    def boom(t): raise RuntimeError("bedrock down")
    monkeypatch.setattr(cr,"embed_text",boom)
    out=cr.retrieve_candidates(question="q",
        ontology_json={"classes":{"http://x/a":{"label":"a"}},"properties":{}},
        lexical=["http://x/a"],k=5)
    assert out==["http://x/a"]
