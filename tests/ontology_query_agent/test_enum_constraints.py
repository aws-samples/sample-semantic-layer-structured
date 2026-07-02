from agents.ontology_query_agent.tier2.enum_constraints import (
    extract_enum_constraints,
    inject_enum_filters,
)

SLICE_TTL = """\
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://x/abc/> .
ex:HoldingStatusShape a sh:NodeShape ;
  sh:targetClass ex:Holding ;
  sh:property [ sh:path <http://x/abc/Holding/holding_status> ;
                sh:in ( "Active" "Inactive" "Closed" ) ] .
"""


def test_parses_sh_in_to_constraint_map():
    cons = extract_enum_constraints(SLICE_TTL)
    assert cons == {
        "http://x/abc/Holding/holding_status": {
            "class": "http://x/abc/Holding",
            "values": ["Active", "Inactive", "Closed"],
        }
    }


def test_preserves_value_order():
    cons = extract_enum_constraints(SLICE_TTL)
    assert cons["http://x/abc/Holding/holding_status"]["values"] == ["Active", "Inactive", "Closed"]


def test_malformed_slice_returns_empty():
    assert extract_enum_constraints("!!! not turtle !!!") == {}


def test_slice_without_shapes_returns_empty():
    assert extract_enum_constraints("@prefix ex: <http://x/> .\nex:A a ex:B .") == {}


def test_accepts_graph_object():
    import rdflib
    g = rdflib.Graph(); g.parse(data=SLICE_TTL, format="turtle")
    cons = extract_enum_constraints(g)
    assert "http://x/abc/Holding/holding_status" in cons


CONS = {"http://x/abc/Holding/holding_status":
        {"class": "http://x/abc/Holding", "values": ["Active", "Inactive", "Closed"]}}
SPARQL = ('SELECT ?status WHERE { ?h a <http://x/abc/Holding> ; '
          '<http://x/abc/Holding/holding_status> ?status . }')


def test_injects_filter_when_question_names_value():
    out, injected = inject_enum_filters(SPARQL, "active holdings", CONS)
    assert 'FILTER' in out and 'Active' in out and '?status' in out
    assert injected and injected[0]["value"] == "Active"
    assert injected[0]["var"] == "status"
    # result still parses
    from agents.ontology_query_agent.tier2.grounding import extract_sparql_iris
    assert extract_sparql_iris(out) is not None


def test_no_injection_when_value_not_named():
    out, injected = inject_enum_filters(SPARQL, "all holdings by party", CONS)
    assert injected == [] and out == SPARQL


def test_no_injection_when_filter_already_present():
    q = SPARQL[:-1] + 'FILTER(?status = "Active") }'
    out, injected = inject_enum_filters(q, "active holdings", CONS)
    assert injected == []


def test_no_injection_when_class_not_bound():
    q = 'SELECT ?s WHERE { ?h <http://x/abc/Holding/holding_status> ?s . }'
    out, injected = inject_enum_filters(q, "active", CONS)
    assert injected == []


def test_no_injection_when_two_values_named():
    out, injected = inject_enum_filters(SPARQL, "active and closed holdings", CONS)
    assert injected == [] and out == SPARQL


def test_no_injection_when_property_not_in_query():
    q = 'SELECT ?h WHERE { ?h a <http://x/abc/Holding> . }'
    out, injected = inject_enum_filters(q, "active holdings", CONS)
    assert injected == []


def test_filter_lands_inside_where_with_group_by():
    q = ('SELECT ?p (SUM(?v) AS ?t) WHERE { ?h a <http://x/abc/Holding> ; '
         '<http://x/abc/Holding/holding_status> ?status ; '
         '<http://x/abc/Holding/market_value> ?v ; '
         '<http://x/abc/Holding/party> ?p . } GROUP BY ?p')
    out, injected = inject_enum_filters(q, "active holdings by party", CONS)
    assert injected and 'Active' in out
    assert out.index('FILTER') < out.index('GROUP BY')
    from agents.ontology_query_agent.tier2.grounding import extract_sparql_iris
    assert extract_sparql_iris(out) is not None


def test_skips_nested_select():
    q = ('SELECT ?status WHERE { { SELECT ?h WHERE { ?h a <http://x/abc/Holding> } } '
         '?h <http://x/abc/Holding/holding_status> ?status . }')
    out, injected = inject_enum_filters(q, "active", CONS)
    assert injected == [] and out == q


def test_finalize_injects_active_filter_for_gt03():
    from agents.ontology_agent.enum_shapes import build_enum_shape_nquads
    import rdflib
    from agents.ontology_query_agent.tier2 import vkg_query_generator as vg

    B = "http://x/abc"
    cls, prop = f"{B}/Holding", f"{B}/Holding/holding_status"
    nq = build_enum_shape_nquads(class_iri=cls, prop_iri=prop,
                                 values=["Active", "Inactive", "Closed"], graph=B)
    ds = rdflib.Dataset(default_union=True); ds.parse(data=nq, format="nquads")
    slice_ttl = ds.serialize(format="turtle")  # slice carries the sh:in shape

    sparql = (f'SELECT ?status WHERE {{ ?h a <{cls}> ; '
              f'<{prop}> ?status . }}')
    gen = vg.VkgQueryGenerator(agent_factory=lambda: None)
    out = gen._finalize(sparql, slice_ttl, "total value of active holdings by party", "")
    assert "FILTER" in out and "Active" in out and "?status" in out
