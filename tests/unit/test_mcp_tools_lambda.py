"""Tests for the MCP tools Lambda invoked by AgentCore Gateway."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Absolute path to the mcp-tools Lambda's index.py. We load by file path rather
# than `import index` to avoid colliding with other test modules that also
# expose an `index.py` on sys.path (e.g. lambda/neptune-tools/index.py loaded
# by test_neptune_tools_get_ontology.py). Without this, whichever test file
# was collected last "wins" sys.path[0] and `import index` returns the wrong
# module under the full-suite run.
_MCP_TOOLS_INDEX_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), '..', '..', 'lambda', 'mcp-tools', 'index.py'
    )
)


def _load_mcp_tools_index():
    """Load lambda/mcp-tools/index.py from its absolute path under a unique name."""
    spec = importlib.util.spec_from_file_location(
        'mcp_tools_index', _MCP_TOOLS_INDEX_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Could not load spec for {_MCP_TOOLS_INDEX_PATH}')
    module = importlib.util.module_from_spec(spec)
    # Register under a unique name so other tests' `import index` cannot shadow
    # us, and so we cannot shadow them.
    sys.modules['mcp_tools_index'] = module
    spec.loader.exec_module(module)
    return module


def _gateway_context(tool_name: str):
    """Build a Lambda context that mirrors the Gateway's client_context."""
    return SimpleNamespace(
        client_context=SimpleNamespace(
            custom={'bedrockAgentCoreToolName': tool_name}
        )
    )


@pytest.fixture(autouse=True)
def index():
    """Load a fresh copy of lambda/mcp-tools/index.py for every test.

    Yields the loaded module so tests can use `index` as a fixture parameter.
    """
    sys.modules.pop('mcp_tools_index', None)
    yield _load_mcp_tools_index()
    sys.modules.pop('mcp_tools_index', None)


def test_lambda_handler_rejects_unknown_tool(index):
    out = index.lambda_handler({}, _gateway_context('Unknown'))
    assert out['statusCode'] == 400
    assert 'unknown tool' in json.loads(out['body'])['error']


def test_resolve_tool_name_strips_target_prefix(index):
    ctx = _gateway_context('mcp-tools-target___OntologyQuery')
    assert index._resolve_tool_name(ctx) == 'OntologyQuery'


def test_resolve_tool_name_handles_unprefixed(index):
    ctx = _gateway_context('OntologyQuery')
    assert index._resolve_tool_name(ctx) == 'OntologyQuery'


def test_ontology_query_returns_blocked_on_input_guardrail(index, monkeypatch):
    monkeypatch.setenv('GUARDRAIL_IDENTIFIER', 'g')
    monkeypatch.setenv('GUARDRAIL_VERSION', '1')
    monkeypatch.setenv(
        'QUERY_RUNTIME_ARN',
        'arn:aws:bedrock-agentcore:us-east-1:0:runtime/q',
    )

    fake_runtime = MagicMock()
    fake_runtime.apply_guardrail.return_value = {
        'action': 'GUARDRAIL_INTERVENED',
        'outputs': [{'text': 'no'}],
    }
    with patch.object(index, '_bedrock_runtime_client', return_value=fake_runtime):
        out = index.tool_ontology_query(
            {'ontologyId': 'o', 'question': 'forbidden'}
        )
    assert out['error'] == 'blocked input'
    fake_runtime.apply_guardrail.assert_called_once()


def test_ontology_query_invokes_runtime_and_returns_struct(index, monkeypatch):
    monkeypatch.setenv(
        'QUERY_RUNTIME_ARN',
        'arn:aws:bedrock-agentcore:us-east-1:0:runtime/q',
    )
    monkeypatch.delenv('GUARDRAIL_IDENTIFIER', raising=False)

    # The runtimes are JWT-inbound now: tools call _invoke_runtime_sync (HTTPS +
    # M2M Bearer token), which returns the parsed runtime response dict.
    parsed = {
        'answer': 'two',
        'sql_query': 'SELECT 1',                         # SPARQL lineage
        'results': [{'count': 2}],
        'reasoning': {'graphTraversal': 'ex:Holding',
                      'sqlQuery': 'SELECT COUNT(*) FROM normalized.admin_codes'},
    }
    with patch.object(index, '_invoke_runtime_sync', return_value=parsed) as inv:
        out = index.tool_ontology_query(
            {'ontologyId': 'ont-1', 'question': 'how many?'}
        )
    assert out['answer'] == 'two'
    assert out['sql'] == 'SELECT 1'
    assert out['rows'] == [{'count': 2}]
    # The EXECUTED Athena SQL is surfaced top-level (todo item 4), distinct from
    # the SPARQL lineage in 'sql'. The full reasoning dict stays in 'lineage'.
    assert out['executed_sql'] == 'SELECT COUNT(*) FROM normalized.admin_codes'
    assert out['lineage'] == {'graphTraversal': 'ex:Holding',
                              'sqlQuery': 'SELECT COUNT(*) FROM normalized.admin_codes'}
    inv.assert_called_once()
    kw = inv.call_args.kwargs
    assert kw['runtime_arn'].endswith('/q')
    assert kw['payload'] == {'question': 'how many?', 'id': 'ont-1'}


def test_metadata_query_returns_struct(index, monkeypatch):
    monkeypatch.setenv(
        'METADATA_QUERY_RUNTIME_ARN',
        'arn:aws:bedrock-agentcore:us-east-1:0:runtime/m',
    )
    monkeypatch.delenv('GUARDRAIL_IDENTIFIER', raising=False)

    parsed = {
        'answer': 'OK',
        'sql_query': 'SELECT 1',
        'results': [{'a': 1}],
        'n_quads': [{'sourceUri': 'kb://x'}],
    }
    with patch.object(index, '_invoke_runtime_sync', return_value=parsed):
        out = index.tool_metadata_query({'ontologyId': 'o', 'question': 'q'})
    assert out['answer'] == 'OK'
    assert out['retrievedChunks'] == [{'sourceUri': 'kb://x'}]


def test_suggestions_passthrough(index, monkeypatch):
    monkeypatch.setenv(
        'SUGGESTIONS_RUNTIME_ARN',
        'arn:aws:bedrock-agentcore:us-east-1:0:runtime/s',
    )

    parsed = {'suggestions': [{'category': 'overview', 'question': 'how many?'}]}
    with patch.object(index, '_invoke_runtime_sync', return_value=parsed):
        out = index.tool_query_suggestions({'ontologyId': 'o'})
    assert out['suggestions'][0]['question'] == 'how many?'


def test_lambda_handler_routes_through_dispatcher(index, monkeypatch):
    monkeypatch.setenv(
        'SUGGESTIONS_RUNTIME_ARN',
        'arn:aws:bedrock-agentcore:us-east-1:0:runtime/s',
    )
    with patch.object(index, '_invoke_runtime_sync', return_value={'suggestions': []}):
        out = index.lambda_handler(
            {'ontologyId': 'o'}, _gateway_context('QuerySuggestions')
        )
    assert out['statusCode'] == 200
    assert json.loads(out['body']) == {'suggestions': []}


def test_runtime_failure_surfaces_500(index, monkeypatch):
    monkeypatch.setenv(
        'QUERY_RUNTIME_ARN',
        'arn:aws:bedrock-agentcore:us-east-1:0:runtime/q',
    )
    monkeypatch.delenv('GUARDRAIL_IDENTIFIER', raising=False)
    with patch.object(index, '_invoke_runtime_sync', side_effect=RuntimeError('boom')):
        out = index.lambda_handler(
            {'ontologyId': 'o', 'question': 'q'},
            _gateway_context('OntologyQuery'),
        )
    assert out['statusCode'] == 500
    assert 'boom' in json.loads(out['body'])['error']


# ---------------------------------------------------------------------------
# ListOntologies — discovery step of the caller chain (scans the metadata table)
# ---------------------------------------------------------------------------


def _fake_metadata_table(items):
    """Build a MagicMock DynamoDB Table whose scan() returns `items` (no paging).

    :param items: the list of item dicts scan() should return.
    :returns: a MagicMock that mimics the boto3 DynamoDB Table resource.
    """
    table = MagicMock()
    table.scan.return_value = {'Items': items}
    return table


def _patch_ddb(index, table):
    """Patch index.boto3.resource(...).Table(...) to return `table`."""
    ddb = MagicMock()
    ddb.Table.return_value = table
    return patch.object(index.boto3, 'resource', return_value=ddb)


def test_list_ontologies_groups_versions_and_maps_mode(index, monkeypatch):
    """ListOntologies keeps the highest version per id and maps type→mode."""
    monkeypatch.setenv('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
    items = [
        {'id': 'a', 'version': 'v1', 'name': 'VKG One', 'type': 'VKG', 'status': 'completed'},
        # v2 of the same ontology must win over v1.
        {'id': 'a', 'version': 'v2', 'name': 'VKG One', 'type': 'VKG', 'status': 'completed',
         'dataSources': [{'t': 1}, {'t': 2}]},
        {'id': 'b', 'version': 'v1', 'name': 'RAG One', 'type': 'SemanticRAG', 'status': 'completed'},
    ]
    with _patch_ddb(index, _fake_metadata_table(items)):
        out = index.lambda_handler({}, _gateway_context('ListOntologies'))
    body = json.loads(out['body'])
    assert out['statusCode'] == 200
    assert body['count'] == 2
    by_id = {o['id']: o for o in body['ontologies']}
    # Highest version + mode mapping.
    assert by_id['a']['latestVersion'] == 'v2'
    assert by_id['a']['mode'] == 'VKG'
    assert by_id['a']['dataSourceCount'] == 2
    assert by_id['b']['mode'] == 'SemanticRAG'


def test_list_ontologies_status_filter(index, monkeypatch):
    """The optional status filter excludes non-matching ontologies."""
    monkeypatch.setenv('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
    items = [
        {'id': 'a', 'version': 'v1', 'name': 'Done', 'type': 'VKG', 'status': 'completed'},
        {'id': 'b', 'version': 'v1', 'name': 'Draft', 'type': 'VKG', 'status': 'draft'},
    ]
    with _patch_ddb(index, _fake_metadata_table(items)):
        out = index.lambda_handler(
            {'status': 'completed'}, _gateway_context('ListOntologies')
        )
    body = json.loads(out['body'])
    assert body['count'] == 1
    assert body['ontologies'][0]['id'] == 'a'


def test_list_ontologies_defaults_missing_type_to_vkg(index, monkeypatch):
    """A record without a `type` defaults to VKG mode (matches REST service)."""
    monkeypatch.setenv('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
    items = [{'id': 'a', 'version': 'v1', 'name': 'Legacy', 'status': 'completed'}]
    with _patch_ddb(index, _fake_metadata_table(items)):
        out = index.lambda_handler({}, _gateway_context('ListOntologies'))
    body = json.loads(out['body'])
    assert body['ontologies'][0]['mode'] == 'VKG'
    assert body['ontologies'][0]['type'] == 'VKG'


def test_list_ontologies_errors_when_table_unset(index, monkeypatch):
    """No ONTOLOGY_METADATA_TABLE configured → a clear error, no crash."""
    monkeypatch.delenv('ONTOLOGY_METADATA_TABLE', raising=False)
    out = index.lambda_handler({}, _gateway_context('ListOntologies'))
    assert out['statusCode'] == 200  # handler wraps tool result; tool returns error key
    assert 'ONTOLOGY_METADATA_TABLE' in json.loads(out['body'])['error']


def test_list_ontologies_is_registered_in_dispatch(index):
    """ListOntologies must be routable through the Gateway dispatch table."""
    assert 'ListOntologies' in index._DISPATCH
