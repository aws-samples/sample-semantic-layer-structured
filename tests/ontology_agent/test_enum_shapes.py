from agents.ontology_agent.enum_shapes import is_enum, MAX_ENUM_CARDINALITY
from agents.ontology_agent.enum_shapes import extract_enum_candidates

PHASE1 = """\
<http://x/abc/Holding/holding_status> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#DatatypeProperty> <http://x/abc> .
<http://x/abc/Holding/holding_status> <http://www.w3.org/2000/01/rdf-schema#domain> <http://x/abc/Holding> <http://x/abc> .
<http://x/abc/Holding/holding_status> <http://www.w3.org/2000/01/rdf-schema#range> <http://www.w3.org/2001/XMLSchema#string> <http://x/abc> .
<http://x/abc/Holding/holding_status> <https://semantic-layer.aws/virtual-kg/mapsToColumn> "holdings.holding_status" <http://x/abc> .
<http://x/abc/Holding/market_value> <http://www.w3.org/2000/01/rdf-schema#range> <http://www.w3.org/2001/XMLSchema#double> <http://x/abc> .
<http://x/abc/Holding/market_value> <https://semantic-layer.aws/virtual-kg/mapsToColumn> "holdings.market_value" <http://x/abc> .
"""


def test_small_categorical_set_is_enum():
    assert is_enum(["Active", "Inactive", "Closed"], truncated=False) is True


def test_high_cardinality_is_not_enum():
    vals = [f"V{i}" for i in range(MAX_ENUM_CARDINALITY + 1)]
    assert is_enum(vals, truncated=False) is False


def test_truncated_probe_is_not_enum():
    assert is_enum(["A", "B"], truncated=True) is False


def test_long_freetext_token_is_not_enum():
    assert is_enum(["Active", "x" * 60], truncated=False) is False


def test_empty_is_not_enum():
    assert is_enum([], truncated=False) is False


def test_returns_only_string_props_with_mapsto_column():
    cands = extract_enum_candidates(PHASE1)
    assert cands == [{
        "prop_iri": "http://x/abc/Holding/holding_status",
        "class_iri": "http://x/abc/Holding",
        "column": "holding_status", "table": "holdings",
        "graph": "http://x/abc",
    }]


def test_malformed_nquads_returns_empty():
    assert extract_enum_candidates("!!! not nquads !!!") == []


import rdflib
from agents.ontology_agent.enum_shapes import build_enum_shape_nquads
from agents.ontology_query_agent.tier2.enum_constraints import extract_enum_constraints


def test_sh_in_round_trips_in_order():
    g = "http://insurance/ontology/abc"
    cls, prop = f"{g}/Holding", f"{g}/Holding/holding_status"
    nq = build_enum_shape_nquads(class_iri=cls, prop_iri=prop,
                                 values=["Active", "Inactive", "Closed"], graph=g)
    # default_union=True: the quads live in the named graph ``g``; rdflib's
    # Turtle serializer emits only the default graph unless the dataset unions
    # its graphs, so this flag lets the round-trip see the named-graph data.
    ds = rdflib.Dataset(default_union=True); ds.parse(data=nq, format="nquads")  # must parse
    ttl = ds.serialize(format="turtle")
    cons = extract_enum_constraints(ttl)
    assert cons[prop]["values"] == ["Active", "Inactive", "Closed"]
    assert cons[prop]["class"] == cls


def test_every_line_is_in_named_graph():
    g = "http://x/abc"
    nq = build_enum_shape_nquads(class_iri=f"{g}/Holding", prop_iri=f"{g}/Holding/s",
                                 values=["A", "B"], graph=g)
    for line in [ln for ln in nq.splitlines() if ln.strip()]:
        assert line.rstrip().endswith(" .")
        assert f"<{g}>" in line


def test_value_with_quote_is_skipped_returns_empty():
    nq = build_enum_shape_nquads(class_iri="http://x/C", prop_iri="http://x/C/p",
                                 values=['Ac"tive'], graph="http://x")
    assert nq == ""


def test_empty_values_returns_empty():
    assert build_enum_shape_nquads(class_iri="http://x/C", prop_iri="http://x/C/p",
                                   values=[], graph="http://x") == ""


def test_same_column_name_two_tables_no_collision():
    g = "http://x/abc"
    a = build_enum_shape_nquads(class_iri=f"{g}/Holding", prop_iri=f"{g}/Holding/status",
                                values=["Active", "Closed"], graph=g)
    b = build_enum_shape_nquads(class_iri=f"{g}/Coverage", prop_iri=f"{g}/Coverage/status",
                                values=["Bound", "Lapsed"], graph=g)
    # default_union=True so Turtle serialization sees the named-graph quads.
    ds = rdflib.Dataset(default_union=True); ds.parse(data=a + b, format="nquads")
    cons = extract_enum_constraints(ds.serialize(format="turtle"))
    assert cons[f"{g}/Holding/status"]["values"] == ["Active", "Closed"]
    assert cons[f"{g}/Coverage/status"]["values"] == ["Bound", "Lapsed"]


import json
from agents.ontology_agent import main

PHASE1_TWO_COLS = """\
<http://x/abc/Holding/holding_status> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#DatatypeProperty> <http://x/abc> .
<http://x/abc/Holding/holding_status> <http://www.w3.org/2000/01/rdf-schema#domain> <http://x/abc/Holding> <http://x/abc> .
<http://x/abc/Holding/holding_status> <http://www.w3.org/2000/01/rdf-schema#range> <http://www.w3.org/2001/XMLSchema#string> <http://x/abc> .
<http://x/abc/Holding/holding_status> <https://semantic-layer.aws/virtual-kg/mapsToColumn> "holdings.holding_status" <http://x/abc> .
<http://x/abc/Holding/notes> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#DatatypeProperty> <http://x/abc> .
<http://x/abc/Holding/notes> <http://www.w3.org/2000/01/rdf-schema#domain> <http://x/abc/Holding> <http://x/abc> .
<http://x/abc/Holding/notes> <http://www.w3.org/2000/01/rdf-schema#range> <http://www.w3.org/2001/XMLSchema#string> <http://x/abc> .
<http://x/abc/Holding/notes> <https://semantic-layer.aws/virtual-kg/mapsToColumn> "holdings.notes" <http://x/abc> .
"""


def test_enum_shape_builder_appends_only_gated_columns(monkeypatch):
    def fake_probe(database, table, column, catalog):
        if column == "holding_status":
            return {"values": ["Active", "Closed"], "distinct_count": 2, "truncated": False, "error": None}
        return {"values": [f"V{i}" for i in range(30)], "distinct_count": 30, "truncated": True, "error": None}
    captured = {}
    def fake_append(ontology_id, table_name, nquads):
        captured["nquads"] = nquads
        return json.dumps({"success": True})
    monkeypatch.setattr(main, "select_distinct_values", fake_probe)
    monkeypatch.setattr(main, "append_enum_shape_nquads", fake_append)
    monkeypatch.setattr(main, "_read_phase1_nquads", lambda o, t: PHASE1_TWO_COLS)
    # call underlying fn of the @tool (Strands tools expose the raw fn; if direct call works use it)
    fn = getattr(main.enum_shape_builder, "entrypoint", main.enum_shape_builder)
    out = json.loads(fn(ontology_id="abc", table_name="holdings", database="db", catalog="cat"))
    assert out["enums_found"] == 1
    assert out["columns_probed"] == 2
    assert "holding_status" in captured["nquads"]
    assert "Active" in captured["nquads"]
    assert "notes" not in captured["nquads"]   # high-cardinality + truncated -> skipped


PHASE1_STATUS_AND_NAME = """\
<http://x/abc/Holding/holding_status> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#DatatypeProperty> <http://x/abc> .
<http://x/abc/Holding/holding_status> <http://www.w3.org/2000/01/rdf-schema#domain> <http://x/abc/Holding> <http://x/abc> .
<http://x/abc/Holding/holding_status> <http://www.w3.org/2000/01/rdf-schema#range> <http://www.w3.org/2001/XMLSchema#string> <http://x/abc> .
<http://x/abc/Holding/holding_status> <https://semantic-layer.aws/virtual-kg/mapsToColumn> "holdings.holding_status" <http://x/abc> .
<http://x/abc/Holding/full_name> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#DatatypeProperty> <http://x/abc> .
<http://x/abc/Holding/full_name> <http://www.w3.org/2000/01/rdf-schema#domain> <http://x/abc/Holding> <http://x/abc> .
<http://x/abc/Holding/full_name> <http://www.w3.org/2000/01/rdf-schema#range> <http://www.w3.org/2001/XMLSchema#string> <http://x/abc> .
<http://x/abc/Holding/full_name> <https://semantic-layer.aws/virtual-kg/mapsToColumn> "holdings.full_name" <http://x/abc> .
"""


def test_enum_shape_builder_gates_on_is_enum_column(monkeypatch):
    """full_name passes is_enum (small, short tokens) but is a name/near-unique
    column, so the precision gate (is_enum_column) must reject it while keeping
    holding_status."""
    def fake_probe(database, table, column, catalog):
        if column == "holding_status":
            return {"values": ["Active", "Closed"], "distinct_count": 2,
                    "truncated": False, "sample_rows": 200, "error": None}
        # full_name: 20 distinct over 25 rows -> high ratio + name-like -> not enum
        return {"values": [f"Name{i}" for i in range(20)], "distinct_count": 20,
                "truncated": False, "sample_rows": 25, "error": None}
    captured = {}
    def fake_append(ontology_id, table_name, nquads):
        captured["nquads"] = nquads
        return json.dumps({"success": True})
    monkeypatch.setattr(main, "select_distinct_values", fake_probe)
    monkeypatch.setattr(main, "append_enum_shape_nquads", fake_append)
    monkeypatch.setattr(main, "_read_phase1_nquads", lambda o, t: PHASE1_STATUS_AND_NAME)
    fn = getattr(main.enum_shape_builder, "entrypoint", main.enum_shape_builder)
    out = json.loads(fn(ontology_id="abc", table_name="holdings", database="db", catalog="cat"))
    assert out["columns_probed"] == 2
    assert out["enums_found"] == 1
    assert "holding_status" in captured["nquads"]
    assert "full_name" not in captured["nquads"]


def test_enum_shape_builder_no_candidates(monkeypatch):
    monkeypatch.setattr(main, "_read_phase1_nquads", lambda o, t: "")
    fn = getattr(main.enum_shape_builder, "entrypoint", main.enum_shape_builder)
    out = json.loads(fn(ontology_id="abc", table_name="holdings", database="db", catalog="cat"))
    assert out["enums_found"] == 0 and out["shapes_written"] == 0


def test_extract_skips_malformed_iri_line_keeps_rest():
    """One malformed FK IRI (literal spaces/parens) must not suppress the whole
    table's enum candidates — regression for the holding_status zero-shape bug."""
    nq = (
        '<http://x/abc/Holding/holding_status> '
        '<http://www.w3.org/1999/02/22-rdf-syntax-ns#type> '
        '<http://www.w3.org/2002/07/owl#DatatypeProperty> <http://x/abc> .\n'
        '<http://x/abc/Holding/holding_status> '
        '<http://www.w3.org/2000/01/rdf-schema#range> '
        '<http://www.w3.org/2001/XMLSchema#string> <http://x/abc> .\n'
        '<http://x/abc/Holding/holding_status> '
        '<https://semantic-layer.aws/virtual-kg/mapsToColumn> '
        '"holding.holding_status" <http://x/abc> .\n'
        '<http://x/abc/Holding/holding_status> '
        '<http://www.w3.org/2000/01/rdf-schema#domain> '
        '<http://x/abc/Holding> <http://x/abc> .\n'
        # Malformed: literal spaces + parens inside the IRI (Phase-2 FK defect).
        '<http://x/abc/Holding/hasHolding(self pk; bridge to party)> '
        '<http://www.w3.org/1999/02/22-rdf-syntax-ns#type> '
        '<http://www.w3.org/2002/07/owl#ObjectProperty> <http://x/abc> .\n'
    )
    cands = extract_enum_candidates(nq)
    cols = [c["column"] for c in cands]
    assert "holding_status" in cols  # survives despite the malformed sibling line


from agents.ontology_agent.enum_shapes import is_enum_column


def test_status_column_is_enum():
    assert is_enum_column("holding_status", ["Active", "Inactive", "Closed"],
                          distinct_count=3, sample_rows=200) is True


def test_type_code_column_is_enum():
    assert is_enum_column("party_type_code", ["IND", "ORG", "TRUST"],
                          distinct_count=3, sample_rows=200) is True


def test_full_name_is_not_enum():
    vals = [f"Name{i}" for i in range(20)]
    assert is_enum_column("full_name", vals, distinct_count=20, sample_rows=25) is False


def test_govt_id_is_not_enum():
    vals = [f"ID{i}" for i in range(15)]
    assert is_enum_column("govt_id", vals, distinct_count=15, sample_rows=20) is False


def test_low_ratio_unnamed_column_is_enum():
    assert is_enum_column("region", ["N", "S", "E", "W"],
                          distinct_count=4, sample_rows=500) is True


def test_near_unique_column_is_not_enum():
    # 24 distinct over 50 rows (ratio 0.48) is NOT a closed vocabulary.
    vals = [f"v{i}" for i in range(24)]
    assert is_enum_column("some_col", vals, distinct_count=24, sample_rows=50) is False


def test_empty_values_not_enum_column():
    assert is_enum_column("status", [], distinct_count=0, sample_rows=100) is False
