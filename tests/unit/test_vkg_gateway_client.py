"""Unit tests for the Neptune Gateway MCP client used by the VKG Tier 2 workflow.

A fake MCPClient records the (name, arguments) of each call and returns canned
text payloads, so we exercise tool-name resolution (``<target>___<tool>`` suffix
match) and the three helpers' parsing without a live gateway.
"""
import json
from types import SimpleNamespace

from agents.ontology_query_agent.tier2.gateway_client import NeptuneGatewayClient


class _FakeMCP:
    """Minimal MCPClient stand-in: namespaced tool list + scripted results."""

    def __init__(self, results):
        # results: {bare_tool_name: payload_str}
        self._results = results
        self.calls = []

    def list_tools_sync(self):
        # Gateway exposes namespaced ids like "<target>___<tool>".
        return [
            SimpleNamespace(tool_name="get-ontology-from-neptune___get_ontology_from_neptune"),
            SimpleNamespace(tool_name="execute-sparql-query___execute_sparql_query"),
            SimpleNamespace(tool_name="ontop-translate___translate_sparql_to_sql"),
        ]

    def call_tool_sync(self, *, tool_use_id, name, arguments=None):
        self.calls.append((name, arguments))
        # Find the canned payload by the bare suffix of the namespaced name.
        bare = name.split("___", 1)[1] if "___" in name else name
        text = self._results.get(bare, "")
        return {"status": "success", "content": [{"text": text}]}


def test_fetch_ontology_resolves_namespaced_name_and_parses_json():
    ont = {"classes": {"ex:A": {}}, "properties": {}, "mappings": {},
           "databases": [{"name": "db", "catalog": "cat"}]}
    mcp = _FakeMCP({"get_ontology_from_neptune": json.dumps(ont)})
    client = NeptuneGatewayClient(mcp_client=mcp)
    out = client.fetch_ontology(ontology_id="ont-1")
    assert out["databases"][0]["catalog"] == "cat"
    # called the namespaced tool name, with the ontology_id arg
    called_name, called_args = mcp.calls[0]
    assert called_name.endswith("___get_ontology_from_neptune")
    assert called_args == {"ontology_id": "ont-1"}


def test_construct_returns_turtle_from_wrapper():
    ttl = "@prefix ex: <http://ex/> .\nex:A a ex:Class ."
    mcp = _FakeMCP({"execute_sparql_query": json.dumps({"query_type": "CONSTRUCT",
                                                        "turtle": ttl})})
    client = NeptuneGatewayClient(mcp_client=mcp)
    out = client.construct(sparql="CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }")
    assert out == ttl
    _, args = mcp.calls[0]
    assert args["query_type"] == "CONSTRUCT"


def test_run_select_flattens_bindings_to_rows():
    payload = {
        "head": {"vars": ["c", "p"]},
        "results": {"bindings": [
            {"c": {"value": "Customer1"}, "p": {"value": "P1"}},
            {"c": {"value": "Customer2"}},  # missing optional p → ""
        ]},
    }
    mcp = _FakeMCP({"execute_sparql_query": json.dumps(payload)})
    client = NeptuneGatewayClient(mcp_client=mcp)
    out = client.run_select(sparql="SELECT ?c ?p WHERE { ?c ?x ?p }")
    assert out["columns"] == ["c", "p"]
    assert out["rows"] == [["Customer1", "P1"], ["Customer2", ""]]
    _, args = mcp.calls[0]
    assert args["query_type"] == "SELECT"


def test_translate_sql_resolves_name_and_parses_json():
    """Drive translate_sql through the real _call/_result_text/json.loads chain."""
    payload = {"sql": "SELECT * FROM normalized.t", "database": "normalized",
               "catalog": "AwsDataCatalog"}
    mcp = _FakeMCP({"translate_sparql_to_sql": json.dumps(payload)})
    client = NeptuneGatewayClient(mcp_client=mcp)
    out = client.translate_sql(sparql="SELECT ?x WHERE { ?x a <http://ex/A> }",
                               ontology_json={"mappings": {}})
    assert out == payload
    called_name, called_args = mcp.calls[0]
    # suffix resolution picked the namespaced translate tool
    assert called_name == "ontop-translate___translate_sparql_to_sql"
    # ontologyJson is sent camelCase (Java Handler contract)
    assert "ontologyJson" in called_args
    assert called_args["ontologyJson"] == {"mappings": {}}


def test_translate_sql_parses_statuscode_body_envelope():
    """The {statusCode, body} envelope is peeled by _result_text before json.loads."""
    inner = {"sql": "SELECT 1", "database": "normalized", "catalog": "AwsDataCatalog"}
    envelope = json.dumps({"statusCode": 200, "body": json.dumps(inner)})
    mcp = _FakeMCP({"translate_sparql_to_sql": envelope})
    client = NeptuneGatewayClient(mcp_client=mcp)
    out = client.translate_sql(sparql="SELECT ?x WHERE { ?x a <http://ex/A> }",
                               ontology_json={})
    assert out == inner


def test_translate_sql_omits_ontology_id_when_empty_and_sends_when_present():
    """Empty ontology_id is omitted; a non-empty one is sent as ontologyId."""
    mcp = _FakeMCP({"translate_sparql_to_sql": json.dumps({"sql": "SELECT 1"})})
    client = NeptuneGatewayClient(mcp_client=mcp)
    # empty → key omitted (Handler hash-fallback)
    client.translate_sql(sparql="SELECT ?x WHERE {}", ontology_json={})
    _, args0 = mcp.calls[0]
    assert "ontologyId" not in args0
    # non-empty → sent for the warm reformulator cache
    client.translate_sql(sparql="SELECT ?x WHERE {}", ontology_json={}, ontology_id="ont-7")
    _, args1 = mcp.calls[1]
    assert args1["ontologyId"] == "ont-7"


def test_construct_raises_on_gateway_error():
    mcp = _FakeMCP({"execute_sparql_query": json.dumps({"error": "boom"})})
    client = NeptuneGatewayClient(mcp_client=mcp)
    try:
        client.construct(sparql="CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "boom" in str(e)
