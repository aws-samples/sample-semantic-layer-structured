"""Where the Hybrid KB/graph split actually falls.

The thin VKG keeps the KB document's PROSE SECTIONS (Business Purpose, Reference
Tables, Common Query Patterns, Sample Data, Notes) out of the graph — those are
what retrieval ranks, and the query path's CONSTRUCT would sweep any predicate on
a candidate class into every prompt. But a graph with no descriptive text at all
left the admin viewer's Description column showing an em-dash for every property,
so ONE capped ``rdfs:comment`` line per subject is now in scope.
"""

from agents.semantic_layer_creation_agent.vkg_builder import build_thin_vkg_nquads


def test_thin_vkg_emits_structure_and_mappings():
    """The structural backbone and Ontop mappings are emitted."""
    nq = build_thin_vkg_nquads(
        table="address",
        columns=[{"name": "state_tc"}],
        class_iri="http://x/Address",
    )
    assert "mapsToColumn" in nq and "Address" in nq


def test_thin_vkg_omits_comment_when_no_description_is_supplied():
    """No description in, no ``rdfs:comment`` out — the line is opt-in."""
    nq = build_thin_vkg_nquads(
        table="address",
        columns=[{"name": "state_tc"}],
        class_iri="http://x/Address",
    )
    assert "#comment" not in nq


def test_thin_vkg_emits_the_supplied_one_line_descriptions():
    """Class and column descriptions become ``rdfs:comment`` on their subjects."""
    nq = build_thin_vkg_nquads(
        table="address",
        columns=[{"name": "state_tc", "type": "string"}],
        class_iri="http://x/Address",
        graph_iri="http://g",
        class_comment="One row per postal address.",
        column_comments={"state_tc": "Two-letter US state code."},
    )
    assert (
        '<http://x/Address> <http://www.w3.org/2000/01/rdf-schema#comment> '
        '"One row per postal address." <http://g> .' in nq
    )
    assert (
        '<http://x/Address/state_tc> '
        '<http://www.w3.org/2000/01/rdf-schema#comment> '
        '"Two-letter US state code." <http://g> .' in nq
    )


def test_column_descriptions_match_case_insensitively():
    """Descriptions are harvested from model prose, which loses catalog casing."""
    nq = build_thin_vkg_nquads(
        table="address",
        columns=[{"name": "stateTc"}],
        class_iri="http://x/Address",
        column_comments={"statetc": "Two-letter US state code."},
    )
    assert "Two-letter US state code." in nq


def test_a_harvested_description_beats_the_catalog_comment():
    """The KB line wins; the Glue comment is only a fallback."""
    nq = build_thin_vkg_nquads(
        table="address",
        columns=[{"name": "state_tc", "comment": "from the catalog"}],
        class_iri="http://x/Address",
        column_comments={"state_tc": "from the KB"},
    )
    assert "from the KB" in nq and "from the catalog" not in nq


def test_the_catalog_comment_is_used_when_nothing_was_harvested():
    """A hand-annotated catalog is still worth surfacing."""
    nq = build_thin_vkg_nquads(
        table="address",
        columns=[{"name": "state_tc", "comment": "from the catalog"}],
        class_iri="http://x/Address",
    )
    assert "from the catalog" in nq


def test_descriptions_are_escaped_into_a_single_nquads_line():
    """Quotes, backslashes and newlines cannot appear raw in an N-Quads literal.

    The viewer parses the artifact line by line with a regex; an unescaped quote
    or a raw newline drops the whole triple, so the description would vanish AND
    take the property's other triples' parse with it.
    """
    nq = build_thin_vkg_nquads(
        table="address",
        columns=[{"name": "state_tc"}],
        class_iri="http://x/Address",
        column_comments={"state_tc": 'a "quoted" line\nand a \\ backslash'},
    )
    comment_lines = [line for line in nq.splitlines() if "#comment" in line]
    assert len(comment_lines) == 1
    assert '\\"quoted\\"' in comment_lines[0]
    assert "\\n" in comment_lines[0] and "\\\\" in comment_lines[0]


def test_relationship_descriptions_are_emitted_on_the_object_property():
    """The viewer's Relationships tab reads the same predicate."""
    nq = build_thin_vkg_nquads(
        table="orders",
        columns=[{"name": "customer_id"}],
        class_iri="http://x/orders",
        joins=[
            {
                "property": "http://x/orders/hasCustomer",
                "target_column": "orders.customer_id",
                "range": "http://x/customers",
                "label": "hasCustomer",
                "comment": "Inferred foreign key: orders.customer_id.",
            }
        ],
    )
    assert (
        '<http://x/orders/hasCustomer> '
        '<http://www.w3.org/2000/01/rdf-schema#comment> '
        '"Inferred foreign key: orders.customer_id."' in nq
    )
