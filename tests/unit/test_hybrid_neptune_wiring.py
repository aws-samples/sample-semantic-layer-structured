"""Unit tests for the Hybrid agents' Neptune gateway wiring.

These cover the four seams that were stubbed until 2026-08-05 — the thin-VKG
Neptune load, the query-time CONSTRUCT fetch, the Glue column probe, and the
``vkgBuilt`` stamp — plus the two silent-mismatch traps found while wiring them:

  * the mapping-predicate VOCABULARY must be identical on both sides, else the
    inverse index is empty and every hybrid query abstains as ungrounded;
  * the gateway's N-Quads parser SILENTLY DROPS lines it cannot match while still
    reporting success, so the client validates the grammar before sending.

All AWS/MCP access is exercised through fakes — no boto3, no gateway.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from agents.semantic_layer_creation_agent import main as creation_main
from agents.semantic_layer_creation_agent.neptune_load import (
    NeptuneLoadClient,
    _result_text,
    validate_nquads,
)
from agents.semantic_layer_creation_agent.vkg_builder import (
    build_thin_vkg_nquads,
    glue_type_to_xsd,
)
from agents.semantic_layer_query_agent.neptune_gateway import (
    NeptuneGatewayClient,
    build_structural_construct,
    escape_sparql_literal,
    normalize_candidate,
    vkg_graph_iri,
)


# --------------------------------------------------------------------------- #
# Fakes.                                                                      #
# --------------------------------------------------------------------------- #
class _FakeToolSpec:
    """Minimal stand-in for a Strands tool wrapper (exposes ``tool_name``)."""

    def __init__(self, *, tool_name: str) -> None:
        """Store the namespaced tool name.

        Parameters:
            tool_name: The namespaced ``<target>___<tool>`` id.
        Returns:
            None.
        """
        self.tool_name = tool_name


class _FakeMcp:
    """Minimal Strands ``MCPClient`` stand-in recording the calls it receives."""

    def __init__(self, *, tool_names: List[str], result: Any) -> None:
        """Configure the exposed tools and the canned call result.

        Parameters:
            tool_names: Namespaced tool ids ``list_tools_sync`` should report.
            result: The object ``call_tool_sync`` returns.
        Returns:
            None.
        """
        self._tool_names = tool_names
        self._result = result
        self.calls: List[Dict[str, Any]] = []

    def list_tools_sync(self) -> List[_FakeToolSpec]:
        """Return the configured tool specs."""
        return [_FakeToolSpec(tool_name=n) for n in self._tool_names]

    def call_tool_sync(self, *, tool_use_id: str, name: str, arguments: Dict[str, Any]):
        """Record the call and return the canned result."""
        self.calls.append({"id": tool_use_id, "name": name, "arguments": arguments})
        return self._result


def _text_result(payload: str, *, status: str = "success") -> Dict[str, Any]:
    """Build an MCP-shaped result carrying ``payload`` as a single text block.

    Parameters:
        payload: The text body the gateway would return.
        status: The MCP result status.
    Returns:
        dict: The result structure ``_result_text`` consumes.
    """
    return {"status": status, "content": [{"text": payload}]}


# --------------------------------------------------------------------------- #
# Glue -> XSD range mapping.                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "glue_type,expected_local",
    [
        ("int", "integer"),
        ("integer", "integer"),
        ("smallint", "integer"),
        ("bigint", "long"),
        ("double", "double"),
        ("float", "double"),
        ("boolean", "boolean"),
        ("date", "date"),
        ("timestamp", "dateTime"),
        ("string", "string"),
        ("STRING", "string"),          # case-insensitive
        ("decimal(10,2)", "decimal"),  # parameterized head is what matters
        ("varchar(255)", "string"),
        ("array<string>", "string"),   # complex types degrade to string
        ("", "string"),
    ],
)
def test_glue_type_to_xsd(glue_type: str, expected_local: str):
    assert glue_type_to_xsd(glue_type) == (
        f"<http://www.w3.org/2001/XMLSchema#{expected_local}>"
    )


def test_thin_vkg_emits_typed_ranges():
    # Ontop needs real datatypes to push numeric predicates down into SQL; an
    # all-xsd:string graph silently loses that.
    nquads = build_thin_vkg_nquads(
        table="orders",
        columns=[{"name": "qty", "type": "bigint"}, {"name": "sku", "type": "string"}],
        class_iri="https://semantic-layer.aws/virtual-kg/orders",
        graph_iri="https://semantic-layer.aws/vkg/hybrid-acme",
    )
    assert "XMLSchema#long" in nquads
    assert "XMLSchema#string" in nquads


def test_thin_vkg_untyped_column_falls_back_to_string():
    # Older callers pass bare {"name": ...} dicts; those must still emit a range.
    nquads = build_thin_vkg_nquads(
        table="orders",
        columns=[{"name": "sku"}],
        class_iri="https://x/orders",
    )
    assert "XMLSchema#string" in nquads


def test_thin_vkg_uses_the_supplied_graph_for_every_quad():
    # One graph per LAYER (not per class) — the query side scopes its CONSTRUCT to
    # that single graph, and the gateway resolves a layer's graph by substring.
    graph = "https://semantic-layer.aws/vkg/hybrid-acme"
    nquads = build_thin_vkg_nquads(
        table="orders",
        columns=[{"name": "qty", "type": "int"}],
        class_iri="https://semantic-layer.aws/virtual-kg/orders",
        graph_iri=graph,
    )
    lines = [line for line in nquads.strip().split("\n") if line]
    assert lines, "expected at least one quad"
    for line in lines:
        assert f"<{graph}> ." in line, line


# --------------------------------------------------------------------------- #
# N-Quads validation (the gateway's silent-drop trap).                        #
# --------------------------------------------------------------------------- #
def test_validate_nquads_accepts_builder_output():
    # The builder's own output must satisfy the gateway's grammar — otherwise the
    # load silently persists only part of the graph.
    nquads = build_thin_vkg_nquads(
        table="orders",
        columns=[{"name": "qty", "type": "bigint"}, {"name": "sku", "type": "string"}],
        class_iri="https://semantic-layer.aws/virtual-kg/orders",
        graph_iri="https://semantic-layer.aws/vkg/hybrid-acme",
    )
    assert validate_nquads(nquads) == len(
        [line for line in nquads.strip().split("\n") if line.strip()]
    )


def test_validate_nquads_rejects_a_line_the_gateway_would_reject():
    # A missing graph position matches the gateway's regex nowhere, so the gateway
    # would refuse the whole payload; catching it here names the offending line.
    bad = '<https://x/A> <https://x/p> "v" .'
    with pytest.raises(ValueError, match="REJECTED"):
        validate_nquads(bad)


# --------------------------------------------------------------------------- #
# Gateway result unwrapping.                                                  #
# --------------------------------------------------------------------------- #
def test_result_text_unwraps_lambda_envelope():
    inner = json.dumps({"success": True, "message": "Persisted 12 triples"})
    envelope = json.dumps({"statusCode": 200, "body": inner})
    assert _result_text(_text_result(envelope)) == inner


def test_result_text_passes_through_plain_json():
    payload = json.dumps({"success": True})
    assert _result_text(_text_result(payload)) == payload


def test_result_text_raises_on_error_status():
    with pytest.raises(RuntimeError, match="gateway tool error"):
        _result_text(_text_result("boom", status="error"))


def test_result_text_falls_back_to_structured_content():
    result = {"status": "success", "content": [], "structuredContent": {"turtle": "x"}}
    assert json.loads(_result_text(result)) == {"turtle": "x"}


# --------------------------------------------------------------------------- #
# NeptuneLoadClient.                                                          #
# --------------------------------------------------------------------------- #
_GOOD_NQUADS = (
    '<https://x/A> <https://x/p> "v" <https://semantic-layer.aws/vkg/l1> .\n'
)


def test_persist_resolves_namespaced_tool_and_sends_nquads():
    mcp = _FakeMcp(
        tool_names=["persist-to-neptune___persist_to_neptune"],
        result=_text_result(json.dumps({"success": True, "message": "Persisted 1"})),
    )
    NeptuneLoadClient(mcp_client=mcp).persist(nquads=_GOOD_NQUADS)
    assert mcp.calls[0]["name"] == "persist-to-neptune___persist_to_neptune"
    assert mcp.calls[0]["arguments"] == {"nquad_data": _GOOD_NQUADS}


def test_persist_raises_when_tool_reports_failure():
    # A failed load must NOT be mistaken for success: the layer would be stamped
    # ready with no graph behind it.
    mcp = _FakeMcp(
        tool_names=["t___persist_to_neptune"],
        result=_text_result(json.dumps({"success": False, "message": "HTTP 400"})),
    )
    with pytest.raises(RuntimeError, match="HTTP 400"):
        NeptuneLoadClient(mcp_client=mcp).persist(nquads=_GOOD_NQUADS)


def test_persist_raises_on_empty_nquads():
    mcp = _FakeMcp(tool_names=["t___persist_to_neptune"], result=_text_result("{}"))
    with pytest.raises(ValueError, match="empty thin VKG"):
        NeptuneLoadClient(mcp_client=mcp).persist(nquads="   \n  ")


def test_persist_raises_when_gateway_lacks_the_tool():
    mcp = _FakeMcp(tool_names=["t___execute_sparql_query"], result=_text_result("{}"))
    with pytest.raises(RuntimeError, match="does not expose tool"):
        NeptuneLoadClient(mcp_client=mcp).persist(nquads=_GOOD_NQUADS)


def test_persist_raises_on_non_json_result():
    mcp = _FakeMcp(
        tool_names=["t___persist_to_neptune"], result=_text_result("<html>502</html>")
    )
    with pytest.raises(RuntimeError, match="non-JSON"):
        NeptuneLoadClient(mcp_client=mcp).persist(nquads=_GOOD_NQUADS)


# --------------------------------------------------------------------------- #
# clear(): a rebuild must REPLACE the layer's graph, not add to it.           #
# --------------------------------------------------------------------------- #
def test_clear_drops_the_layers_graph_by_id():
    mcp = _FakeMcp(
        tool_names=["neptune___delete_graph"],
        result=_text_result(
            json.dumps({"success": True, "graphs_deleted": 1, "triples_deleted": 714})
        ),
    )
    payload = NeptuneLoadClient(mcp_client=mcp).clear(ontology_id="lay-1")
    assert mcp.calls[0]["name"] == "neptune___delete_graph"
    assert mcp.calls[0]["arguments"] == {"ontology_id": "lay-1"}
    assert payload["triples_deleted"] == 714


def test_clear_accepts_a_first_build_with_nothing_to_delete():
    """``graphs_deleted == 0`` is the normal first build, not an error."""
    mcp = _FakeMcp(
        tool_names=["t___delete_graph"],
        result=_text_result(json.dumps({"success": True, "graphs_deleted": 0})),
    )
    assert NeptuneLoadClient(mcp_client=mcp).clear(ontology_id="lay-1")["success"]


def test_clear_raises_when_the_drop_fails():
    """Loading on top of an uncleared graph silently mixes two builds' triples."""
    mcp = _FakeMcp(
        tool_names=["t___delete_graph"],
        result=_text_result(json.dumps({"success": False, "message": "HTTP 500"})),
    )
    with pytest.raises(RuntimeError, match="HTTP 500"):
        NeptuneLoadClient(mcp_client=mcp).clear(ontology_id="lay-1")


def test_clear_raises_without_an_ontology_id():
    """A blank id would ask the gateway to resolve graphs for every layer."""
    mcp = _FakeMcp(tool_names=["t___delete_graph"], result=_text_result("{}"))
    with pytest.raises(ValueError, match="ontology_id"):
        NeptuneLoadClient(mcp_client=mcp).clear(ontology_id="  ")


def test_load_clears_before_it_persists(monkeypatch):
    """Order matters: clearing AFTER the insert would wipe the new graph too."""
    order: List[str] = []

    class _Client:
        def __init__(self, *, mcp_client: Any) -> None:
            """Ignore the transport; record call order only."""

        def clear(self, *, ontology_id: str) -> Dict[str, Any]:
            """Record the clear."""
            order.append(f"clear:{ontology_id}")
            return {"success": True}

        def persist(self, *, nquads: str) -> Dict[str, Any]:
            """Record the persist."""
            order.append("persist")
            return {"success": True}

    class _Session:
        def __enter__(self):
            """Enter the fake MCP session."""
            return self

        def __exit__(self, *_exc):
            """Exit the fake MCP session."""
            return False

    monkeypatch.setenv("NEPTUNE_GATEWAY_URL", "https://gateway.example/mcp")
    monkeypatch.setattr(
        "agents.semantic_layer_creation_agent.neptune_load.build_gateway_client",
        lambda **_kwargs: _Session(),
    )
    monkeypatch.setattr(
        "agents.semantic_layer_creation_agent.neptune_load.NeptuneLoadClient", _Client
    )
    creation_main._load_vkg_into_neptune(namespace="lay-1", nquads=_GOOD_NQUADS)
    assert order == ["clear:lay-1", "persist"]


# --------------------------------------------------------------------------- #
# CONSTRUCT builder.                                                          #
# --------------------------------------------------------------------------- #
def test_escape_sparql_literal():
    assert escape_sparql_literal('a"b\\c') == 'a\\"b\\\\c'
    assert escape_sparql_literal("a\nb\tc") == "a\\nb\\tc"


@pytest.mark.parametrize(
    "candidate,expected",
    [
        ("bird_toxicology.molecule", "molecule"),
        ("cat.db.tbl", "tbl"),
        ("Orders", "orders"),
        ("  spaced.Table  ", "table"),
    ],
)
def test_normalize_candidate(candidate: str, expected: str):
    # mapsToTable stores the BARE table name, so the db/catalog qualifier that
    # Phase 1 emits has to be stripped or nothing matches.
    assert normalize_candidate(candidate) == expected


def test_construct_scopes_to_the_layer_graph_and_covers_both_branches():
    sparql = build_structural_construct(
        namespace="hybrid-acme", candidates=["acme.orders", "acme.customer"]
    )
    assert "GRAPH <https://semantic-layer.aws/vkg/hybrid-acme>" in sparql
    # Both candidate tables present, normalized and lowercased.
    assert '"orders"' in sparql and '"customer"' in sparql
    # Branch A (class by mapsToTable) and branch B (property by domain).
    assert "mapsToTable" in sparql
    assert "rdfs:domain" in sparql
    assert "UNION" in sparql
    # The subject sweep is what makes range/mapsToColumn come along for free.
    assert "?s ?p ?o" in sparql


# --------------------------------------------------------------------------- #
# CONSTRUCT SEMANTICS — the tests above assert on query TEXT, which is exactly  #
# why they all passed while the shipped query returned ZERO triples against the #
# live graph. A FILTER only sees variables bound in its OWN group graph         #
# pattern, so a FILTER(... = ?table) placed inside a UNION branch cannot read a #
# VALUES ?table from the enclosing group: ?table is unbound, every comparison   #
# fails, and Phase 5 silently marks every column unmapped. These tests EXECUTE  #
# the generated query over a real store built by the real VKG builder, so a     #
# scoping regression fails here instead of in production.                       #
# --------------------------------------------------------------------------- #
_ACME_GRAPH = "https://semantic-layer.aws/vkg/hybrid-acme"
_OTHER_GRAPH = "https://semantic-layer.aws/vkg/hybrid-other"


def _vkg_dataset() -> Any:
    """Build an rdflib Dataset holding three tables' thin VKG across two layers.

    Uses the creation agent's real ``build_thin_vkg_nquads`` so the query side is
    exercised against the exact shapes it will meet in Neptune. ``Orders`` is
    deliberately mixed-case to cover the ``LCASE`` match, and ``shipment`` /
    the other layer's ``orders`` are decoys that must NOT come back.

    Returns:
        Any: An rdflib ``Dataset`` with the quads parsed in.
    """
    from rdflib import Dataset  # noqa: PLC0415 - optional dep, test-only

    def _quads(*, table: str, graph: str) -> str:
        """Emit one table's thin VKG N-Quads into the given named graph."""
        return build_thin_vkg_nquads(
            table=table,
            columns=[{"name": "id", "type": "bigint"}, {"name": "sku", "type": "string"}],
            class_iri=f"https://semantic-layer.aws/virtual-kg/{table.lower()}",
            graph_iri=graph,
        )

    nquads = (
        _quads(table="Orders", graph=_ACME_GRAPH)
        + _quads(table="shipment", graph=_ACME_GRAPH)
        + _quads(table="orders", graph=_OTHER_GRAPH)
    )
    dataset = Dataset()
    dataset.parse(data=nquads, format="nquads")
    return dataset


def _construct(*, candidates: List[str], namespace: str = "hybrid-acme") -> Any:
    """Run the generated CONSTRUCT over the fixture dataset.

    Parameters:
        candidates: Candidate table ids to slice on.
        namespace: The layer namespace whose graph to query.
    Returns:
        Any: The rdflib Graph the CONSTRUCT produced.
    """
    sparql = build_structural_construct(namespace=namespace, candidates=candidates)
    return _vkg_dataset().query(sparql).graph


def test_construct_actually_returns_triples_for_a_candidate_table():
    # The regression this pins: the shipped query was syntactically valid and
    # returned 200 with ZERO triples, so nothing downstream noticed.
    result = _construct(candidates=["acme.orders"])
    assert len(result) > 0, "structural slice must not be empty"


def test_construct_returns_the_class_and_its_properties():
    from rdflib import RDFS, URIRef  # noqa: PLC0415 - optional dep, test-only

    result = _construct(candidates=["acme.orders"])
    cls = URIRef("https://semantic-layer.aws/virtual-kg/orders")
    maps_to_table = URIRef("https://semantic-layer.aws/virtual-kg/mapsToTable")
    maps_to_column = URIRef("https://semantic-layer.aws/virtual-kg/mapsToColumn")
    # Branch A: the class itself, with its table mapping.
    assert (cls, maps_to_table, None) in result
    # Branch B: both columns' properties, reached via rdfs:domain.
    props = {s for s, _, _ in result.triples((None, RDFS.domain, cls))}
    assert len(props) == 2, f"expected both column properties, got {props}"
    # The subject sweep must carry the mappings and ranges along for free —
    # these are what the grounding verifier reads to mark a column mapped.
    for prop in props:
        assert (prop, maps_to_column, None) in result
        assert (prop, RDFS.range, None) in result


def test_construct_excludes_tables_outside_the_candidate_set():
    result = _construct(candidates=["acme.orders"])
    assert "shipment" not in result.serialize(format="nt"), (
        "a non-candidate table leaked into the slice; the VALUES filter is not "
        "constraining anything"
    )


def test_construct_is_case_insensitive_on_the_table_name():
    # The fixture's mapsToTable literal is "Orders"; Phase 1 emits lowercase.
    assert len(_construct(candidates=["acme.ORDERS"])) > 0


def test_construct_is_scoped_to_the_layer_graph():
    # The other layer also has an "orders" table. Querying a namespace with no
    # matching graph must come back empty rather than borrowing another layer's.
    empty = _construct(candidates=["acme.orders"], namespace="hybrid-missing")
    assert len(empty) == 0


def test_construct_returns_a_slice_per_candidate_when_several_are_given():
    both = _construct(candidates=["acme.orders", "acme.shipment"])
    only_orders = _construct(candidates=["acme.orders"])
    assert len(both) > len(only_orders), (
        "adding a candidate must widen the slice; equal sizes mean the VALUES "
        "block is being ignored"
    )


def test_construct_deduplicates_and_sorts_candidates():
    sparql = build_structural_construct(
        namespace="l1", candidates=["db.orders", "other.Orders", "db.customer"]
    )
    values_line = [ln for ln in sparql.split("\n") if "VALUES ?table" in ln][0]
    assert values_line.count('"orders"') == 1
    assert values_line.index('"customer"') < values_line.index('"orders"')


def test_construct_raises_without_candidates():
    # An unconstrained CONSTRUCT would drag the whole graph back, defeating "thin".
    with pytest.raises(ValueError, match="at least one candidate"):
        build_structural_construct(namespace="l1", candidates=[])


def test_construct_raises_when_candidates_are_all_blank():
    with pytest.raises(ValueError, match="at least one candidate"):
        build_structural_construct(namespace="l1", candidates=["  ", ""])


def test_vkg_graph_iri_matches_on_both_sides():
    # The two agents derive the graph IRI independently (isolation rule forbids a
    # shared import), so pin that they agree — a divergence means the query side
    # queries an empty graph.
    assert vkg_graph_iri(namespace="hybrid-acme") == creation_main.vkg_graph_iri(
        namespace="hybrid-acme"
    )


def test_vkg_graph_iri_embeds_the_namespace():
    # The gateway's _resolve_graph_uri finds a layer's graph by substring match.
    assert "hybrid-acme" in vkg_graph_iri(namespace="hybrid-acme")


# --------------------------------------------------------------------------- #
# NeptuneGatewayClient.                                                       #
# --------------------------------------------------------------------------- #
def test_construct_client_returns_turtle():
    mcp = _FakeMcp(
        tool_names=["neptune___execute_sparql_query"],
        result=_text_result(json.dumps({"query_type": "CONSTRUCT", "turtle": "<a> <b> <c> ."})),
    )
    turtle = NeptuneGatewayClient(mcp_client=mcp).construct(sparql="CONSTRUCT {}")
    assert turtle == "<a> <b> <c> ."
    assert mcp.calls[0]["arguments"]["query_type"] == "CONSTRUCT"


def test_construct_client_raises_on_error_payload():
    # Phase 5 converts a raised fetch error into hybrid_verify_unavailable; a
    # swallowed error would instead yield an unverified answer.
    mcp = _FakeMcp(
        tool_names=["n___execute_sparql_query"],
        result=_text_result(json.dumps({"error": "MalformedQueryException"})),
    )
    with pytest.raises(RuntimeError, match="CONSTRUCT failed"):
        NeptuneGatewayClient(mcp_client=mcp).construct(sparql="CONSTRUCT {}")


def test_construct_client_accepts_raw_turtle():
    mcp = _FakeMcp(
        tool_names=["n___execute_sparql_query"], result=_text_result("<a> <b> <c> .")
    )
    assert NeptuneGatewayClient(mcp_client=mcp).construct(
        sparql="CONSTRUCT {}"
    ) == "<a> <b> <c> ."


# --------------------------------------------------------------------------- #
# Ontology fetch + Ontop translate (the governed-rule tier's two gateway hops).#
# --------------------------------------------------------------------------- #
def test_select_json_returns_the_unflattened_bindings():
    # rule_graph.read_rules rebuilds an RDF graph from these, so the term `type`
    # must survive: flattened to values it cannot tell an IRI from a literal and
    # every rule field comes back a string.
    payload = {
        "head": {"vars": ["s", "p", "o"]},
        "results": {"bindings": [{
            "s": {"type": "uri", "value": "https://ex/#RULE-1"},
            "p": {"type": "uri", "value": "https://ex/vkgr#name"},
            "o": {"type": "literal", "value": "lapse risk"},
        }]},
    }
    mcp = _FakeMcp(
        tool_names=["neptune___execute_sparql_query"],
        result=_text_result(json.dumps(payload)),
    )
    out = NeptuneGatewayClient(mcp_client=mcp).select_json(sparql="SELECT * {}")
    assert out == payload
    assert mcp.calls[0]["arguments"] == {
        "sparql_query": "SELECT * {}", "query_type": "SELECT",
    }


def test_select_json_raises_on_an_error_payload():
    # Unlike fetch_ontology, an unreadable rules graph RAISES: returned as data it
    # would read as "this layer has no rules" on every question, forever.
    mcp = _FakeMcp(
        tool_names=["n___execute_sparql_query"],
        result=_text_result(json.dumps({"error": "MalformedQueryException"})),
    )
    with pytest.raises(RuntimeError, match="SELECT failed"):
        NeptuneGatewayClient(mcp_client=mcp).select_json(sparql="SELECT * {}")


def test_select_json_raises_on_a_non_json_body():
    mcp = _FakeMcp(
        tool_names=["n___execute_sparql_query"],
        result=_text_result("<html>502</html>"),
    )
    with pytest.raises(RuntimeError, match="non-JSON"):
        NeptuneGatewayClient(mcp_client=mcp).select_json(sparql="SELECT * {}")


def test_fetch_ontology_returns_the_parsed_payload():
    payload = {
        "classes": {"https://ex/#Claim": {"label": "Claim"}},
        "properties": {},
        "mappings": {"https://ex/#amount": {"table": "claims", "column": "amount"}},
    }
    mcp = _FakeMcp(
        tool_names=["neptune___get_ontology_from_neptune"],
        result=_text_result(json.dumps(payload)),
    )
    out = NeptuneGatewayClient(mcp_client=mcp).fetch_ontology(ontology_id="ont-1")
    assert out == payload
    assert mcp.calls[0]["name"] == "neptune___get_ontology_from_neptune"
    assert mcp.calls[0]["arguments"] == {"ontology_id": "ont-1"}


def test_fetch_ontology_returns_an_error_payload_rather_than_raising():
    # An empty graph is a fact about the layer, not a transport failure — the
    # rule tier degrades to Tier 2 on it, so it must be inspectable.
    mcp = _FakeMcp(
        tool_names=["n___get_ontology_from_neptune"],
        result=_text_result(json.dumps({"error": "no graph for ont-1"})),
    )
    out = NeptuneGatewayClient(mcp_client=mcp).fetch_ontology(ontology_id="ont-1")
    assert out["error"] == "no graph for ont-1"


def test_fetch_ontology_raises_on_a_non_json_body():
    mcp = _FakeMcp(
        tool_names=["n___get_ontology_from_neptune"],
        result=_text_result("<html>502</html>"),
    )
    with pytest.raises(RuntimeError, match="non-JSON"):
        NeptuneGatewayClient(mcp_client=mcp).fetch_ontology(ontology_id="ont-1")


def test_translate_sql_sends_the_ontology_id_for_the_warm_cache():
    mcp = _FakeMcp(
        tool_names=["neptune___translate_sparql_to_sql"],
        result=_text_result(json.dumps({"sql": "SELECT 1", "database": "db"})),
    )
    out = NeptuneGatewayClient(mcp_client=mcp).translate_sql(
        sparql="SELECT * WHERE {}", ontology_json={"mappings": {}}, ontology_id="ont-1",
    )
    assert out["sql"] == "SELECT 1"
    assert mcp.calls[0]["arguments"]["ontologyId"] == "ont-1"
    assert mcp.calls[0]["arguments"]["ontologyJson"] == {"mappings": {}}


def test_translate_sql_omits_an_empty_ontology_id():
    # Sending ontologyId="" would key the handler's reformulator cache on the
    # empty string for EVERY layer; omitting it restores the content-hash fallback.
    mcp = _FakeMcp(
        tool_names=["n___translate_sparql_to_sql"],
        result=_text_result(json.dumps({"sql": "SELECT 1"})),
    )
    NeptuneGatewayClient(mcp_client=mcp).translate_sql(
        sparql="SELECT * WHERE {}", ontology_json={},
    )
    assert "ontologyId" not in mcp.calls[0]["arguments"]


def test_translate_sql_returns_an_error_payload_rather_than_raising():
    # A rule Ontop cannot reformulate must fall through to Tier 2, not 5xx.
    mcp = _FakeMcp(
        tool_names=["n___translate_sparql_to_sql"],
        result=_text_result(json.dumps({"error": "unmapped predicate"})),
    )
    out = NeptuneGatewayClient(mcp_client=mcp).translate_sql(
        sparql="SELECT * WHERE {}", ontology_json={},
    )
    assert out["error"] == "unmapped predicate"
    assert "sql" not in out


def test_gateway_raises_when_a_needed_tool_is_absent():
    # Failing loudly here is the point: a silently-missing tool would look like
    # "this layer has no rules" on every question.
    mcp = _FakeMcp(tool_names=["n___execute_sparql_query"], result=_text_result("{}"))
    with pytest.raises(RuntimeError, match="translate_sparql_to_sql"):
        NeptuneGatewayClient(mcp_client=mcp).translate_sql(
            sparql="SELECT * WHERE {}", ontology_json={},
        )


# --------------------------------------------------------------------------- #
# Vocabulary agreement (the empty-inverse-index trap).                        #
# --------------------------------------------------------------------------- #
def test_mapping_vocabulary_is_identical_on_both_sides():
    # The creation agent writes <vocab>mapsToTable/mapsToColumn and the query
    # agent reads them by FULL IRI. When these drifted, build_inverse_index
    # returned {} and every hybrid query abstained as ungrounded with no error.
    from agents.semantic_layer_creation_agent.ontop_mappings import (
        VIRTUAL_KG_VOCAB as creation_vocab,
    )
    from agents.semantic_layer_query_agent.ontop_mappings import (
        VIRTUAL_KG_VOCAB as query_vocab,
    )

    assert creation_vocab == query_vocab


def test_query_vocab_default_matches_what_the_builder_emits():
    from agents.semantic_layer_query_agent import main as query_main
    from agents.semantic_layer_query_agent.ontop_mappings import VIRTUAL_KG_VOCAB

    assert query_main._vocab_for({}) == VIRTUAL_KG_VOCAB


def test_query_vocab_config_override_wins():
    from agents.semantic_layer_query_agent import main as query_main

    assert query_main._vocab_for({"vkgVocab": "https://custom#"}) == "https://custom#"


def test_built_vkg_is_indexable_by_the_query_side():
    """End-to-end on the pure pieces: build quads, index them, expect mappings.

    This is the regression that catches BOTH the vocab drift and the
    ``columns=[]`` bug — either one leaves the inverse index empty.
    """
    from rdflib import Graph

    from agents.semantic_layer_query_agent.ontop_mappings import (
        VIRTUAL_KG_VOCAB,
        build_inverse_index,
    )

    nquads = build_thin_vkg_nquads(
        table="orders",
        columns=[{"name": "qty", "type": "bigint"}],
        class_iri=f"{VIRTUAL_KG_VOCAB}orders",
        graph_iri=vkg_graph_iri(namespace="hybrid-acme"),
    )
    # The CONSTRUCT slice arrives as triples, so parse as N-Triples by dropping
    # the graph position — equivalent to what the gateway's Turtle output gives us.
    triples = "\n".join(
        " ".join(line.split(" ")[:-2]) + " ."
        for line in nquads.strip().split("\n")
        if line.strip()
    )
    graph = Graph()
    graph.parse(data=triples, format="nt")
    idx = build_inverse_index(graph, vocab=VIRTUAL_KG_VOCAB)
    assert idx.class_by_table["orders"] == f"{VIRTUAL_KG_VOCAB}orders"
    assert idx.pred_by_column["orders.qty"] == f"{VIRTUAL_KG_VOCAB}orders/qty"


# --------------------------------------------------------------------------- #
# Creation-side Glue column read + vkgBuilt stamp.                            #
# --------------------------------------------------------------------------- #
class _FakeGlue:
    """Minimal Glue client stand-in for ``get_table``."""

    def __init__(self, *, table: Dict[str, Any]) -> None:
        """Store the canned table payload.

        Parameters:
            table: The value to return under the ``Table`` key.
        Returns:
            None.
        """
        self._table = table
        self.kwargs: Optional[Dict[str, Any]] = None

    def get_table(self, **kwargs: Any) -> Dict[str, Any]:
        """Record the request kwargs and return the canned table."""
        self.kwargs = kwargs
        return {"Table": self._table}


class _FakeSession:
    """boto3 Session stand-in returning one canned client."""

    def __init__(self, *, client: Any) -> None:
        """Store the client to hand out.

        Parameters:
            client: The object every ``client()`` call returns.
        Returns:
            None.
        """
        self._client = client

    def client(self, *_args: Any, **_kwargs: Any) -> Any:
        """Return the canned client regardless of service name."""
        return self._client

    @property
    def region_name(self) -> str:
        """Region reported to set_boto_session's logging."""
        return "us-east-1"


def _use_glue(monkeypatch: pytest.MonkeyPatch, glue: _FakeGlue) -> None:
    """Point the creation agent's boto session at ``glue``.

    Parameters:
        monkeypatch: pytest monkeypatch fixture.
        glue: The fake Glue client to install.
    Returns:
        None.
    """
    monkeypatch.setattr(
        creation_main, "get_boto_session", lambda: _FakeSession(client=glue)
    )


def test_glue_columns_includes_partition_keys(monkeypatch: pytest.MonkeyPatch):
    # Partition keys live OUTSIDE StorageDescriptor.Columns but are queryable, so
    # omitting them would leave un-mappable columns in generated SQL.
    glue = _FakeGlue(
        table={
            "StorageDescriptor": {"Columns": [{"Name": "qty", "Type": "bigint"}]},
            "PartitionKeys": [{"Name": "dt", "Type": "date"}],
        }
    )
    _use_glue(monkeypatch, glue)
    cols = creation_main._glue_columns(
        database="acme", table="orders", catalog_id="AWSDataCatalog"
    )
    assert [c["name"] for c in cols] == ["qty", "dt"]
    assert cols[1]["type"] == "date"


def test_glue_columns_omits_catalog_id_for_default_catalog(
    monkeypatch: pytest.MonkeyPatch,
):
    # Glue rejects the literal "AWSDataCatalog" as a CatalogId.
    glue = _FakeGlue(
        table={"StorageDescriptor": {"Columns": [{"Name": "a", "Type": "string"}]}}
    )
    _use_glue(monkeypatch, glue)
    creation_main._glue_columns(
        database="acme", table="orders", catalog_id="AWSDataCatalog"
    )
    assert "CatalogId" not in (glue.kwargs or {})


def test_glue_columns_passes_federated_catalog_id(monkeypatch: pytest.MonkeyPatch):
    glue = _FakeGlue(
        table={"StorageDescriptor": {"Columns": [{"Name": "a", "Type": "string"}]}}
    )
    _use_glue(monkeypatch, glue)
    catalog = "111122223333:s3tablescatalog/semantic-layer-dev-analytics-tables"
    creation_main._glue_columns(
        database="bird_toxicology", table="molecule", catalog_id=catalog
    )
    assert (glue.kwargs or {})["CatalogId"] == catalog


def test_glue_columns_raises_when_no_columns(monkeypatch: pytest.MonkeyPatch):
    # A column-less VKG has no datatype properties and no mapsToColumn mappings —
    # it cannot ground anything, so this must fail rather than build an empty graph.
    glue = _FakeGlue(table={"StorageDescriptor": {"Columns": []}})
    _use_glue(monkeypatch, glue)
    with pytest.raises(RuntimeError, match="no columns"):
        creation_main._glue_columns(
            database="acme", table="orders", catalog_id="AWSDataCatalog"
        )


def test_probe_table_is_fail_soft_when_glue_denies(monkeypatch: pytest.MonkeyPatch):
    # The KB is advisory prose, so one table's probe failure must not abort the
    # corpus. (Contrast _glue_columns above, which is fail-LOUD for the VKG.)
    def _boom(**_kwargs: Any) -> List[Dict[str, str]]:
        raise RuntimeError("AccessDeniedException")

    monkeypatch.setattr(creation_main, "_glue_columns", _boom)
    schema_json, sample_json = creation_main._probe_table(
        database="acme", table="orders", catalog_id="AWSDataCatalog"
    )
    assert json.loads(schema_json) == {}
    assert json.loads(sample_json) == {}


def test_load_vkg_raises_without_gateway(monkeypatch: pytest.MonkeyPatch):
    # No gateway must fail the BUILD, not quietly mark the layer complete.
    monkeypatch.delenv("NEPTUNE_GATEWAY_URL", raising=False)
    with pytest.raises(RuntimeError, match="NEPTUNE_GATEWAY_URL"):
        creation_main._load_vkg_into_neptune(namespace="l1", nquads=_GOOD_NQUADS)


def test_build_vkg_stamps_only_after_a_successful_load(
    monkeypatch: pytest.MonkeyPatch,
):
    # The query side gates on vkgBuilt, so stamping before the load succeeds would
    # advertise a layer with no graph behind it.
    calls: List[str] = []

    monkeypatch.setattr(
        creation_main,
        "_glue_columns",
        lambda **_k: [{"name": "qty", "type": "bigint", "comment": ""}],
    )

    def _fail_load(**_kwargs: Any) -> None:
        calls.append("load")
        raise RuntimeError("gateway down")

    monkeypatch.setattr(creation_main, "_load_vkg_into_neptune", _fail_load)
    monkeypatch.setattr(
        creation_main, "_stamp_vkg_built", lambda **_k: calls.append("stamp")
    )

    # Descriptions are harvested from S3 in the real build; stub the read so this
    # test stays hermetic (their absence is fail-soft, not the subject here).
    monkeypatch.setattr(creation_main, "_harvest_kb_descriptions", lambda **_k: {})

    build_vkg = creation_main._make_build_vkg(
        id="hybrid-acme",
        version="v1",
        config={},
        tables=[{"database": "acme", "table": "orders", "catalogId": "AWSDataCatalog"}],
    )
    with pytest.raises(RuntimeError, match="gateway down"):
        build_vkg()
    assert calls == ["load"], "stamp must not run when the load failed"


def test_build_vkg_passes_real_columns_and_layer_graph(
    monkeypatch: pytest.MonkeyPatch,
):
    # THE bug this wiring fixes: columns=[] produced a classes-only graph.
    captured: Dict[str, str] = {}

    monkeypatch.setattr(
        creation_main,
        "_glue_columns",
        lambda **_k: [{"name": "qty", "type": "bigint", "comment": ""}],
    )
    monkeypatch.setattr(
        creation_main,
        "_load_vkg_into_neptune",
        lambda *, namespace, nquads: captured.update(ns=namespace, nq=nquads),
    )
    monkeypatch.setattr(creation_main, "_stamp_vkg_built", lambda **_k: None)
    # The same N-Quads are also persisted to S3 for the layer's Metadata tab.
    monkeypatch.setattr(creation_main, "_save_vkg_to_s3", lambda **_k: "s3://b/k.nq")
    monkeypatch.setattr(creation_main, "_harvest_kb_descriptions", lambda **_k: {})

    creation_main._make_build_vkg(
        id="hybrid-acme",
        version="v1",
        config={},
        tables=[{"database": "acme", "table": "orders", "catalogId": "AWSDataCatalog"}],
    )()

    assert captured["ns"] == "hybrid-acme"
    assert "mapsToColumn" in captured["nq"], "no column mappings => cannot ground"
    assert "XMLSchema#long" in captured["nq"], "real Glue type must reach the range"
    assert "https://semantic-layer.aws/vkg/hybrid-acme" in captured["nq"]


def test_stamp_vkg_built_writes_both_fields(monkeypatch: pytest.MonkeyPatch):
    updates: List[Dict[str, Any]] = []

    class _FakeTable:
        def update_item(self, **kwargs: Any) -> None:
            """Record the update request."""
            updates.append(kwargs)

    monkeypatch.setattr(creation_main, "_metadata_table", lambda: _FakeTable())
    monkeypatch.setattr(creation_main, "_resolve_active_version", lambda _t, _i: "v3")
    creation_main._stamp_vkg_built(id="hybrid-acme", graph_iri="https://g/1")

    assert updates[0]["Key"] == {"id": "hybrid-acme", "version": "v3"}
    assert updates[0]["ExpressionAttributeValues"][":built"] is True
    assert updates[0]["ExpressionAttributeValues"][":graph"] == "https://g/1"


def test_stamp_vkg_built_raises_on_write_failure(monkeypatch: pytest.MonkeyPatch):
    # An unstamped layer is unusable by the query side, so the stamp is NOT
    # best-effort — unlike _write_terminal_status.
    class _BoomTable:
        def update_item(self, **_kwargs: Any) -> None:
            """Simulate a DynamoDB failure."""
            raise RuntimeError("ProvisionedThroughputExceeded")

    monkeypatch.setattr(creation_main, "_metadata_table", lambda: _BoomTable())
    monkeypatch.setattr(creation_main, "_resolve_active_version", lambda _t, _i: "v1")
    with pytest.raises(RuntimeError, match="could not stamp"):
        creation_main._stamp_vkg_built(id="hybrid-acme", graph_iri="https://g/1")


# --------------------------------------------------------------------------- #
# Query-side artifact gate.                                                   #
# --------------------------------------------------------------------------- #
def test_vkg_exists_requires_gateway_and_stamp(monkeypatch: pytest.MonkeyPatch):
    from agents.semantic_layer_query_agent import main as query_main

    monkeypatch.setenv("NEPTUNE_GATEWAY_URL", "https://gw.example/mcp")
    assert query_main._vkg_exists(namespace="l1", config={"vkgBuilt": True}) is True
    assert query_main._vkg_exists(namespace="l1", config={"vkgGraphId": "g"}) is True
    # Stamped but unreachable, and reachable but unstamped, are both unusable.
    assert query_main._vkg_exists(namespace="l1", config={}) is False
    monkeypatch.delenv("NEPTUNE_GATEWAY_URL", raising=False)
    assert query_main._vkg_exists(namespace="l1", config={"vkgBuilt": True}) is False


def test_neptune_construct_ttl_raises_without_gateway(monkeypatch: pytest.MonkeyPatch):
    from agents.semantic_layer_query_agent import main as query_main

    monkeypatch.delenv("NEPTUNE_GATEWAY_URL", raising=False)
    with pytest.raises(RuntimeError, match="NEPTUNE_GATEWAY_URL"):
        query_main._neptune_construct_ttl(namespace="l1", candidates=["db.orders"])
