"""Tests for named-graph resolution and deletion in lambda/neptune-tools.

A rebuild of a layer does NOT overwrite its Neptune graph: the graph URI embeds the
layer's name, callers timestamp that name per build, and persistence is a SPARQL
``INSERT DATA``. So graphs accumulate — on 2026-08-09 every rebuilt BIRD layer had
three, and resolution (``LIMIT 1``, no ``ORDER BY``) was serving the OLDEST. Every
query then answered from a two-day-stale ontology while the build logs, DynamoDB
status and unit tests all reported success.

These tests pin the two properties that make that failure impossible to repeat:
resolution is deterministic and prefers the newest build, and deleting a layer
removes every graph it ever wrote rather than one arbitrary graph.
"""

import importlib.util
import json
import os
from typing import Any, Dict, List
from unittest.mock import patch

# Load lambda/neptune-tools/index.py under a UNIQUE module name — several Lambda
# handlers under test are named `index.py` and a plain `import index` would hand
# this file whichever one loaded first in a full-suite run.
_NEPTUNE_TOOLS_INDEX_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "lambda", "neptune-tools", "index.py")
)
_spec = importlib.util.spec_from_file_location(
    "neptune_tools_index_graphs", _NEPTUNE_TOOLS_INDEX_PATH
)
index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(index)

ONTOLOGY_ID = "vkg-bird_card_games"
# The three real graph URIs observed in Neptune for this layer, deliberately NOT in
# chronological order so a test that passes only because of input ordering fails.
OLDEST = f"http://bird-bird_card_games-ontology-20260804-122204/ontology/{ONTOLOGY_ID}"
MIDDLE = f"http://bird-bird_card_games-ontology-20260804-230127/ontology/{ONTOLOGY_ID}"
NEWEST = f"http://bird-bird_card_games-ontology-20260806-084812/ontology/{ONTOLOGY_ID}"


def _graph_bindings(uris: List[str]) -> Dict[str, Any]:
    """Shape a list of graph URIs like a Neptune SELECT ?g result."""
    return {"results": {"bindings": [{"g": {"value": u}} for u in uris]}}


def _count_bindings(n: int) -> Dict[str, Any]:
    """Shape a triple count like a Neptune SELECT (COUNT(*) AS ?tripleCount) result."""
    return {"results": {"bindings": [{"tripleCount": {"value": str(n)}}]}}


class _FakeResponse:
    """Minimal requests.Response stand-in for the SPARQL Update write path."""

    def __init__(self, status_code: int = 200, text: str = "") -> None:
        """Store the HTTP status and body the fake update call should report.

        Args:
            status_code: HTTP status to report.
            text: Response body.
        """
        self.status_code = status_code
        self.text = text


def test_resolution_orders_graphs_so_neptune_cannot_return_an_arbitrary_one() -> None:
    """The SPARQL must carry an ORDER BY; without it Neptune may return any graph."""
    queries: List[str] = []

    def _fake_query(sparql_query: str) -> Dict[str, Any]:
        queries.append(sparql_query)
        return _graph_bindings([NEWEST])

    with patch.object(index, "execute_sparql_query", _fake_query):
        index._resolve_graph_uris(ONTOLOGY_ID)

    assert "ORDER BY DESC(?g)" in queries[0]
    # A LIMIT would defeat the point: delete_graph needs to see every graph.
    assert "LIMIT" not in queries[0].upper()


def test_the_newest_build_is_served_not_a_stale_one() -> None:
    """The exact live failure: three graphs, and the 08-04 one was being served."""
    # Neptune honours the ORDER BY, so it hands back newest-first.
    ordered = [NEWEST, MIDDLE, OLDEST]
    with patch.object(index, "execute_sparql_query", lambda q: _graph_bindings(ordered)):
        assert index._resolve_graph_uri(ONTOLOGY_ID) == NEWEST


def test_a_missing_graph_still_raises_rather_than_returning_a_wrong_default() -> None:
    """No graph means the layer is not servable; a silent default would hide that."""
    with patch.object(index, "execute_sparql_query", lambda q: _graph_bindings([])):
        try:
            index._resolve_graph_uri(ONTOLOGY_ID)
        except ValueError as exc:
            assert ONTOLOGY_ID in str(exc)
        else:
            raise AssertionError("expected ValueError for an ontology with no graph")


def test_a_query_failure_does_not_masquerade_as_an_empty_result() -> None:
    """A Neptune error must still reach the caller as 'no graph found', not a URI."""
    def _boom(sparql_query: str) -> Dict[str, Any]:
        raise RuntimeError("neptune unreachable")

    with patch.object(index, "execute_sparql_query", _boom):
        assert index._resolve_graph_uris(ONTOLOGY_ID) == []


def test_delete_drops_every_graph_for_the_layer_not_just_one() -> None:
    """Dropping one graph leaves a superseded graph that resolution can still serve."""
    ordered = [NEWEST, MIDDLE, OLDEST]
    updates: List[str] = []

    def _fake_query(sparql_query: str) -> Dict[str, Any]:
        if "COUNT" in sparql_query:
            return _count_bindings(789)
        return _graph_bindings(ordered)

    def _fake_update(update_query: str) -> _FakeResponse:
        updates.append(update_query)
        return _FakeResponse(200)

    with patch.object(index, "execute_sparql_query", _fake_query):
        with patch.object(index, "_sparql_update", _fake_update):
            result = json.loads(index.tool_delete_graph(ONTOLOGY_ID))

    assert result["success"] is True
    assert sorted(result["graphs_deleted"]) == sorted(ordered)
    assert result["triples_deleted"] == 789 * 3
    for uri in ordered:
        assert f"DROP SILENT GRAPH <{uri}>" in updates


def test_a_partially_failed_delete_is_reported_as_a_failure() -> None:
    """A surviving graph can still be served, so partial success is not success."""
    ordered = [NEWEST, OLDEST]

    def _fake_query(sparql_query: str) -> Dict[str, Any]:
        if "COUNT" in sparql_query:
            return _count_bindings(10)
        return _graph_bindings(ordered)

    def _fake_update(update_query: str) -> _FakeResponse:
        if OLDEST in update_query:
            return _FakeResponse(500, "internal error")
        return _FakeResponse(200)

    with patch.object(index, "execute_sparql_query", _fake_query):
        with patch.object(index, "_sparql_update", _fake_update):
            result = json.loads(index.tool_delete_graph(ONTOLOGY_ID))

    assert result["success"] is False
    assert OLDEST in result["message"]
    # The graph that WAS dropped is still reported, so a retry is informed.
    assert result["graphs_deleted"] == [NEWEST]


def test_deleting_a_layer_with_no_graphs_succeeds_without_a_drop() -> None:
    """Idempotent delete: a pre-build clear runs before any graph exists."""
    updates: List[str] = []

    with patch.object(index, "execute_sparql_query", lambda q: _graph_bindings([])):
        with patch.object(index, "_sparql_update", lambda q: updates.append(q)):
            result = json.loads(index.tool_delete_graph(ONTOLOGY_ID))

    assert result["success"] is True
    assert result["graphs_deleted"] == 0
    assert updates == []
