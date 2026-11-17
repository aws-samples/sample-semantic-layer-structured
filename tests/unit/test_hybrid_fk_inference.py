"""Relationship inference for the Hybrid thin VKG.

A Hybrid layer's graph is built with no model in the loop, and it never emitted a
single ``owl:ObjectProperty`` — the viewer reported "No relationships found" and
the query side had no declared join edges. There is no catalog FK metadata to
read (Glue's ``get_table`` returns none, Iceberg exposes none), so the edges are
inferred from the TABLE and COLUMN NAMES of the layer's own tables.

These tests pin the two inference rules, the deliberate non-matches (which is
where a naive substring rule invents edges), and the property-naming contract.
"""
from agents.semantic_layer_creation_agent import fk_inference


# The `bird_card_games` layer's REAL column names, as the Glue catalog reports
# them: the source schema is camelCase but the catalog folds everything to lower
# case, so `setCode` arrives as `setcode` with no word boundary left. Using the
# pretty camelCase spelling here would pass while the live layer inferred nothing.
BIRD_CARDS = {
    "cards": ["id", "uuid", "name", "setcode", "multiverseid", "mcmid"],
    "sets": ["id", "code", "name", "keyrunecode", "parentcode", "mcmid"],
    "set_translations": ["id", "setcode", "language", "translation"],
    "foreign_data": ["id", "uuid", "multiverseid", "language"],
    "legalities": ["id", "uuid", "format", "status"],
    "rulings": ["id", "uuid", "date", "text"],
}


def _edge(edges, source_table, source_column, target_table):
    """Return the single edge matching a (source table, column, target) triple.

    Args:
        edges: The edge list returned by ``infer_joins``.
        source_table: Expected source table.
        source_column: Expected source column.
        target_table: Expected target table.

    Returns:
        dict | None: The matching edge, or ``None`` when there is none.
    """
    for edge in edges:
        if (
            edge["source_table"] == source_table
            and edge["source_column"] == source_column
            and edge["target_table"] == target_table
        ):
            return edge
    return None


def test_tokenize_splits_camel_snake_and_acronyms():
    assert fk_inference.tokenize("setCode") == ["set", "code"]
    assert fk_inference.tokenize("set_code") == ["set", "code"]
    assert fk_inference.tokenize("cardUUID") == ["card", "uuid"]
    assert fk_inference.tokenize("Card-ID") == ["card", "id"]


def test_reference_rule_grounds_lowercased_camel_fk_on_the_plural_targets_column():
    """``cards.setcode`` -> ``sets.code``: re-segmented, singularized, renamed."""
    edges = fk_inference.infer_joins(BIRD_CARDS)
    edge = _edge(edges, "cards", "setcode", "sets")
    assert edge is not None
    assert edge["target_column"] == "code"
    assert edge["rule"] == "reference"
    # The same FK exists on the second table that carries it.
    assert _edge(edges, "set_translations", "setcode", "sets") is not None


def test_a_run_whose_head_names_no_table_is_not_a_reference():
    """`keyrunecode` / `parentcode` / `mcmid` name nothing in the layer."""
    edges = fk_inference.infer_joins(BIRD_CARDS)
    assert not [e for e in edges if e["source_column"] in
                ("keyrunecode", "parentcode", "mcmid")]


def test_segmented_shared_identifier_is_paired_when_the_remainder_is_substantial():
    """`multiverseid` is a real shared key; the catalog just lost its boundary."""
    edges = fk_inference.infer_joins(BIRD_CARDS)
    assert _edge(edges, "cards", "multiverseid", "foreign_data") is not None


def test_english_words_ending_in_an_identifier_token_are_not_segmented():
    """`valid` -> `val` + `id` must not become an identifier column."""
    assert fk_inference._is_identifier_column("valid") is False
    assert fk_inference._is_identifier_column("paid") is False
    assert fk_inference._is_identifier_column("multiverseid") is True


def test_short_ambiguous_suffixes_are_never_segmented():
    """`risk`/`task`/`desk` all end in `sk`; segmenting them invents identifiers."""
    for word in ("risk", "task", "desk", "disk"):
        assert fk_inference._is_identifier_column(word) is False
    # An explicit boundary still counts.
    assert fk_inference._is_identifier_column("order_sk") is True


def test_shared_identifier_rule_is_symmetric():
    """Nothing in ``uuid`` says which table owns it, so BOTH directions exist."""
    edges = fk_inference.infer_joins(BIRD_CARDS)
    assert _edge(edges, "cards", "uuid", "rulings") is not None
    assert _edge(edges, "rulings", "uuid", "cards") is not None


def test_bare_surrogate_keys_never_pair_across_tables():
    """`id` is each table's OWN key — pairing it links every table to every other."""
    edges = fk_inference.infer_joins(BIRD_CARDS)
    assert not [e for e in edges if e["source_column"] == "id"]


def test_value_columns_that_merely_end_in_id_letters_are_not_edges():
    """A substring rule reads `valid`/`hybrid` as "ends in id" and invents edges."""
    edges = fk_inference.infer_joins(
        {
            "orders": ["valid", "hybrid", "paid"],
            "id": ["valid"],
        }
    )
    assert edges == []


def test_reference_to_a_table_outside_the_layer_is_dropped():
    """An edge needs a class to point at; a table not in the layer has none."""
    edges = fk_inference.infer_joins({"orders": ["id", "customer_id", "total"]})
    assert edges == []


def test_unresolvable_reference_is_dropped_not_guessed():
    """The target names no matching column, so grounding it would be a guess."""
    edges = fk_inference.infer_joins(
        {
            "orders": ["order_id", "customer_id"],
            "customers": ["name", "email"],  # no id / customer_id / customer
        }
    )
    assert _edge(edges, "orders", "customer_id", "customers") is None


def test_ambiguous_target_name_is_dropped():
    """`set` and `sets` both normalize to `set`; picking one mis-grounds silently."""
    edges = fk_inference.infer_joins(
        {
            "set": ["code"],
            "sets": ["code"],
            "cards": ["set_code"],
        }
    )
    assert not [e for e in edges if e["source_table"] == "cards"]


def test_two_columns_to_the_same_target_get_distinct_property_names():
    """Otherwise one property IRI silently overwrites the other."""
    edges = fk_inference.infer_joins(
        {
            "flights": ["id", "origin_airport_id", "dest_airport_id"],
            "airports": ["id", "name"],
        }
    )
    names = sorted(e["property_name"] for e in edges if e["source_table"] == "flights")
    assert len(names) == 2 and len(set(names)) == 2
    assert all(name.startswith("hasAirportsVia") for name in names)


def test_single_edge_property_name_is_unqualified():
    edges = fk_inference.infer_joins(
        {"orders": ["id", "customer_id"], "customers": ["id", "name"]}
    )
    assert [e["property_name"] for e in edges] == ["hasCustomers"]


def test_a_widely_shared_identifier_is_rejected():
    """Sharing beyond the cap means a generic value column; symmetric emit is n^2."""
    tables = {f"t{i}": ["batch_code"] for i in range(fk_inference.MAX_SHARED_TABLES + 2)}
    assert fk_inference.infer_joins(tables) == []


def test_edges_are_sorted_for_byte_identical_rebuilds():
    edges = fk_inference.infer_joins(BIRD_CARDS)
    keys = [(e["source_table"], e["property_name"]) for e in edges]
    assert keys == sorted(keys)


def test_key_columns_by_table_collects_referenced_columns_per_target():
    """These are exactly the columns worth MEASURING for uniqueness."""
    edges = fk_inference.infer_joins(BIRD_CARDS)
    candidates = fk_inference.key_columns_by_table(edges)
    assert candidates["sets"] == ["code"]
    # Both shared identifiers on `cards` are measured; only the one that comes
    # back unique-and-non-null can become its declared key.
    assert candidates["cards"] == ["multiverseid", "uuid"]
