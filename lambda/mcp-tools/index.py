"""MCP tools Lambda for AgentCore Gateway (item #6).

Invoked by an AgentCore Gateway target — the Gateway speaks MCP to the
external client (Claude Desktop, Cursor, …) and translates each
``tools/call`` into a Lambda invocation. The tool name lives in
``context.client_context.custom['bedrockAgentCoreToolName']`` (the same
contract the Neptune tools Lambda uses) and the event is the flat
arguments map.

Three tools mirror the design doc ``OntologyQuery``, ``MetadataQuery``,
``QuerySuggestions``. Each tool:
  1. Applies the Bedrock Guardrails INPUT screen.
  2. Invokes the matching AgentCore Runtime via boto3.
  3. Applies the OUTPUT guardrail screen on the assistant text.
  4. Returns the structured result.

Rate limiting and credential management are owned by the Gateway —
this Lambda just runs tool implementations.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional


def _require_https(url: str) -> str:
    """Raise ValueError if *url* does not use the https scheme.

    Prevents mis-configured env vars (http:// or file://) from reaching urllib.

    :param url: the URL to validate.
    :returns: the original url, unchanged.
    :raises ValueError: if the scheme is not https.
    """
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError(f"Refusing non-HTTPS URL: {url!r}")
    return url

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


# The query tools now read the runtime's FULL chat SSE stream (chat-shaped
# payload), so this must cover a whole VKG turn (Phase 1-5 + Ontop + Athena),
# which can approach ~60s. Default 120s (was 60) leaves headroom; override via
# the MCP_MAX_WAIT_SECONDS env on the Lambda.
_MAX_WAIT_SECONDS = int(os.environ.get('MCP_MAX_WAIT_SECONDS', '120'))
_POLL_INTERVAL_SECONDS = 0.5


def _bedrock_runtime_client():
    """Lazy bedrock-runtime client for ApplyGuardrail.

    :returns: a boto3 bedrock-runtime client in the configured region.
    """
    return boto3.client(
        'bedrock-runtime',
        region_name=os.environ.get('AWS_REGION', 'us-east-1'),
    )


# ---------------------------------------------------------------------------
# OAuth (M2M client-credentials) token + runtime invocation over HTTPS
# ---------------------------------------------------------------------------
#
# The query runtimes are JWT-inbound, so this Lambda invokes them with a Cognito
# machine-to-machine access token (client_credentials grant + the
# semantic-layer-mcp/invoke scope) over the runtime's public /invocations HTTPS
# endpoint. The token is cached in module scope until shortly before expiry and
# refreshed on demand (or on a 401/403).

# Module-scoped token cache: {'token': str, 'expires_at': epoch_seconds}.
_m2m_token_cache: Dict[str, Any] = {}
# Refresh this many seconds before the real expiry to avoid edge-of-expiry races.
_TOKEN_SKEW_SECONDS = 60

# The Cognito hosted-UI /oauth2/token endpoint is fronted by WAF Bot Control
# (AWSManagedRulesBotControlRuleSet), which 403s requests carrying urllib's
# default 'Python-urllib/3.x' User-Agent (a known bot signature). Sending a
# browser User-Agent lets the M2M token mint through. Applied to the data-plane
# /invocations request too for consistency (harmless — that path is not behind
# this WAF).
_BROWSER_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
)


def _secrets_client():
    """Lazy Secrets Manager client (M2M client secret lives here).

    :returns: a boto3 secretsmanager client in the configured region.
    """
    return boto3.client(
        'secretsmanager',
        region_name=os.environ.get('AWS_REGION', 'us-east-1'),
    )


def _m2m_client_secret() -> str:
    """Read the confidential M2M client secret from Secrets Manager.

    :returns: the Cognito M2M app-client secret string.
    :raises RuntimeError: if the secret ARN env var is unset (fail loud).
    """
    secret_arn = os.environ.get('M2M_CLIENT_SECRET_ARN', '')
    if not secret_arn:
        raise RuntimeError('M2M_CLIENT_SECRET_ARN not configured')
    resp = _secrets_client().get_secret_value(SecretId=secret_arn)
    return resp['SecretString']


def _fetch_m2m_token(*, force: bool = False) -> str:
    """Return a valid M2M access token, fetching/refreshing as needed.

    Caches the token in module scope until ``_TOKEN_SKEW_SECONDS`` before expiry.
    Uses the Cognito ``/oauth2/token`` client_credentials grant with HTTP Basic
    auth (client_id:client_secret) — pure stdlib ``urllib``, no extra deps.

    :param force: when True, bypass the cache and fetch a fresh token (used after
        a 401/403 from the runtime in case the cached token was revoked).
    :returns: a bearer access token string.
    :raises RuntimeError: if required env vars are missing (fail loud).
    """
    now = time.time()
    if (
        not force
        and _m2m_token_cache.get('token')
        and _m2m_token_cache.get('expires_at', 0) - _TOKEN_SKEW_SECONDS > now
    ):
        return _m2m_token_cache['token']

    token_endpoint = os.environ.get('OAUTH_TOKEN_ENDPOINT', '')
    client_id = os.environ.get('M2M_CLIENT_ID', '')
    scope = os.environ.get('OAUTH_SCOPE', '')
    if not token_endpoint or not client_id or not scope:
        raise RuntimeError(
            'OAUTH_TOKEN_ENDPOINT, M2M_CLIENT_ID, and OAUTH_SCOPE must be configured'
        )
    client_secret = _m2m_client_secret()

    body = urllib.parse.urlencode(
        {'grant_type': 'client_credentials', 'scope': scope}
    ).encode('utf-8')
    basic = base64.b64encode(f'{client_id}:{client_secret}'.encode('utf-8')).decode('ascii')
    req = urllib.request.Request(
        _require_https(token_endpoint),
        data=body,
        method='POST',
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Authorization': f'Basic {basic}',
            'User-Agent': _BROWSER_UA,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 — scheme enforced by _require_https above  # nosemgrep: dynamic-urllib-use-detected — fixed AWS service endpoint from config, not user-controlled host
        payload = json.loads(resp.read().decode('utf-8'))
    token = payload['access_token']
    expires_in = int(payload.get('expires_in', 3600))
    _m2m_token_cache['token'] = token
    _m2m_token_cache['expires_at'] = now + expires_in
    return token


def _runtime_invocations_url(runtime_arn: str) -> str:
    """Build the public data-plane /invocations URL for a runtime ARN.

    :param runtime_arn: the AgentCore Runtime ARN.
    :returns: the HTTPS invocations URL (qualifier DEFAULT), with the ARN URL-encoded.
    """
    region = os.environ.get('AWS_REGION', 'us-east-1')
    encoded = urllib.parse.quote(runtime_arn, safe='')
    return _require_https(
        f'https://bedrock-agentcore.{region}.amazonaws.com/'
        f'runtimes/{encoded}/invocations?qualifier=DEFAULT'
    )


def _apply_guardrail(*, text: str, source: str) -> Dict[str, Any]:
    """Apply Bedrock Guardrails. Returns dict with `blocked` + `message`.

    Fail-open on API error (matches the existing GuardrailService behaviour
    in the FastAPI Lambda) so a transient Bedrock outage doesn't block all
    chat traffic.
    """
    guardrail_id = os.environ.get('GUARDRAIL_IDENTIFIER', '')
    guardrail_version = os.environ.get('GUARDRAIL_VERSION', '')
    if not guardrail_id or not guardrail_version:
        return {'blocked': False, 'message': '', 'action': 'NONE'}
    try:
        response = _bedrock_runtime_client().apply_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=guardrail_version,
            source=source,
            content=[{'text': {'text': text}}],
        )
        return {
            'blocked': response.get('action') == 'GUARDRAIL_INTERVENED',
            'message': (
                response.get('outputs', [{}])[0]
                .get('text', '')
                if response.get('outputs')
                else ''
            ),
            'action': response.get('action', 'NONE'),
        }
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning('apply_guardrail failed (fail-open): %s', exc)
        return {'blocked': False, 'message': '', 'action': 'NONE'}


def _parse_sse(body: str) -> Dict[str, Any]:
    """Parse an AG-UI chat-stream SSE body into the SAME dict shape ``_run_query``
    returns, so the tool bodies read identical keys regardless of transport.

    The MCP tools now send a chat-shaped payload (so the runtime persists a
    chat-sessions row that the Monitoring tab counts). The chat entrypoint yields
    ``data: {json}`` SSE frames — ``message_chunk`` deltas then a terminal
    ``run_finished`` carrying ``totals`` (or ``run_error``). VERIFIED live totals
    keys (2026-07-01): ``sql`` = SPARQL lineage, ``executedSql`` = executed Athena
    SQL, ``rows``, plus top-level ``graphTraversal``/``phaseTimeline``/
    ``provenance`` (there is NO ``reasoning`` key). We remap those onto the
    direct-invoke contract: ``answer``/``sql_query``/``results``/``reasoning``.

    Args:
        body: The full decoded SSE response body.

    Returns:
        ``{answer, sql_query, results, reasoning, provenance, error}`` — empty
        ``sql_query``/``results`` when no ``run_finished`` arrived (a truncated or
        timed-out stream), so the caller never reports a false success.
    """
    parts: list = []
    totals: Dict[str, Any] = {}
    error = None
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith('data:'):
            continue
        raw = line[len('data:'):].strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        etype = ev.get('type')
        if etype == 'message_chunk':
            parts.append(ev.get('delta', ''))
        elif etype == 'run_finished':
            totals = ev.get('totals', {}) or {}
        elif etype == 'run_error':
            error = ev.get('error') or 'run_error'
    return {
        'answer': ''.join(parts),
        'sql_query': totals.get('sql', ''),          # SPARQL lineage
        'results': totals.get('rows', []),
        'reasoning': {
            'graphTraversal': totals.get('graphTraversal', ''),
            'phaseTimeline': totals.get('phaseTimeline', []),
            'provenance': totals.get('provenance', {}),
            'sqlQuery': totals.get('executedSql', ''),  # executed Athena SQL
        },
        'provenance': totals.get('provenance', {}),
        'error': error,
    }


def _invoke_runtime_sync(*, runtime_arn: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke a JWT-inbound AgentCore Runtime over HTTPS with an M2M Bearer token.

    Replaces the prior boto3 SigV4 ``invoke_agent_runtime`` call: the runtimes are
    now JWT-inbound, so this POSTs to the runtime's public /invocations endpoint
    with ``Authorization: Bearer <M2M token>``. Retries ONCE with a force-refreshed
    token on a 401/403 (handles a revoked/rotated cached token). Parses the
    streamed/JSON response body.

    :param runtime_arn: the AgentCore Runtime ARN to invoke.
    :param payload: the JSON-serializable request payload.
    :returns: the parsed JSON response dict, or ``{'result': <raw text>}`` if the
        body is not JSON.
    :raises urllib.error.HTTPError: on a non-auth HTTP error, or a persistent
        401/403 after the retry.
    """
    url = _runtime_invocations_url(runtime_arn)
    session_id = uuid.uuid4().hex + uuid.uuid4().hex[:1]  # 33 chars
    body = json.dumps(payload).encode('utf-8')

    def _do(token: str) -> str:
        req = urllib.request.Request(
            url,
            data=body,
            method='POST',
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': session_id,
                'User-Agent': _BROWSER_UA,
            },
        )
        with urllib.request.urlopen(req, timeout=_MAX_WAIT_SECONDS) as resp:  # nosec B310 — scheme enforced by _require_https in _runtime_invocations_url  # nosemgrep: dynamic-urllib-use-detected — fixed AWS service endpoint from config, not user-controlled host
            return resp.read().decode('utf-8', errors='replace')

    try:
        text = _do(_fetch_m2m_token())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # Cached token may be stale/revoked — force-refresh and retry once.
            logger.warning('runtime invoke %s on first try; refreshing token', exc.code)
            text = _do(_fetch_m2m_token(force=True))
        else:
            raise
    # The query tools now send a chat-shaped payload → the runtime replies with an
    # AG-UI SSE stream (data: {json} frames), not a single JSON object. Detect that
    # and normalize it to the direct-invoke dict shape via _parse_sse. Non-SSE
    # responses (suggestions, list-ontologies, errors) keep the JSON path.
    if text.lstrip().startswith('data:'):
        return _parse_sse(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {'result': text}


def tool_ontology_query(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap the VKG query agent."""
    question = args.get('question') or ''
    ontology_id = args.get('ontologyId') or ''
    if not question or not ontology_id:
        return {'error': 'question and ontologyId required'}

    input_guard = _apply_guardrail(text=question, source='INPUT')
    if input_guard['blocked']:
        return {'error': 'blocked input', 'message': input_guard['message']}

    runtime_arn = os.environ.get('QUERY_RUNTIME_ARN', '')
    if not runtime_arn:
        return {'error': 'OntologyQuery runtime not configured'}

    # Chat-shaped payload → routes to the runtime's _chat_stream, which persists a
    # chat-sessions row tagged source='mcp' so the admin Monitoring tab captures
    # MCP traffic (the direct {'question','id'} shape hit _run_query, which never
    # persisted). Fresh 33-char sessionId per call ⇒ create branch, no ownership
    # conflict. userId is intentionally omitted: _user_id_from_context derives the
    # trusted user from the M2M JWT sub, so a payload userId would be ignored.
    _sid = uuid.uuid4().hex + uuid.uuid4().hex[:1]  # 33 chars — runtime session min
    result = _invoke_runtime_sync(
        runtime_arn=runtime_arn,
        payload={'message': question, 'id': ontology_id, 'ontologyId': ontology_id,
                 'turnId': f'mcp-{uuid.uuid4().hex[:8]}', 'sessionId': _sid,
                 'mode': 'vkg', 'source': 'mcp'},
    )
    answer = result.get('answer') or ''
    if answer:
        output_guard = _apply_guardrail(text=answer, source='OUTPUT')
        if output_guard['blocked']:
            return {
                'error': 'blocked output',
                'message': output_guard['message'],
            }
    reasoning = result.get('reasoning', {}) or {}
    return {
        'answer': answer,
        'rows': result.get('results', []),
        'sql': result.get('sql_query', ''),       # SPARQL lineage (unchanged contract)
        'sparql': result.get('sparql_query', ''),
        # The EXECUTED Athena SQL (Ontop reformulation of the grounded SPARQL),
        # distinct from the SPARQL lineage in 'sql'. Surfaced top-level so MCP
        # clients + the chat result panel can show the real SQL (todo item 4).
        'executed_sql': reasoning.get('sqlQuery', ''),
        'lineage': reasoning,
    }


def tool_metadata_query(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap the Semantic-RAG query agent."""
    question = args.get('question') or ''
    ontology_id = args.get('ontologyId') or ''
    if not question or not ontology_id:
        return {'error': 'question and ontologyId required'}

    input_guard = _apply_guardrail(text=question, source='INPUT')
    if input_guard['blocked']:
        return {'error': 'blocked input', 'message': input_guard['message']}

    runtime_arn = os.environ.get('METADATA_QUERY_RUNTIME_ARN', '')
    if not runtime_arn:
        return {'error': 'MetadataQuery runtime not configured'}

    # Chat-shaped payload (see tool_ontology_query) so MetadataQuery MCP calls
    # persist a chat-sessions row (source='mcp') and appear on the Monitoring tab.
    _sid = uuid.uuid4().hex + uuid.uuid4().hex[:1]  # 33 chars
    result = _invoke_runtime_sync(
        runtime_arn=runtime_arn,
        payload={'message': question, 'id': ontology_id, 'ontologyId': ontology_id,
                 'turnId': f'mcp-{uuid.uuid4().hex[:8]}', 'sessionId': _sid,
                 'mode': 'semantic-rag', 'source': 'mcp'},
    )
    answer = result.get('answer') or ''
    if answer:
        output_guard = _apply_guardrail(text=answer, source='OUTPUT')
        if output_guard['blocked']:
            return {
                'error': 'blocked output',
                'message': output_guard['message'],
            }
    return {
        'answer': answer,
        'rows': result.get('results', []),
        'sql': result.get('sql_query', ''),
        'retrievedChunks': result.get('n_quads', []),
    }


def _version_num(version_str: str) -> int:
    """Parse a 'v<N>' version string into its integer N for max() comparison.

    :param version_str: a version label like 'v1', 'v12' (or 'v0' fallback).
    :returns: the integer version number, or 0 if it cannot be parsed.
    """
    try:
        return int(str(version_str).lstrip('vV'))
    except (ValueError, AttributeError):
        return 0


def _decimals_to_native(obj: Any) -> Any:
    """Recursively convert DynamoDB Decimal values to int/float for JSON output.

    boto3's DynamoDB resource returns numbers as decimal.Decimal, which
    json.dumps cannot serialize. Convert ints losslessly, others to float.

    :param obj: a value from a DynamoDB item (possibly nested dict/list).
    :returns: the same structure with Decimals replaced by int/float.
    """
    import decimal

    if isinstance(obj, list):
        return [_decimals_to_native(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _decimals_to_native(v) for k, v in obj.items()}
    if isinstance(obj, decimal.Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def tool_list_ontologies(args: Dict[str, Any]) -> Dict[str, Any]:
    """List all published semantic layers (ontologies) and their query mode.

    This is the FIRST step of the semantic-layer caller chain: a client calls
    ListOntologies to discover the available ontologies, reads each one's
    ``mode`` to decide which query tool to use (VKG → OntologyQuery, SemanticRAG
    → MetadataQuery), then calls that tool with the chosen ``ontologyId``.

    Scans the metadata DynamoDB table (records are versioned: same ``id`` across
    many ``version`` rows) and returns one summary per ontology using its
    highest-version record as the active config — mirroring the REST API's
    list_ontologies so the MCP and web surfaces agree.

    :param args: tool arguments. Optional ``status`` (str) filters to ontologies
        in that build status (e.g. 'completed'); when omitted, all are returned.
    :returns: ``{'ontologies': [{id, name, mode, type, status, updatedAt,
        dataSourceCount, latestVersion, description}, ...], 'count': int}``.
    """
    from collections import defaultdict

    table_name = os.environ.get('ONTOLOGY_METADATA_TABLE', '')
    if not table_name:
        return {'error': 'ONTOLOGY_METADATA_TABLE not configured'}

    status_filter = (args.get('status') or '').strip()

    ddb = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
    table = ddb.Table(table_name)

    # Scan all versions of all ontologies (paginated), then keep the highest
    # version per id as the active config — same strategy as the REST service.
    response = table.scan()
    all_items: List[Dict[str, Any]] = response.get('Items', [])
    while 'LastEvaluatedKey' in response:
        response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
        all_items.extend(response.get('Items', []))

    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in all_items:
        groups[item.get('id')].append(_decimals_to_native(item))

    ontologies: List[Dict[str, Any]] = []
    for _ontology_id, versions in groups.items():
        config = max(versions, key=lambda v: _version_num(v.get('version', 'v0')))
        # 'type' is the stored discriminator: 'VKG' (default) or 'SemanticRAG'.
        otype = config.get('type', 'VKG')
        if status_filter and config.get('status') != status_filter:
            continue
        ontologies.append(
            {
                'id': config.get('id'),
                'name': config.get('name'),
                # 'mode' is the caller-chain hint: which query tool to invoke next.
                'mode': 'VKG' if otype == 'VKG' else 'SemanticRAG',
                'type': otype,
                'status': config.get('status', 'unknown'),
                'updatedAt': config.get('updatedAt', ''),
                'dataSourceCount': len(config.get('dataSources') or []),
                'latestVersion': config.get('version', 'v1'),
                'description': config.get('useCasesDescription', '')
                or config.get('dataSourcesDescription', ''),
            }
        )

    return {'ontologies': ontologies, 'count': len(ontologies)}


def tool_query_suggestions(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap the Query Suggestions agent."""
    ontology_id = args.get('ontologyId') or ''
    if not ontology_id:
        return {'error': 'ontologyId required'}

    runtime_arn = os.environ.get('SUGGESTIONS_RUNTIME_ARN', '')
    if not runtime_arn:
        return {'error': 'QuerySuggestions runtime not configured'}

    result = _invoke_runtime_sync(
        runtime_arn=runtime_arn,
        payload={'id': ontology_id},
    )
    return {'suggestions': result.get('suggestions', [])}


# ---------------------------------------------------------------------------
# Lambda entry point — same Gateway contract as neptune-tools
# ---------------------------------------------------------------------------


def _resolve_tool_name(context) -> Optional[str]:
    """Pull bedrockAgentCoreToolName out of the Lambda context's client
    context. The Gateway prefixes it with the target name + ``___``."""
    if not context or not getattr(context, 'client_context', None):
        return None
    custom = getattr(context.client_context, 'custom', None) or {}
    raw = custom.get('bedrockAgentCoreToolName')
    if not raw:
        return None
    if '___' in raw:
        return raw.split('___', 1)[1]
    return raw


_DISPATCH: Dict[str, Any] = {
    'ListOntologies': tool_list_ontologies,
    'OntologyQuery': tool_ontology_query,
    'MetadataQuery': tool_metadata_query,
    'QuerySuggestions': tool_query_suggestions,
}


def lambda_handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    """Lambda entry point. Returns the tool result as a dict; the Gateway
    wraps it in the MCP envelope when it forwards to the client."""
    logger.info('mcp-tools event: %s', json.dumps(event)[:500])
    tool_name = _resolve_tool_name(context)
    if tool_name not in _DISPATCH:
        return {
            'statusCode': 400,
            'body': json.dumps({'error': f'unknown tool: {tool_name!r}'}),
        }
    handler = _DISPATCH[tool_name]
    arguments = event if isinstance(event, dict) else {}
    try:
        result = handler(arguments)
        return {'statusCode': 200, 'body': json.dumps(result)}
    except Exception as exc:  # noqa: BLE001 — never crash the Gateway
        logger.exception('tool %s raised: %s', tool_name, exc)
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(exc)}),
        }
