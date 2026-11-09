"""Tests for the build-time queryability gate (P0.3).

A layer can reach ``status: completed`` with a well-formed ontology that Ontop
cannot build a reformulator for, which makes EVERY query against it fail. That is
invisible until query time — ``bird_card_games`` shipped that way and silently
scored 52 benchmark rows zero. These tests pin the two properties that make the
gate trustworthy: a genuine translation failure is caught, and an inconclusive
probe never fails a build it has no evidence against.
"""

import json
import os
import sys
from typing import Any, Dict, List
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents"))

CLASS_IRI = "http://onto/abc/Party"

ONTOLOGY: Dict[str, Any] = {
    "classes": {CLASS_IRI: {"label": "Party"}, "http://onto/abc/Unmapped": {}},
    "properties": {f"{CLASS_IRI}/pid": {"type": "owl#DatatypeProperty"}},
    "mappings": {
        CLASS_IRI: {"table": "insurance_db.party"},
        f"{CLASS_IRI}/pid": {"column": "party.party_id"},
    },
    "databases": [{"name": "insurance_db", "catalog": "AwsDataCatalog"}],
}


class _FakeMcp:
    """Minimal MCPClient stand-in recording tool calls and returning canned bodies."""

    def __init__(self, responses: Dict[str, Dict[str, Any]], calls: List[str]) -> None:
        """Store the per-tool canned responses and the call-recording list.

        Args:
            responses: Tool name (or its bare suffix) -> the JSON body to return.
            calls: List each invoked tool name is appended to.
        """
        self._responses = responses
        self._calls = calls
        self.arguments: List[Dict[str, Any]] = []

    def __enter__(self) -> "_FakeMcp":
        """Enter the context manager (the real client opens a session here)."""
        return self

    def __exit__(self, *exc_info: object) -> bool:
        """Exit without suppressing exceptions."""
        return False

    def call_tool_sync(
        self, tool_use_id: str, name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Return the canned body for *name*, wrapped the way the Gateway wraps it."""
        self._calls.append(name)
        self.arguments.append(arguments)
        for key, body in self._responses.items():
            if key in name:
                return {"content": [{"text": json.dumps(body)}]}
        raise AssertionError(f"unexpected gateway tool: {name}")


def _patched(responses: Dict[str, Dict[str, Any]], calls: List[str]):
    """Patch MCPClient so the probe talks to a _FakeMcp instead of the gateway.

    Args:
        responses: Tool-name substring -> JSON body.
        calls: List each invoked tool name is appended to.

    Returns:
        The unittest.mock patcher context manager.
    """
    fake = _FakeMcp(responses, calls)
    return patch("ontology_agent.main.MCPClient", return_value=fake)


def test_a_layer_whose_translation_fails_is_not_queryable() -> None:
    """The card_games failure: reformulator build fails, so the layer is dead."""
    from ontology_agent.main import _probe_layer_is_queryable

    calls: List[str] = []
    responses = {
        "get_ontology_from_neptune": ONTOLOGY,
        "translate_sparql_to_sql": {
            "error": "Failed to build Ontop reformulator: Has no parent provider"
        },
    }
    with patch.dict(os.environ, {"NEPTUNE_GATEWAY_URL": "https://gw"}):
        with _patched(responses, calls):
            queryable, detail = _probe_layer_is_queryable(ontology_id="abc")

    assert queryable is False
    assert "Has no parent provider" in detail


def test_empty_sql_is_also_a_failure_not_a_pass() -> None:
    """A query folded to nothing returns zero rows against a populated table.

    That scores 0 with no error anywhere, which is the failure mode this gate
    exists to convert into a loud build failure.
    """
    from ontology_agent.main import _probe_layer_is_queryable

    calls: List[str] = []
    responses = {
        "get_ontology_from_neptune": ONTOLOGY,
        "translate_sparql_to_sql": {"sql": "", "database": "insurance_db"},
    }
    with patch.dict(os.environ, {"NEPTUNE_GATEWAY_URL": "https://gw"}):
        with _patched(responses, calls):
            queryable, detail = _probe_layer_is_queryable(ontology_id="abc")

    assert queryable is False
    assert "no SQL" in detail


def test_a_healthy_layer_passes_and_probes_a_mapped_class() -> None:
    """The probed class must be MAPPED, or the translation never reaches a table."""
    from ontology_agent.main import _probe_layer_is_queryable

    calls: List[str] = []
    fake = _FakeMcp(
        {
            "get_ontology_from_neptune": ONTOLOGY,
            "translate_sparql_to_sql": {
                "sql": "SELECT R1.PARTY_ID FROM insurance_db.party R1",
                "database": "insurance_db",
                "catalog": "AwsDataCatalog",
            },
        },
        calls,
    )
    with patch.dict(os.environ, {"NEPTUNE_GATEWAY_URL": "https://gw"}):
        with patch("ontology_agent.main.MCPClient", return_value=fake):
            queryable, detail = _probe_layer_is_queryable(ontology_id="abc")

    assert queryable is True
    assert CLASS_IRI in detail
    # The unmapped class must not be chosen: it has no table, so translating it
    # would prove nothing about whether the layer can read data.
    sparql = fake.arguments[1]["sparql"]
    assert CLASS_IRI in sparql
    assert "Unmapped" not in sparql
    # It must go through the gateway (the deployed path), not a local translation.
    assert any("get_ontology_from_neptune" in call for call in calls)
    assert any("translate_sparql_to_sql" in call for call in calls)


def test_an_unreachable_gateway_does_not_fail_the_build() -> None:
    """An inconclusive probe must not fail a build it has no evidence against."""
    from ontology_agent.main import _probe_layer_is_queryable

    with patch.dict(os.environ, {"NEPTUNE_GATEWAY_URL": "https://gw"}):
        with patch(
            "ontology_agent.main.MCPClient", side_effect=RuntimeError("connection refused")
        ):
            queryable, detail = _probe_layer_is_queryable(ontology_id="abc")

    assert queryable is True
    assert "inconclusive" in detail


def test_no_gateway_configured_skips_the_probe() -> None:
    """Local/test environments have no gateway; the gate must stay out of the way."""
    from ontology_agent.main import _probe_layer_is_queryable

    with patch.dict(os.environ, {"NEPTUNE_GATEWAY_URL": ""}):
        queryable, detail = _probe_layer_is_queryable(ontology_id="abc")

    assert queryable is True
    assert "skipped" in detail


def test_a_class_without_a_table_mapping_is_not_probed() -> None:
    """Nothing mapped means nothing to prove; assembly already gates on empties."""
    from ontology_agent.main import _probe_layer_is_queryable

    calls: List[str] = []
    unmapped = {"classes": {CLASS_IRI: {}}, "mappings": {}, "properties": {}}
    with patch.dict(os.environ, {"NEPTUNE_GATEWAY_URL": "https://gw"}):
        with _patched({"get_ontology_from_neptune": unmapped}, calls):
            queryable, detail = _probe_layer_is_queryable(ontology_id="abc")

    assert queryable is True
    assert "no class carries a table mapping" in detail
    # No translate call may have been attempted.
    assert not any("translate" in call for call in calls)


def test_a_neptune_fetch_error_is_a_failure() -> None:
    """If the ontology cannot be read back, the layer is not servable."""
    from ontology_agent.main import _probe_layer_is_queryable

    calls: List[str] = []
    with patch.dict(os.environ, {"NEPTUNE_GATEWAY_URL": "https://gw"}):
        with _patched({"get_ontology_from_neptune": {"error": "graph not found"}}, calls):
            queryable, detail = _probe_layer_is_queryable(ontology_id="abc")

    assert queryable is False
    assert "graph not found" in detail
