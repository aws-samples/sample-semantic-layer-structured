"""Tests for stamping verified ``vkg:hasPrimaryKey`` triples onto an ontology.

The stamp is the bridge between the probe (which measures uniqueness) and the
Java translator (which reads ``mappings[classIRI].primaryKey`` to build the OBDA
subject template). These tests pin the literal SHAPE the translator parses, the
graph the triple lands in, and the refusal to stamp anything unverified.
"""

from typing import Dict, List, Optional

import pytest

from agents.ontology_agent.key_stamp import (
    HAS_PRIMARY_KEY_PRED,
    emit_has_primary_key,
    parse_class_mappings,
    stamp_primary_keys,
)

VOCAB = "https://semantic-layer.aws/virtual-kg/"
GRAPH = "http://onto/abc"

# One class over three columns, in the shape the ontology agent emits.
NQUADS = f"""<{GRAPH}/Post> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#Class> <{GRAPH}> .
<{GRAPH}/Post> <{VOCAB}mapsToTable> "so.posts" <{GRAPH}> .
<{GRAPH}/Post/aboutme> <{VOCAB}mapsToColumn> "posts.aboutme" <{GRAPH}> .
<{GRAPH}/Post/id> <{VOCAB}mapsToColumn> "posts.id" <{GRAPH}> .
<{GRAPH}/Post/userId> <{VOCAB}mapsToColumn> "posts.user_id" <{GRAPH}> .
"""


def _probe_factory_from(rows_by_call: List[List[Dict[str, object]]], captured: List[str]):
    """Build a ProbeFactory whose probe returns successive canned results.

    Args:
        rows_by_call: One result list per probe call, in order.
        captured: List each executed SQL string is appended to.

    Returns:
        A ProbeFactory callable.
    """
    state = {"i": 0}

    def _factory(database: str, catalog: Optional[str]):
        """Return the RunProbe for *database*."""

        def _run(sql: str) -> List[Dict[str, object]]:
            """Record *sql* and return the next canned result."""
            captured.append(sql)
            result = rows_by_call[min(state["i"], len(rows_by_call) - 1)]
            state["i"] += 1
            return result

        return _run

    return _factory


def test_parses_table_columns_and_graph_per_class() -> None:
    """The probed column set must match what the OBDA mapping will declare."""
    mappings = parse_class_mappings(NQUADS)
    assert len(mappings) == 1
    mapping = mappings[0]
    assert mapping.class_iri == f"{GRAPH}/Post"
    assert mapping.database == "so"
    assert mapping.table == "posts"
    # Bare column names, de-duplicated, in encounter (catalog) order.
    assert mapping.columns == ["aboutme", "id", "user_id"]
    # The graph matters: a triple in another graph is invisible to the query
    # side's graph-scoped CONSTRUCT.
    assert mapping.graph == GRAPH


def test_only_properties_nested_under_the_class_are_attributed_to_it() -> None:
    """Nesting is the same rule the Java consumer applies to pick a class's columns."""
    nquads = (
        f'<{GRAPH}/A> <{VOCAB}mapsToTable> "db.a" <{GRAPH}> .\n'
        f'<{GRAPH}/A/x> <{VOCAB}mapsToColumn> "a.x" <{GRAPH}> .\n'
        f'<{GRAPH}/B/y> <{VOCAB}mapsToColumn> "b.y" <{GRAPH}> .\n'
    )
    mapping = parse_class_mappings(nquads)[0]
    assert mapping.columns == ["x"]


def test_duplicate_column_mappings_are_probed_once() -> None:
    """Two properties on one column must not duplicate an aggregate alias."""
    nquads = (
        f'<{GRAPH}/A> <{VOCAB}mapsToTable> "db.a" <{GRAPH}> .\n'
        f'<{GRAPH}/A/x> <{VOCAB}mapsToColumn> "a.pid" <{GRAPH}> .\n'
        f'<{GRAPH}/A/xAlias> <{VOCAB}mapsToColumn> "a.pid" <{GRAPH}> .\n'
    )
    assert parse_class_mappings(nquads)[0].columns == ["pid"]


def test_emits_the_dotted_literal_shape_the_translator_parses() -> None:
    """The translator accepts dotted, comma-separated ``table.column`` values."""
    line = emit_has_primary_key(
        class_iri="http://x/Post", table="posts", key_columns=["user_id"], graph=GRAPH
    )
    assert line == (
        f'<http://x/Post> {HAS_PRIMARY_KEY_PRED} "posts.user_id" <{GRAPH}> .'
    )


def test_composite_key_literal_is_comma_separated_in_key_order() -> None:
    """Order determines the subject template's column order and must be stable."""
    line = emit_has_primary_key(
        class_iri="http://x/E",
        table="enrolment",
        key_columns=["student_id", "course_id"],
        graph=GRAPH,
    )
    assert '"enrolment.student_id, enrolment.course_id"' in line


def test_refuses_to_emit_an_empty_key() -> None:
    """An empty literal would look like a declaration while declaring nothing."""
    with pytest.raises(ValueError, match="empty hasPrimaryKey"):
        emit_has_primary_key(
            class_iri="http://x/C", table="t", key_columns=[], graph=GRAPH
        )


def test_stamps_a_verified_key_into_the_same_named_graph() -> None:
    """The happy path: one unique column found and declared on the class."""
    captured: List[str] = []
    # aboutme not unique; id unique; user_id not unique. All null-free.
    rows = [{"total": "5", "d0": "2", "n0": "5", "d1": "5", "n1": "5", "d2": "3", "n2": "5"}]
    stamped, keys = stamp_primary_keys(
        nquads=NQUADS, probe_factory=_probe_factory_from([rows], captured)
    )
    assert keys == {f"{GRAPH}/Post": ["id"]}
    assert f'<{GRAPH}/Post> {HAS_PRIMARY_KEY_PRED} "posts.id" <{GRAPH}> .' in stamped
    # The original content survives unchanged.
    assert f'<{GRAPH}/Post> <{VOCAB}mapsToTable> "so.posts" <{GRAPH}> .' in stamped


def test_unverifiable_key_leaves_the_class_unstamped() -> None:
    """No triple at all, so the translator can tell a guess from a fact."""
    captured: List[str] = []
    # Nothing unique.
    rows = [{"total": "5", "d0": "2", "n0": "5", "d1": "2", "n1": "5", "d2": "2", "n2": "5"}]
    stamped, keys = stamp_primary_keys(
        nquads=NQUADS, probe_factory=_probe_factory_from([rows, []], captured)
    )
    assert keys == {}
    assert HAS_PRIMARY_KEY_PRED not in stamped


def test_a_failing_probe_does_not_abort_the_whole_build() -> None:
    """One table's timeout must not fail a 75-table layer; it just goes unstamped."""

    def _factory(database: str, catalog: Optional[str]):
        """Return a probe that always raises."""

        def _run(sql: str) -> List[Dict[str, object]]:
            """Simulate an Athena failure."""
            raise RuntimeError("Athena query FAILED: TABLE_NOT_FOUND")

        return _run

    stamped, keys = stamp_primary_keys(nquads=NQUADS, probe_factory=_factory)
    assert keys == {}
    assert stamped == NQUADS


def test_rerunning_does_not_reprobe_or_duplicate_an_existing_key() -> None:
    """Assembly and delta builds both re-run; the stamp must be idempotent."""
    captured: List[str] = []
    already = NQUADS + (
        f'<{GRAPH}/Post> {HAS_PRIMARY_KEY_PRED} "posts.id" <{GRAPH}> .\n'
    )
    rows = [{"total": "5", "d0": "5", "n0": "5"}]
    stamped, keys = stamp_primary_keys(
        nquads=already, probe_factory=_probe_factory_from([rows], captured)
    )
    assert keys == {}
    assert captured == [], "an already-keyed class must not be re-probed"
    assert stamped.count(HAS_PRIMARY_KEY_PRED) == 1


def test_catalog_resolver_reaches_the_probe_for_a_federated_database() -> None:
    """A federated (S3 Tables) database probed via the default catalog cannot resolve."""
    seen: List[Optional[str]] = []

    def _factory(database: str, catalog: Optional[str]):
        """Record the catalog handed to the probe."""
        seen.append(catalog)

        def _run(sql: str) -> List[Dict[str, object]]:
            """Return no rows; only the catalog matters here."""
            return []

        return _run

    stamp_primary_keys(
        nquads=NQUADS,
        probe_factory=_factory,
        catalog_for={"so": "s3tablescatalog/bucket"}.get,
    )
    assert seen == ["s3tablescatalog/bucket"]


def test_class_with_no_database_segment_is_left_unstamped() -> None:
    """A bare table name cannot form a qualified probe query."""
    nquads = (
        f'<{GRAPH}/A> <{VOCAB}mapsToTable> "a" <{GRAPH}> .\n'
        f'<{GRAPH}/A/x> <{VOCAB}mapsToColumn> "a.x" <{GRAPH}> .\n'
    )
    stamped, keys = stamp_primary_keys(
        nquads=nquads, probe_factory=_probe_factory_from([[]], [])
    )
    assert keys == {}
    assert stamped == nquads


def test_class_with_no_mapped_columns_is_left_unstamped() -> None:
    """The translator skips such a class, so a key would identify nothing."""
    nquads = f'<{GRAPH}/A> <{VOCAB}mapsToTable> "db.a" <{GRAPH}> .\n'
    stamped, keys = stamp_primary_keys(
        nquads=nquads, probe_factory=_probe_factory_from([[]], [])
    )
    assert keys == {}
    assert stamped == nquads
