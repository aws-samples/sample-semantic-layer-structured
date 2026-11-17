"""Tests for the ontology agent's assembly-time key-stamping wiring.

``_stamp_verified_primary_keys`` is the seam between the consolidated N-Quads and
the Athena probe. These tests pin the two things the surrounding code depends on:
the catalog is resolved per database (a federated S3 Tables database probed via
the default catalog cannot resolve its table), and a probe failure degrades to
"no key" instead of aborting the whole layer build.
"""

import os
import sys
from typing import Any, Dict, List
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents"))

VOCAB = "https://semantic-layer.aws/virtual-kg/"
GRAPH = "http://onto/abc"

NQUADS = (
    f'<{GRAPH}/Party> <{VOCAB}mapsToTable> "insurance_db.party" <{GRAPH}> .\n'
    f'<{GRAPH}/Party/pid> <{VOCAB}mapsToColumn> "party.party_id" <{GRAPH}> .\n'
    f'<{GRAPH}/Party/name> <{VOCAB}mapsToColumn> "party.name" <{GRAPH}> .\n'
)

TABLES: List[Dict[str, Any]] = [
    {
        "database": "insurance_db",
        "table": "party",
        "catalogId": "s3tablescatalog/mybucket",
    }
]


def test_stamps_a_verified_key_and_routes_the_probe_via_the_table_catalog() -> None:
    """A federated database must be probed through ITS catalog, not the default."""
    from ontology_agent.main import _stamp_verified_primary_keys

    seen: List[Dict[str, str]] = []

    def fake_run_athena_query(query: str, database: str, catalog: str) -> Dict[str, Any]:
        """Record the routing and report party_id unique, name not."""
        seen.append({"database": database, "catalog": catalog})
        return {
            "success": True,
            "columns": [],
            "rows": [{"total": "4", "d0": "4", "n0": "4", "d1": "2", "n1": "4"}],
            "error": None,
        }

    with patch("ontology_agent.main._run_athena_query", side_effect=fake_run_athena_query):
        stamped, delta = _stamp_verified_primary_keys(
            ontology_id="abc", nquads=NQUADS, tables=TABLES
        )

    assert seen == [{"database": "insurance_db", "catalog": "s3tablescatalog/mybucket"}]
    # The delta must carry the new triple: it is what gets pushed to Neptune, and
    # the query path reads the ontology from Neptune rather than from the S3 file.
    assert delta.strip() == (
        f'<{GRAPH}/Party> <{VOCAB}hasPrimaryKey> "party.party_id" <{GRAPH}> .'
    )
    assert (
        f'<{GRAPH}/Party> <{VOCAB}hasPrimaryKey> "party.party_id" <{GRAPH}> .' in stamped
    )


def test_a_failed_probe_query_leaves_the_ontology_unchanged() -> None:
    """An un-probeable table must not block a build, and must not invent a key."""
    from ontology_agent.main import _stamp_verified_primary_keys

    def failing_query(query: str, database: str, catalog: str) -> Dict[str, Any]:
        """Simulate an Athena failure the way _run_athena_query reports it."""
        return {
            "success": False,
            "columns": [],
            "rows": [],
            "error": "Query FAILED: TABLE_NOT_FOUND",
        }

    with patch("ontology_agent.main._run_athena_query", side_effect=failing_query):
        stamped, delta = _stamp_verified_primary_keys(
            ontology_id="abc", nquads=NQUADS, tables=TABLES
        )

    assert stamped == NQUADS
    assert "hasPrimaryKey" not in stamped
    # An empty delta is what stops the caller pushing anything to Neptune.
    assert delta == ""


def test_a_raising_probe_is_contained_so_assembly_still_saves() -> None:
    """Assembly must survive an unexpected exception from the probe path."""
    from ontology_agent.main import _stamp_verified_primary_keys

    with patch(
        "ontology_agent.main._run_athena_query",
        side_effect=RuntimeError("boto3 exploded"),
    ):
        stamped, delta = _stamp_verified_primary_keys(
            ontology_id="abc", nquads=NQUADS, tables=TABLES
        )

    assert stamped == NQUADS
    assert delta == ""


def test_defaults_to_the_glue_catalog_when_the_table_list_names_none() -> None:
    """Athena addresses the default catalog as AWSDataCatalog, not an empty string."""
    from ontology_agent.main import _stamp_verified_primary_keys

    seen: List[str] = []

    def fake_run(query: str, database: str, catalog: str) -> Dict[str, Any]:
        """Record the catalog and report nothing unique."""
        seen.append(catalog)
        return {"success": True, "columns": [], "rows": [], "error": None}

    with patch("ontology_agent.main._run_athena_query", side_effect=fake_run):
        _stamp_verified_primary_keys(
            ontology_id="abc",
            nquads=NQUADS,
            tables=[{"database": "insurance_db", "table": "party"}],
        )

    assert seen == ["AWSDataCatalog"]


def test_the_key_delta_is_exactly_what_neptune_needs_and_nothing_more() -> None:
    """The delta must be pushable to Neptune on its own.

    The query path reads the ontology from NEPTUNE; the S3 file is
    documentation/backup. A key stamped only into S3 would leave the translator
    guessing row identity, so the whole fix would be silently inert. Pushing the
    FULL ontology instead would re-insert every existing quad, so the delta must
    contain only the new lines — each a complete, self-contained N-Quad that
    ``persist_to_neptune``'s line regex can parse (it silently drops any line it
    cannot match, which would discard the key without an error).
    """
    from ontology_agent.main import _stamp_verified_primary_keys

    def fake_run(query: str, database: str, catalog: str) -> Dict[str, Any]:
        """Report party_id unique, name not."""
        return {
            "success": True,
            "columns": [],
            "rows": [{"total": "4", "d0": "4", "n0": "4", "d1": "2", "n1": "4"}],
            "error": None,
        }

    with patch("ontology_agent.main._run_athena_query", side_effect=fake_run):
        stamped, delta = _stamp_verified_primary_keys(
            ontology_id="abc", nquads=NQUADS, tables=TABLES
        )

    lines = [line for line in delta.splitlines() if line.strip()]
    assert len(lines) == 1, "the delta must hold ONLY the newly stamped triples"
    # No pre-existing quad may be re-sent, and every line must be a full quad
    # (subject, predicate, literal object, graph) terminated with ' .'.
    assert "mapsToTable" not in delta
    assert "mapsToColumn" not in delta
    for line in lines:
        assert line.startswith("<") and line.endswith(" ."), line
        assert line.count("<") >= 3, f"missing the named-graph component: {line}"
    # Appending the delta to the input must reproduce the S3 text exactly, so the
    # two destinations cannot drift apart.
    assert NQUADS + delta == stamped
