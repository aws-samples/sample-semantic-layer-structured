"""Tests for clearing a layer's prior Neptune graphs before a rebuild.

Persistence into Neptune is a SPARQL ``INSERT DATA`` into a graph whose URI embeds
the layer's (timestamped) name, so a rebuild adds a graph instead of replacing one.
Only the revision path ever deleted the old graph; the ordinary full-rebuild path
did not, which is how three graphs per BIRD layer accumulated. These tests pin that
the clear happens on a full rebuild, is skipped for a delta build, and never takes
a build down with it.
"""

import json
import os
import sys
from typing import Any, Dict, List
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents"))


class _FakeMcp:
    """Minimal MCPClient stand-in recording the gateway tool calls it receives."""

    def __init__(self, calls: List[Dict[str, Any]], body: Dict[str, Any]) -> None:
        """Store the recording list and the canned JSON body to return.

        Args:
            calls: List each call is appended to as {"name", "arguments"}.
            body: The JSON body the fake delete_graph should report.
        """
        self._calls = calls
        self._body = body

    def __enter__(self) -> "_FakeMcp":
        """Enter the context manager (the real client opens a session here)."""
        return self

    def __exit__(self, *exc_info: object) -> bool:
        """Exit without suppressing exceptions."""
        return False

    def call_tool_sync(
        self, tool_use_id: str, name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Record the call and return the canned body, wrapped as the Gateway wraps it."""
        self._calls.append({"name": name, "arguments": arguments})
        return {"content": [{"text": json.dumps(self._body)}]}


def test_a_rebuild_clears_every_prior_graph_through_the_gateway() -> None:
    """The clear must go through delete_graph, which drops ALL matching graphs."""
    from ontology_agent.main import _clear_prior_neptune_graphs

    calls: List[Dict[str, Any]] = []
    body = {"success": True, "graphs_deleted": ["g1", "g2", "g3"], "triples_deleted": 2367}
    with patch.dict(os.environ, {"NEPTUNE_GATEWAY_URL": "https://gw"}):
        with patch("ontology_agent.main.MCPClient", return_value=_FakeMcp(calls, body)):
            detail = _clear_prior_neptune_graphs(ontology_id="vkg-bird_card_games")

    assert len(calls) == 1
    assert calls[0]["name"] == "delete-graph___delete_graph"
    assert calls[0]["arguments"] == {"ontology_id": "vkg-bird_card_games"}
    # The detail is logged, so the graph count must survive into it — three graphs
    # silently dropped is exactly the fact an operator needs to see.
    assert "2367" in detail


def test_an_unreachable_gateway_does_not_abort_the_rebuild() -> None:
    """Failing to clear is worse than not rebuilding, but not worth losing the build.

    Resolution now prefers the newest graph, so an uncleared layer still serves the
    fresh ontology — the stale graphs are clutter, not corruption.
    """
    from ontology_agent.main import _clear_prior_neptune_graphs

    with patch.dict(os.environ, {"NEPTUNE_GATEWAY_URL": "https://gw"}):
        with patch(
            "ontology_agent.main.MCPClient", side_effect=RuntimeError("connection refused")
        ):
            detail = _clear_prior_neptune_graphs(ontology_id="abc")

    assert "could not clear prior graphs" in detail
    assert "connection refused" in detail


def test_no_gateway_configured_is_a_no_op() -> None:
    """Local and unit-test environments have no gateway to call."""
    from ontology_agent.main import _clear_prior_neptune_graphs

    with patch.dict(os.environ, {"NEPTUNE_GATEWAY_URL": ""}):
        detail = _clear_prior_neptune_graphs(ontology_id="abc")

    assert "nothing cleared" in detail


def test_the_build_path_skips_the_clear_for_a_delta_rebuild() -> None:
    """A delta build ADDS to the existing graph; clearing it would erase the layer.

    Asserted on the source rather than by running the entrypoint, because the guard
    lives inside a background thread that a unit test cannot reach without standing
    up DynamoDB, Athena and the whole agent loop.
    """
    import ontology_agent.main as main_mod

    source = open(main_mod.__file__, encoding="utf-8").read()
    call = "_clear_prior_neptune_graphs(ontology_id=ontology_id)"
    # The build-path call is the LAST of the two call sites (the other is the
    # revision path, which always clears and needs no guard).
    call_at = source.rindex(call)
    # The clear must sit INSIDE the delta guard, not merely somewhere in the file.
    # rindex bounded by the call site finds the nearest preceding guard.
    guard_at = source.rindex("if not delta_targets:", 0, call_at)
    assert call_at - guard_at < 500, "the clear is not inside the delta_targets guard"
