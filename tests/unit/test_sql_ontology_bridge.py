from rdflib import Graph
from agents.semantic_layer_query_agent.ontop_mappings import build_inverse_index
from agents.semantic_layer_query_agent.sql_ontology_bridge import sql_to_triples
VOCAB="https://example.org/vkg#"
TTL=f'@prefix vkg: <{VOCAB}> .\n<http://x/Address> vkg:mapsToTable "address" .\n<http://x/Address/state_tc> vkg:mapsToColumn "address.state_tc" .\n'
def _idx():
    g=Graph(); g.parse(data=TTL, format="turtle"); return build_inverse_index(g, vocab=VOCAB)
def _idx_with_join():
    ttl = TTL + '<http://x/Party> vkg:mapsToTable "party" .\n<http://x/Address/hasParty> vkg:mapsToColumn "address.party_id" .\n'
    g=Graph(); g.parse(data=ttl, format="turtle"); return build_inverse_index(g, vocab=VOCAB)
def test_maps_qualified_column():
    t = sql_to_triples("SELECT a.state_tc FROM address a", idx=_idx(), dialect="athena")
    assert ("http://x/Address","http://x/Address/state_tc") in t.triples
def test_unmapped_column_degrades():
    t = sql_to_triples("SELECT a.zzz FROM address a", idx=_idx(), dialect="athena")
    assert t.triples == [] and "zzz" in t.unmapped
def test_unparseable_sql_empty():
    t = sql_to_triples("NOT SQL ((", idx=_idx(), dialect="athena")
    assert t.triples == [] and t.parse_failed is True
def test_join_maps_to_object_property():
    t = sql_to_triples("SELECT * FROM address a JOIN party p ON a.party_id = p.party_id",
                       idx=_idx_with_join(), dialect="athena")
    assert any(pred.endswith("hasParty") for _c, pred in t.triples)


# --------------------------------------------------------------------------- #
# UNQUALIFIED columns. A single-table query needs no alias — plain SQL          #
# semantics resolve a bare column against the only table in scope, and models   #
# write it that way constantly ("SELECT COUNT(party_id) FROM normalized.party"). #
# Treating those as unmapped made the Phase-5 gate flag CORRECT columns, told    #
# the generator "Do NOT use party_id", and abstained on 8 of 15 eval scenarios   #
# holding perfectly valid SQL. Resolve them the way SQL does.                    #
# --------------------------------------------------------------------------- #
def test_unqualified_column_resolves_against_the_single_from_table():
    t = sql_to_triples("SELECT state_tc FROM address", idx=_idx(), dialect="athena")
    assert ("http://x/Address", "http://x/Address/state_tc") in t.triples
    assert t.unmapped == []


def test_unqualified_column_resolves_when_the_table_is_schema_qualified():
    # "normalized.address" is how the generator writes it: the sqlglot Table node
    # carries db="normalized", name="address", and the mapping index is keyed on
    # the BARE table name only.
    t = sql_to_triples("SELECT state_tc FROM normalized.address",
                       idx=_idx(), dialect="athena")
    assert ("http://x/Address", "http://x/Address/state_tc") in t.triples
    assert t.unmapped == []


def test_unqualified_column_absent_from_the_single_table_stays_unmapped():
    # Resolution must not become a rubber stamp: a bare column that is NOT a real
    # column of the only table in scope is still unverifiable.
    t = sql_to_triples("SELECT zzz FROM address", idx=_idx(), dialect="athena")
    assert t.triples == [] and "zzz" in t.unmapped


def test_unqualified_column_stays_unmapped_when_several_tables_are_in_scope():
    # With two tables in scope a bare column is genuinely ambiguous — pinning it
    # to a guess could ground a WRONG class, so it must stay unverifiable and be
    # routed back for qualification.
    t = sql_to_triples(
        "SELECT state_tc FROM address JOIN party ON address.party_id = party.party_id",
        idx=_idx_with_join(), dialect="athena",
    )
    assert "state_tc" in t.unmapped


def test_unqualified_aggregate_over_single_table_grounds():
    # The exact shape that abstained in the eval: no alias, aggregate + WHERE.
    ttl = TTL + '<http://x/Address/is_deleted> vkg:mapsToColumn "address.is_deleted" .\n'
    g = Graph()
    g.parse(data=ttl, format="turtle")
    idx = build_inverse_index(g, vocab=VOCAB)
    t = sql_to_triples(
        "SELECT COUNT(state_tc) FROM normalized.address WHERE is_deleted = false",
        idx=idx, dialect="athena",
    )
    assert t.unmapped == []
    assert len(t.triples) == 2
