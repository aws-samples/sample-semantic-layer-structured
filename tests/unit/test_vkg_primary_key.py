"""Tests for the thin VKG builder's verified-primary-key emission.

The key triple is what stops Ontop guessing row identity by column name. These
tests pin the emitted shape, the opt-in default (no key unless measured), and the
validation that a key may only name a column the mapping actually declares.
"""

import pytest

from agents.semantic_layer_creation_agent.ontop_mappings import HAS_PRIMARY_KEY_PRED
from agents.semantic_layer_creation_agent.vkg_builder import build_thin_vkg_nquads

COLUMNS = [
    {"name": "aboutme", "type": "string"},
    {"name": "id", "type": "bigint"},
    {"name": "user_id", "type": "bigint"},
]


def test_no_key_triple_when_uniqueness_was_not_measured() -> None:
    """Absent measurement, emit nothing — an unverified key is worse than none.

    Its presence would additionally license Ontop to drop de-duplication.
    """
    nquads = build_thin_vkg_nquads(
        table="posts", columns=COLUMNS, class_iri="http://x/Post"
    )
    assert HAS_PRIMARY_KEY_PRED not in nquads


def test_verified_key_is_emitted_in_the_dotted_translator_shape() -> None:
    """The translator parses dotted ``table.column`` literals."""
    nquads = build_thin_vkg_nquads(
        table="posts",
        columns=COLUMNS,
        class_iri="http://x/Post",
        graph_iri="http://g",
        key_columns=["user_id"],
    )
    assert (
        f'<http://x/Post> {HAS_PRIMARY_KEY_PRED} "posts.user_id" <http://g> .'
        in nquads
    )


def test_composite_key_preserves_declaration_order() -> None:
    """Order sets the subject template's column order, which must be stable."""
    nquads = build_thin_vkg_nquads(
        table="enrolment",
        columns=[{"name": "student_id"}, {"name": "course_id"}],
        class_iri="http://x/E",
        key_columns=["student_id", "course_id"],
    )
    assert '"enrolment.student_id, enrolment.course_id"' in nquads


def test_key_naming_an_unmapped_column_fails_loudly() -> None:
    """Otherwise the subject template references a column the mapping never declares.

    Ontop would then fail with COLUMN_NOT_FOUND at query time — long after the
    build reported success.
    """
    with pytest.raises(ValueError, match="COLUMN_NOT_FOUND"):
        build_thin_vkg_nquads(
            table="posts",
            columns=COLUMNS,
            class_iri="http://x/Post",
            key_columns=["nope"],
        )


def test_key_triple_lands_in_the_same_graph_as_the_table_mapping() -> None:
    """A triple in another graph is invisible to the graph-scoped CONSTRUCT."""
    nquads = build_thin_vkg_nquads(
        table="posts",
        columns=COLUMNS,
        class_iri="http://x/Post",
        graph_iri="http://g",
        key_columns=["id"],
    )
    key_line = next(line for line in nquads.splitlines() if HAS_PRIMARY_KEY_PRED in line)
    table_line = next(line for line in nquads.splitlines() if "mapsToTable" in line)
    assert key_line.endswith("<http://g> .")
    assert table_line.endswith("<http://g> .")


def test_key_triple_does_not_disturb_the_inverse_mapping_index() -> None:
    """The query side matches by exact predicate IRI, so the key must be ignored."""
    from rdflib import Dataset, URIRef

    from agents.semantic_layer_query_agent.ontop_mappings import build_inverse_index

    nquads = build_thin_vkg_nquads(
        table="posts",
        columns=COLUMNS,
        class_iri="http://x/Post",
        graph_iri="http://g",
        key_columns=["id"],
    )
    # Quads need a Dataset; a plain Graph cannot hold a named-graph component.
    dataset = Dataset()
    dataset.parse(data=nquads, format="nquads")
    index = build_inverse_index(
        dataset.graph(URIRef("http://g")),
        vocab="https://semantic-layer.aws/virtual-kg/",
    )
    assert index.class_by_table["posts"] == "http://x/Post"
    # The key literal ("posts.id") must NOT be indexed as a column mapping — it
    # would shadow the real posts.id property predicate.
    assert index.pred_by_column["posts.id"] == "http://x/Post/id"
