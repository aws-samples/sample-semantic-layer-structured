"""Hybrid semantic-layer query agent runtime entrypoint.

Assembles the query agent's runtime shell around the Tier-2 Hybrid resolution
graph (``agents.semantic_layer_query_agent.graph.run_hybrid_workflow``):

* Tier-1 governed-metric lookup (cosine >= 0.85 early-return).
* The governed-rule tier (flag-gated on ``GOVERNED_RULES_ENABLED``): a matched
  PUBLISHED rule is either executed via Ontop over the layer's thin VKG, or —
  when an input or an applied threshold is unresolved — answered advisorily with
  the gap named. Never executes a rule whose bounds nobody approved.
* Tier-2 Hybrid graph (KB recall + thin-VKG ontology-verified grounding).
* AG-UI streaming chat dispatch, chat-session persistence, memory hooks, and
  Bedrock Guardrails INPUT screening.
* The ``@app.entrypoint`` ``invoke`` that ties Tier-1 + Tier-2 together.

Hybrid artifact guard (the design's fail-loud rule): before answering, the
entrypoint asserts BOTH hybrid artifacts exist — the Bedrock Knowledge Base
(KB) AND the thin virtual-knowledge-graph (VKG) in Neptune. If EITHER is
missing, ``_resolve`` returns a ``hybrid_artifact_missing`` degraded result. It
never silently falls back to a single-engine (KB-only or VKG-only) answer,
because a hybrid answer that skips ontology verification is a silently weaker
answer.

Isolation note (``tests/unit/test_hybrid_isolation.py``): this module imports
ONLY from within ``agents.semantic_layer_query_agent``, ``strands``, and
``bedrock_agentcore`` — never ``agents.shared`` or a sibling agent. All heavy
AWS / Bedrock / Strands calls live behind injectable seams so ``_resolve`` (and
the wiring around it) is unit-testable under the conftest stubs without a live
runtime.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional

import boto3

try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp
except ImportError as exc:  # pragma: no cover — surfaced at container startup
    import sys

    print(f"STARTUP ERROR: failed to import BedrockAgentCoreApp: {exc}", flush=True)
    sys.exit(1)

from strands import Agent
from strands.models import BedrockModel

from .advisory import build_advisory_answer
from .agui_emitter import AGUIEmitter
from .answer_span import emit_answer_span
from .chat_sessions import ChatSessionService, SessionOwnershipError
# ``execution_agent`` imports no AWS at module load (boto3 is lazy inside
# ``athena_run_query``), so pulling ROW_CAP here keeps the renderer's truncation
# note in step with the executor's real cap instead of restating the number.
from .execution_agent import ROW_CAP
# Vendored per the copy-and-own rule (see firewall_context's module docstring);
# the canonical module is agents/shared/firewall_context.py.
from . import firewall_context as _firewall_context
# Document terminal. Byte-identical copies of agents/shared/document_answer.py
# and document_retrieval.py — copied, not imported, because this agent's
# container ships only its own package (tests/unit/test_hybrid_isolation.py
# enforces that). The call site must stay identical to the other two agents'.
from .document_answer import DocumentAnswer, try_document_answer
from .graph import HybridDeps, run_hybrid_workflow
from .guardrail_service_shim import GuardrailService as _MemoryGuardrailService
from .guardrails import GuardrailService
from .memory_hooks import persist_turn_pair
from .provenance import build_provenance
from .query_prompts import ANSWER_RENDER_PROMPT
# The governed-rule tier's collaborators. Bound at module scope (not imported
# inside the tier) so unit tests can replace each seam by name; none of these
# modules touches AWS at import time.
from .rule_executor import build_rule_answer, unapproved_threshold_ids
from .rule_executor import execute_rule as rule_execute_fn
from .rule_lookup import lookup as rule_lookup_fn
from .slice_builder import SliceBuilder
from .sql_generator import SqlGenerator
from .topic_router import TopicRouter

# ---------------------------------------------------------------------------
# Logging.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# AgentCore app instance — decorates ``invoke`` below.
app = BedrockAgentCoreApp()

region = os.getenv('AWS_REGION', 'us-east-1')

# ---------------------------------------------------------------------------
# Model ids. Every literal here MUST appear in ``models.json`` (enforced by
# tests/unit/test_model_manifests.py). The embedding model id lives in
# embedding.py (also declared in the manifest).
# ---------------------------------------------------------------------------
# Generation / judge model (Sonnet 5 — adaptive thinking on by default, so
# temperature is intentionally omitted when building the BedrockModel).
QUERY_MODEL_ID: str = 'global.anthropic.claude-sonnet-5'
# Cheap intent/router model.
ROUTER_MODEL_ID: str = 'global.anthropic.claude-haiku-4-5-20251001-v1:0'

# Serialized-slice token budget for the Phase-3 slice builder.
SLICE_TOKEN_BUDGET: int = 12000

# Phase-1 KNN recall breadth.
TOPIC_ROUTER_K: int = 20

# ---------------------------------------------------------------------------
# DynamoDB handles.
# ---------------------------------------------------------------------------
_metadata_table_name = os.getenv('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
_dynamodb = boto3.resource('dynamodb', region_name=region)


# ---------------------------------------------------------------------------
# Boto session seam — injectable so the runtime can push scoped credentials and
# unit tests stay hermetic.
# ---------------------------------------------------------------------------
_boto_session: Optional[boto3.Session] = None


def set_boto_session(session: boto3.Session) -> None:
    """Set the boto3 session used for all AWS API calls in this runtime.

    Args:
        session: A configured ``boto3.Session`` with the desired credentials.

    Returns:
        None.
    """
    global _boto_session
    _boto_session = session
    logger.info("Boto3 session set with region: %s", session.region_name)


def get_boto_session() -> boto3.Session:
    """Return the configured boto3 session, creating a default one if unset.

    Returns:
        The active ``boto3.Session``.
    """
    global _boto_session
    if _boto_session is None:
        _boto_session = boto3.Session()
    return _boto_session


# ---------------------------------------------------------------------------
# Metric / metadata table accessors (seams).
# ---------------------------------------------------------------------------
def metrics_table():
    """Return the boto3 Table resource for the governed-metrics catalog."""
    return _dynamodb.Table(os.environ.get("METRICS_TABLE", "semantic-layer-metrics"))


def metadata_table():
    """Return the boto3 Table resource for the semantic-layer metadata catalog."""
    return _dynamodb.Table(_metadata_table_name)


def _athena_client():
    """Return an Athena client honoring the injected boto session."""
    return get_boto_session().client("athena", region_name=region)


def get_latest_metadata_item(id: str) -> Optional[dict]:
    """Return the metadata item with the highest numeric version for ``id``.

    Versions are stored as ``'v1'``, ``'v2'``, … The sort key is a string, so we
    fetch all versions and pick the one whose numeric suffix is largest (handles
    v10, v11, … correctly, unlike a raw lexicographic sort).

    Args:
        id: The semantic-layer id (partition key on the metadata table).

    Returns:
        The latest-version item dict, or ``None`` when the id has no rows.
    """
    from boto3.dynamodb.conditions import Key

    resp = metadata_table().query(KeyConditionExpression=Key('id').eq(id))
    items = resp.get('Items', [])
    if not items:
        return None

    def _version_num(item: dict) -> int:
        """Parse the numeric part of a ``'v<N>'`` version string (0 on error)."""
        try:
            return int(item.get('version', 'v0').lstrip('v'))
        except ValueError:
            return 0

    return max(items, key=_version_num)


def _catalog_database_for_layer(config: dict) -> tuple[str, str]:
    """Return the ``(catalog_id, database_name)`` a layer's tables live in.

    The Tier-1 governed-metric executor needs the SAME Athena catalog/database
    the Tier-2 path uses: for an S3 Tables layer the schema (e.g. ``normalized``)
    only resolves when the federated catalog is named in the execution context.
    A layer's tables are all ingested into one catalog+database, so the first
    ``dataSources`` entry is representative.

    Args:
        config: A metadata-layer config item (from ``get_latest_metadata_item``).

    Returns:
        ``(catalog_id, database_name)``; ``("", "")`` when the config carries no
        ``dataSources`` (the executor then falls back to the default Glue
        catalog).
    """
    data_sources = config.get("dataSources") or []
    if not data_sources:
        return "", ""
    first = data_sources[0] or {}
    catalog_id = first.get("catalogId") or first.get("catalog_id") or ""
    database_name = first.get("databaseName") or first.get("database_name") or ""
    return catalog_id, database_name


# ---------------------------------------------------------------------------
# Bedrock model builders (seams — never called at import).
# ---------------------------------------------------------------------------
def _build_query_model() -> BedrockModel:
    """Build the BedrockModel used by the Tier-2 SQL generator + execution agent.

    ``temperature`` is intentionally omitted — Sonnet 5 has adaptive thinking on
    by default and Bedrock rejects ``temperature``/``top_p``/``top_k`` when
    thinking is active. ``max_tokens`` is generous because adaptive-thinking
    tokens share the OUTPUT budget.

    Returns:
        A configured ``BedrockModel``.
    """
    return BedrockModel(
        model_id=QUERY_MODEL_ID,
        max_tokens=8000,
        boto_session=get_boto_session(),
    )


def _build_judge_model() -> BedrockModel:
    """Build the BedrockModel used by the Phase-3 slice-sufficiency judge.

    Returns:
        A configured ``BedrockModel`` (same model id as the generator; smaller
        output budget since the judge emits a small structured verdict).
    """
    return BedrockModel(
        model_id=QUERY_MODEL_ID,
        max_tokens=4000,
        boto_session=get_boto_session(),
    )


# ---------------------------------------------------------------------------
# Guardrail + chat-session singletons.
# ---------------------------------------------------------------------------
_guardrails = GuardrailService()          # fail-open, chat INPUT/OUTPUT screen
_chat_sessions = ChatSessionService()
_memory_guardrail = _MemoryGuardrailService()  # fail-closed, PII-redact memory


def _user_id_from_context(context, payload) -> str:
    """Return the Cognito subject from the forwarded Bearer JWT.

    AgentCore already validated the token, so we decode the claims without
    re-verifying. Falls back to ``payload['userId']`` when no Bearer token is
    present.

    Args:
        context: AgentCore invocation context (may carry ``request_headers``).
        payload: The chat request payload (used for the ``userId`` fallback).

    Returns:
        The Cognito subject id, the payload ``userId``, or an empty string.
    """
    import base64

    headers = (getattr(context, 'request_headers', None) or {}) if context else {}
    auth = headers.get('Authorization') or headers.get('authorization') or ''
    if auth.startswith('Bearer '):
        try:
            seg = auth[len('Bearer '):].split('.')[1]
            # Re-pad the base64url segment so urlsafe_b64decode accepts it.
            seg += '=' * (-len(seg) % 4)
            return json.loads(base64.urlsafe_b64decode(seg)).get('sub', '') or ''
        except Exception:  # noqa: BLE001 — malformed token must not break the turn
            pass
    return payload.get('userId', '') or ''


# ---------------------------------------------------------------------------
# Tier-1 governed-metric response shaping.
# ---------------------------------------------------------------------------
def _summarize_metric_rows(*, metric_id: str, columns: List[str],
                           rows: List[list]) -> str:
    """Build a natural-language answer from a governed-metric result.

    Shapes a useful sentence from the result itself (scalar / single record /
    multi-row) so the user sees the value(s) rather than a bare row/column count.

    Args:
        metric_id: The governed metric id (for the multi-row fallback label).
        columns: Result column names.
        rows: Result rows, each a positional list aligned to ``columns``.

    Returns:
        A concise plain-English answer string.
    """
    if not rows:
        return f"The governed metric '{metric_id}' returned no results."
    if len(rows) == 1:
        row = rows[0]
        if len(columns) == 1 and len(row) == 1:
            return f"The result is {row[0]}."
        pairs = ", ".join(
            f"{col}: {row[i] if i < len(row) else ''}"
            for i, col in enumerate(columns)
        )
        return f"The governed metric '{metric_id}' returned one result — {pairs}."
    return (
        f"The governed metric '{metric_id}' returned {len(rows)} rows across "
        f"{len(columns)} column(s). See the result table for details."
    )


def _metric_rows_to_positional(*, columns: List[str],
                               rows: List[Any]) -> List[list]:
    """Normalize ``execute_metric``'s dict-rows to positional lists for the UI.

    The chat UI renders rows POSITIONALLY (``row[idx]`` by column order), while
    ``execute_metric`` returns rows as ``[{col: val}, ...]``. A row already a
    list/tuple is passed through.

    Args:
        columns: Ordered column names.
        rows: Rows as dicts (or already-positional lists).

    Returns:
        Rows as positional lists aligned to ``columns``.
    """
    out: List[list] = []
    for r in rows:
        if isinstance(r, dict):
            out.append([r.get(c, "") for c in columns])
        elif isinstance(r, (list, tuple)):
            out.append(list(r))
        else:
            out.append([r])
    return out


def _build_response_from_metric(*, metric, result: Dict[str, Any],
                                id: str) -> Dict[str, Any]:
    """Shape a Tier-1 metric result into the same payload shape as a Tier-2 answer.

    Args:
        metric: The matched ``Metric`` object.
        result: The ``execute_metric`` result dict (``columns`` / ``rows``).
        id: The semantic-layer id (echoed into metadata).

    Returns:
        The normalized answer dict (answer / sql_query / results / columns /
        reasoning / metadata / provenance).
    """
    cols = result.get("columns", []) or []
    pos_rows = _metric_rows_to_positional(columns=cols, rows=result.get("rows", []))
    sql = getattr(metric, "compiled_sql", "")
    answer = _summarize_metric_rows(
        metric_id=metric.metric_id, columns=cols, rows=pos_rows,
    )
    return {
        "answer": answer,
        "sql_query": sql,
        "executed_sql": sql,
        "executed": True,
        "degraded": None,
        "results": pos_rows,
        "columns": cols,
        "n_quads": [],
        "reasoning": {
            "interpretation": f"Tier 1 governed-metric match: {metric.metric_id}",
            "graphTraversal": "governed metric",
            "dataSourceSelection": "Athena (governed metric)",
            "sqlQuery": sql,
            "summarization": answer,
        },
        "metadata": {"tier": 1, "metric_id": metric.metric_id, "ontology_id": id},
        "provenance": build_provenance(
            tier="governed_metric",
            sources=[f"metric:{metric.metric_id}"],
        ),
    }


# ---------------------------------------------------------------------------
# Tier-2 Hybrid seams: KB retrieval, KNN recall, few-shots, Neptune CONSTRUCT.
# ---------------------------------------------------------------------------
def _kb_id_for(config: dict) -> str:
    """Return the Bedrock KB id for a layer config (env fallback).

    Args:
        config: The metadata-layer config item.

    Returns:
        The KB id, or an empty string when neither the config nor the env
        supplies one (which the artifact guard treats as a missing KB artifact).
    """
    return config.get('kbId') or os.environ.get('SEMANTIC_RAG_KB_ID', '')


def _bedrock_agent_runtime():
    """Return a bedrock-agent-runtime client honoring the injected boto session."""
    return get_boto_session().client('bedrock-agent-runtime', region_name=region)


def _kb_few_shots(question: str, namespace: str, *, kb_id: str) -> List[Dict[str, Any]]:
    """Retrieve few-shot exemplars (sample-SQL / glossary chunks) for a question.

    Wraps the Bedrock KB ``retrieve`` call so the Phase-3 slice builder can inject
    proven query exemplars into the slice. Fail-soft: any retrieval error yields
    an empty list (few-shots are an enrichment, not a correctness requirement).

    Args:
        question: The natural-language user question.
        namespace: The semantic-layer namespace (unused here — scoping lives in
            the KB filter, kept for the SliceBuilder ``kb_retrieve`` protocol).
        kb_id: The Bedrock Knowledge Base id.

    Returns:
        A list of ``{"content": <chunk-text>}`` exemplar dicts.
    """
    if not kb_id:
        return []
    try:
        resp = _bedrock_agent_runtime().retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={'text': question},
            retrievalConfiguration={
                'vectorSearchConfiguration': {'numberOfResults': 5},
            },
        )
    except Exception as exc:  # noqa: BLE001 — few-shots are best-effort
        logger.warning("kb few-shot retrieve failed (non-fatal): %s", exc)
        return []
    out: List[Dict[str, Any]] = []
    for r in resp.get('retrievalResults', []):
        body = r.get('content', {}).get('text', '')
        if body:
            out.append({"content": body})
    return out


def _topic_router_index(namespace: str) -> str:
    """Return the per-namespace KNN topic-router index name."""
    return f"topic-router-{namespace}"


def _hydrate_topic_router(*, namespace: str, layer_id: str, version: str) -> int:
    """Populate the namespace's ``topic-router-<ns>`` index from the KB corpus.

    The index is in-memory and per-container, so a freshly started runtime has
    none: something must build it before the first search or every query fails.
    Recall targets TABLES, so the source is the layer's KB markdown corpus in S3
    (see :mod:`kb_table_index` for why the thin VKG cannot serve this) — the KB
    keys carry the ``{database}.{table}`` ids Phase 1 needs and the prose gives
    the recall signal.

    Idempotent: ``knn_hydration`` tracks hydrated namespaces, so only the first
    call per namespace per container pays the embedding cost.

    Args:
        namespace: The semantic-layer namespace whose index to build.
        layer_id: The layer id scoping the KB corpus in S3.
        version: The layer version scoping the KB corpus in S3.

    Returns:
        The number of tables indexed (0 when already hydrated).

    Raises:
        RuntimeError: ``ARTIFACTS_BUCKET`` is unset, or the layer has no KB
            documents — either way the index cannot be built, and a silently
            empty index would make every question abstain for a reason unrelated
            to the question.
    """
    from . import kb_table_index, knn_hydration

    bucket = os.environ.get('ARTIFACTS_BUCKET', '')
    if not bucket:
        raise RuntimeError(
            "ARTIFACTS_BUCKET is not configured — cannot read the KB corpus that "
            "the Phase-1 topic-router index is built from."
        )
    list_keys, get_text = kb_table_index.build_s3_seams(
        bucket=bucket,
        s3_client=get_boto_session().client('s3', region_name=region),
    )
    fetch = kb_table_index.build_table_metadata_fetcher(
        layer_id=layer_id, version=version, bucket=bucket,
        list_keys=list_keys, get_text=get_text,
    )
    return knn_hydration.hydrate_topic_router_namespace(
        namespace=namespace, fetch_iri_metadata=fetch,
    )


def _build_chunks_lookup(*, layer_id: str, version: str) -> Callable[..., Dict[str, str]]:
    """Build the Phase-3 slice builder's KB-markdown chunk source.

    Reads the SAME S3 corpus the topic-router hydrates from, but returns whole
    documents: the slice builder parses the Columns table / Referenced-By joins /
    ACORD path out of them, which :func:`kb_table_index.recall_text` strips.

    Args:
        layer_id: The layer id scoping the KB corpus in S3.
        version: The layer version scoping that corpus.

    Returns:
        The ``chunks_lookup(table_ids, namespace)`` callable.

    Raises:
        RuntimeError: ``ARTIFACTS_BUCKET`` is unset. A slice with no columns
            silently degrades every question to ``phase3_max_rounds``, so fail
            loud rather than build a schema-less builder.
    """
    from . import kb_table_index

    bucket = os.environ.get('ARTIFACTS_BUCKET', '')
    if not bucket:
        raise RuntimeError(
            "ARTIFACTS_BUCKET is not configured — cannot read the KB corpus the "
            "Phase-3 slice's columns and joins are parsed from."
        )
    list_keys, get_text = kb_table_index.build_s3_seams(
        bucket=bucket,
        s3_client=get_boto_session().client('s3', region_name=region),
    )
    return kb_table_index.build_chunks_lookup(
        layer_id=layer_id, version=version,
        list_keys=list_keys, get_text=get_text,
    )


def _build_topic_router(namespace: str, *, layer_id: str = "",
                        version: str = "v1") -> TopicRouter:
    """Build the Phase-1 KNN topic router for a namespace.

    Wires the embedding seam (Titan v2) and the in-memory KNN search over the
    namespace's ``topic-router-<namespace>`` index. Both are within-package
    modules copied under the isolation rule.

    The index lives in process memory, so it must be hydrated before the first
    search — hydration happens lazily inside the search closure (mirroring
    ``ontology_query_agent.tier2.vkg_topic_router``) rather than eagerly here, so
    building the router stays cheap and a namespace that is never queried never
    pays for an index.

    Args:
        namespace: The semantic-layer namespace whose recall index to search.
        layer_id: The layer id scoping the KB corpus hydration reads. Defaults to
            ``namespace`` when empty (they coincide unless a layer overrides its
            namespace).
        version: The layer version scoping that corpus.

    Returns:
        A configured :class:`TopicRouter`.
    """
    from . import knn_index
    from .embedding import embed_text

    index_name = _topic_router_index(namespace)
    corpus_layer_id = layer_id or namespace

    def _knn_search(*, vector: List[float], k: int) -> List[Dict[str, Any]]:
        """Search the namespace index, hydrating it first if this is a cold start.

        Hydration is attempted BEFORE the search (it is a no-op once done) so a
        cold container serves its first question correctly. ``IndexNotFoundError``
        is still caught around the search itself because hydration raising is not
        the only way the index can be absent — a hydration that legitimately
        indexed nothing would also leave it missing.
        """
        _hydrate_topic_router(
            namespace=namespace, layer_id=corpus_layer_id, version=version,
        )
        try:
            return knn_index.knn_search(
                endpoint="", index=index_name, vector=vector, k=k,
            )
        except knn_index.IndexNotFoundError:
            # Recall found nothing to search. Return empty rather than raising:
            # Phase 1 turns an empty candidate list into the explicit
            # ``phase1_empty`` degrade, which is a truthful "could not ground"
            # answer, whereas an exception here aborts the whole invocation.
            logger.warning(
                "topic-router index %s absent after hydration — recall empty",
                index_name,
            )
            return []

    return TopicRouter(knn_search=_knn_search, embed=embed_text, k=TOPIC_ROUTER_K)


def _build_slice_judge() -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """Build the Phase-3 slice-sufficiency judge callable.

    The judge asks the query model whether a serialized slice answers the
    question and returns ``{"sufficient": bool, "missing": [...], "usage": {}}``.
    Fail-soft: a model/parse error is treated as "sufficient" so a judge outage
    never blocks a query (the ontology grounding gate in Phase 5 remains the hard
    backstop).

    Returns:
        A callable ``judge({"slice", "question"}) -> verdict dict``.
    """
    def _judge(payload: Dict[str, Any]) -> Dict[str, Any]:
        prompt = (
            "You are a schema-slice sufficiency judge. Given a JSON schema slice "
            "and a question, decide whether the slice contains every table and "
            "column needed to answer it. Reply ONLY with JSON: "
            '{"sufficient": true|false, "missing": ["db.table", ...]}.\n\n'
            f"# Slice\n{payload.get('slice', '')}\n\n"
            f"# Question\n{payload.get('question', '')}"
        )
        try:
            agent = Agent(model=_build_judge_model(),
                          system_prompt="Emit only the JSON verdict.")
            response = agent(prompt)
            text = response.message['content'][0]['text'].strip()
            if text.startswith('```'):
                text = text.strip('`')
                if text.startswith('json'):
                    text = text[4:]
            verdict = json.loads(text)
            return {
                "sufficient": bool(verdict.get("sufficient", True)),
                "missing": verdict.get("missing") or [],
                "usage": {},
            }
        except Exception as exc:  # noqa: BLE001 — fail-soft to "sufficient"
            logger.warning("slice judge failed (non-fatal, treating as "
                           "sufficient): %s", exc)
            return {"sufficient": True, "missing": [], "usage": {}}

    return _judge


def _build_sql_generator() -> SqlGenerator:
    """Build the Phase-4 NL→SQL generator wired to the Bedrock query model.

    The generator reaches the model through an injected ``invoke_model`` seam
    that returns ``(sql_text, usage)`` so the workflow can account tokens.

    Returns:
        A configured :class:`SqlGenerator`.
    """
    def _invoke_model(prompt: str, **_kwargs: Any):
        """Send ``prompt`` to the query model; return ``(sql_text, usage)``."""
        agent = Agent(model=_build_query_model(),
                      system_prompt="Emit only an Athena SQL SELECT query.",
                      tools=[])
        response = agent(prompt)
        text = response.message['content'][0]['text']
        usage: Dict[str, int] = {}
        try:
            acc = response.metrics.accumulated_usage
            for key in ('inputTokens', 'outputTokens', 'totalTokens'):
                value = acc.get(key) if isinstance(acc, dict) else getattr(acc, key, None)
                if value is not None:
                    usage[key] = int(value)
        except AttributeError:
            usage = {}
        return text, usage

    return SqlGenerator(invoke_model=_invoke_model)


def _neptune_construct_ttl(*, namespace: str, candidates: List[str]) -> str:
    """Fetch the thin-VKG structural slice for ``candidates`` as Turtle text.

    This is the Phase-5 CONSTRUCT seam wired into
    :class:`agents.semantic_layer_query_agent.neptune_fetch.StructuralGraphFetcher`.
    It reaches Neptune ONLY through the AgentCore Neptune Gateway (this runtime has
    no direct SigV4/VPC path), using the gateway's ``execute_sparql_query``
    CONSTRUCT branch. FAILS LOUD (raises) on any error — Phase 5 turns a raised
    error into a ``hybrid_verify_unavailable`` degrade rather than a silent
    single-engine answer.

    Args:
        namespace: The ontology namespace to fetch the structural slice for.
        candidates: The candidate ``{database}.{table}`` ids whose structural
            neighbourhood the slice must cover.

    Returns:
        The structural slice serialized as Turtle text.

    Raises:
        RuntimeError: When no gateway is configured, or the CONSTRUCT call
            fails — the caller must surface this as a hard degrade.
        ValueError: When ``candidates`` is empty (an unconstrained CONSTRUCT would
            pull the entire graph).
    """
    gateway_url = os.environ.get('NEPTUNE_GATEWAY_URL', '')
    if not gateway_url:
        # No gateway wired — fail loud so Phase 5 degrades to
        # hybrid_verify_unavailable instead of answering from the KB alone.
        raise RuntimeError(
            "NEPTUNE_GATEWAY_URL is not configured — cannot fetch the thin-VKG "
            "structural slice required for hybrid grounding"
        )
    # Import lazily so this module still imports under the conftest stubs, which
    # provide no strands/mcp transport.
    from .neptune_gateway import (
        NeptuneGatewayClient,
        build_gateway_client,
        build_structural_construct,
    )

    sparql = build_structural_construct(namespace=namespace, candidates=candidates)
    mcp_client = build_gateway_client(gateway_url=gateway_url, region=region)
    # The MCP session must be open for the duration of the call; open it per fetch
    # because StructuralGraphFetcher caches the resulting Graph, so this runs at
    # most once per (namespace, candidate-set) within a request.
    with mcp_client:
        turtle = NeptuneGatewayClient(mcp_client=mcp_client).construct(sparql=sparql)
    logger.info(
        "thin-VKG slice fetched: namespace=%s candidates=%d turtle_bytes=%d",
        namespace, len(candidates), len(turtle.encode('utf-8')),
    )
    return turtle


def _vocab_for(config: dict) -> str:
    """Return the VKG vocabulary namespace prefix for a layer.

    The prefix (including its trailing ``#`` or ``/``) is what
    ``build_inverse_index`` reads ``mapsToTable`` / ``mapsToColumn`` terms
    against. Sourced from the layer config with an env fallback.

    Args:
        config: The metadata-layer config item.

    Returns:
        The vocabulary namespace prefix string.
    """
    # Default to the SHARED VKG vocabulary the creation agent actually writes
    # (ontop_mappings.VIRTUAL_KG_VOCAB). The previous default
    # ("https://semantic-layer.local/vkg#") matched nothing the builder emits, so
    # build_inverse_index returned an empty index and every hybrid query abstained
    # as ungrounded. Config/env may still override per layer.
    from .ontop_mappings import VIRTUAL_KG_VOCAB

    return (
        config.get('vkgVocab')
        or os.environ.get('VKG_VOCAB')
        or VIRTUAL_KG_VOCAB
    )


# ---------------------------------------------------------------------------
# Tier-2 execution seam.
# ---------------------------------------------------------------------------
def _render_answer(
    *,
    question: str,
    columns: List[str],
    rows: List[list],
    over_limit: bool,
    usage_sink: Optional[Dict[str, int]] = None,
    slice_text: str = "",
) -> str:
    """Render the user-facing NL answer from the Athena result via a bounded LLM.

    Hybrid Phase 5 is otherwise deterministic (bounded Athena executor, no LLM),
    which cost two things at once: the answer was a row-count stub that hid a
    correct result from the user, and — because no model span was emitted — the
    SESSION FinalAnswerFaithfulness / GoalSuccess judges had no answer-like span
    to score. This is a real Bedrock call whose OUTPUT is the answer, so the
    Strands SDK auto-instruments an in-graph ``chat`` span (real model id + usage,
    NL output) the eval harvester captures. Mirrors the VKG agent's equivalent
    renderer.

    Args:
        question: The natural-language user question.
        columns: Result column names.
        rows: Result rows as positional lists aligned to ``columns``. A bounded
            sample goes in the prompt; the UI renders the full table separately.
        over_limit: True when the result was truncated to the display cap.
        usage_sink: Optional accumulator; this call's real (billed) Bedrock token
            usage is folded in so Phase-5 telemetry counts the renderer.
        slice_text: The Phase-3 slice the SQL was grounded in. Carried in the
            prompt as a ``[retrieved_schema_context]`` block so this span — which
            anchors the SESSION context — lets the ``SqlGrounded`` judge verify
            the executed SQL against the schema it was allowed to use.

    Returns:
        The NL answer string.

    Raises:
        Exception: Propagated to the caller. The fail-soft fallback lives in
            ``BoundedSqlExecutor._render`` (single place, so the deterministic
            summary is not duplicated here).
    """
    agent = Agent(model=_build_query_model(),
                  system_prompt=ANSWER_RENDER_PROMPT, tools=[])
    # Cap the rows placed in the prompt so a large result never blows the context.
    sample = rows[:20]
    trunc_note = f" (result truncated to the first {ROW_CAP} rows)" if over_limit else ""
    schema_block = (
        f"[retrieved_schema_context]\n{slice_text}\n[/retrieved_schema_context]\n\n"
        if slice_text else ""
    )
    prompt = (
        f"{schema_block}"
        f"[question]\n{question}\n[/question]\n\n"
        f"The query has already run. Result columns: {columns}\n"
        f"Result rows ({len(rows)} total{trunc_note}; up to 20 shown):\n"
        f"{json.dumps(sample, default=str)}\n\n"
        "Report the answer to the question in 1-2 sentences."
    )
    response = agent(prompt)
    if usage_sink is not None:
        try:
            acc = response.metrics.accumulated_usage
            for key in ('inputTokens', 'outputTokens', 'totalTokens'):
                value = acc.get(key) if isinstance(acc, dict) else getattr(acc, key, None)
                if value is not None:
                    usage_sink[key] = usage_sink.get(key, 0) + int(value)
        except AttributeError:
            # A model shim without ``.metrics`` (unit tests) reports no usage;
            # the answer itself is unaffected.
            pass
    return response.message['content'][0]['text']


def _run_tier2_execution(ctx) -> Dict[str, Any]:
    """Phase-5 execution: run the grounded SQL through the bounded executor.

    Wires the deterministic row-cap helpers, the Athena run-query seam, and the
    bounded answer renderer so the grounded SQL executes with a ``LIMIT``, gets
    over-limit detection, and produces a real natural-language answer. The
    ``BoundedSqlExecutor`` keeps all boto3/Athena and Bedrock access behind its
    injected seams.

    Args:
        ctx: The :class:`WorkflowContext` (carries ``sql`` / ``database_name`` /
            ``catalog_id`` / ``question`` / ``slice_text``).

    Returns:
        The parsed result dict (``columns``/``rows``/``answer``/``usage``/
        ``over_limit``/``total_row_count``).
    """
    from .execution_agent import BoundedSqlExecutor, athena_run_query

    output_loc = os.environ.get('ATHENA_OUTPUT_LOCATION', '')
    workgroup = os.environ.get('ATHENA_WORKGROUP', 'primary')

    def _run_query(*, sql: str, database_name: str, catalog_id: str) -> Dict[str, Any]:
        """Execute one grounded SELECT against Athena via the default seam."""
        return athena_run_query(
            sql=sql, database_name=database_name, catalog_id=catalog_id,
            s3_output_location=output_loc, workgroup=workgroup, region=region,
        )

    executor = BoundedSqlExecutor(
        run_query=_run_query, render_answer=_render_answer,
    )
    return executor.execute(
        sql=ctx.sql, database_name=ctx.database_name, catalog_id=ctx.catalog_id,
        question=ctx.question, slice_text=ctx.slice_text,
    )


def _build_hybrid_deps(*, kb_id: str, namespace: str, config: dict,
                       question: str = "") -> HybridDeps:
    """Assemble the injected Tier-2 Hybrid phase implementations.

    Args:
        kb_id: The Bedrock Knowledge Base id (Phase 1 few-shots + recall scope).
        namespace: The semantic-layer namespace.
        config: The metadata-layer config item (for vocab lookup, and for the
            layer id / version that scope the topic-router's KB corpus).
        question: This turn's question, used as the input side of the eval
            final-answer span. Empty (the default) still wires the emitter — the
            span is worth more with a blank question than not emitted at all,
            since without it a clarify/degraded turn vanishes from the SESSION
            context entirely.

    Returns:
        A fully-wired :class:`HybridDeps`.
    """
    # The KB corpus is keyed by layer id + version, which are NOT interchangeable
    # with the namespace, so pass them explicitly rather than letting the router
    # assume namespace == id.
    layer_id = config.get('id') or namespace
    version = config.get('version') or 'v1'
    router = _build_topic_router(namespace, layer_id=layer_id, version=version)
    builder = SliceBuilder(
        kb_retrieve=lambda q, ns: _kb_few_shots(q, ns, kb_id=kb_id),
        judge_fn=_build_slice_judge(),
        token_counter=_count_tokens,
        budget=SLICE_TOKEN_BUDGET,
        # Without a chunk source the slice carries table NAMES ONLY — no columns,
        # no joins — so the Phase-3 judge can never find the column it needs, the
        # expand round adds nothing, and the graph degrades ``phase3_max_rounds``
        # while actually holding the table it reports as unavailable.
        chunks_lookup=_build_chunks_lookup(layer_id=layer_id, version=version),
    )
    generator = _build_sql_generator()

    from .neptune_fetch import StructuralGraphFetcher

    fetch = StructuralGraphFetcher(construct_ttl=_neptune_construct_ttl)

    return HybridDeps(
        router=router,
        builder=builder,
        generator=generator,
        fetch=fetch,
        run_execution=_run_tier2_execution,
        vocab=_vocab_for(config),
        dialect="athena",
        answer_emitter=_make_answer_emitter(question=question),
    )


def _count_tokens(text: str) -> int:
    """Count tokens in ``text`` for the slice-budget check.

    Uses ``tiktoken`` (cl100k_base) when available — a good approximation for
    Claude — falling back to a whitespace word count so the builder works under
    the conftest stubs without ``tiktoken`` installed.

    Args:
        text: The serialized-slice text to measure.

    Returns:
        An approximate token count.
    """
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:  # noqa: BLE001 — fall back to a cheap word-count estimate
        return len(text.split())


# ---------------------------------------------------------------------------
# Hybrid artifact guard + Tier-2 delegation.
# ---------------------------------------------------------------------------
def _run_tier2(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run the Tier-2 Hybrid graph for a payload and normalize its result.

    Kept a MODULE-LEVEL function so the entrypoint test can monkeypatch it
    (``main._run_tier2``). Wires :class:`HybridDeps` from the real seams, runs
    the graph, and normalizes the populated :class:`WorkflowContext` into the
    uniform answer dict the chat layer renders.

    Args:
        payload: The request payload — needs ``question`` and ``id`` /
            ``ontologyId``.

    Returns:
        The normalized answer dict (answer / sql_query / executed_sql / executed
        / degraded / results / n_quads / reasoning / metadata / provenance).
    """
    question = payload.get('question') or payload.get('message') or ''
    layer_id = payload.get('id') or payload.get('ontologyId') or ''
    config = get_latest_metadata_item(layer_id) or {}
    namespace = config.get('namespace') or layer_id
    kb_id = _kb_id_for(config)

    deps = _build_hybrid_deps(kb_id=kb_id, namespace=namespace, config=config,
                              question=question)
    # Phase 1 recovers only the DATABASE from its ``database.table`` candidate
    # ids; the CATALOG lives solely in the layer config. An S3 Tables layer's
    # schema (e.g. ``normalized``) resolves only when its federated catalog is
    # named in the Athena execution context, so pass it in explicitly — omitting
    # it makes every Phase-5 execution fail SCHEMA_NOT_FOUND.
    catalog_id, _database = _catalog_database_for_layer(config)
    ctx = run_hybrid_workflow(
        question=question, namespace=namespace, deps=deps, catalog_id=catalog_id,
    )
    return _normalize_tier2(
        ctx=ctx, config=config, question=question, layer_id=layer_id, kb_id=kb_id
    )


# User-facing message per degraded terminal. Module-level so the response builder
# and the eval answer-span emitter read the SAME text — a span whose answer text
# differs from what the user received grades the wrong thing.
_DEGRADED_MESSAGES: Dict[str, str] = {
    "phase1_empty": (
        "I couldn't find any tables in the knowledge base relevant to your "
        "question."
    ),
    "phase3_max_rounds": (
        "I found relevant tables but couldn't assemble a complete enough "
        "schema slice to answer your question reliably."
    ),
    "grounding_unresolved": (
        "I couldn't build a query fully grounded in the available ontology "
        "for your question."
    ),
    "hybrid_verify_unavailable": (
        "I couldn't verify the query against the semantic layer's ontology "
        "(the structural graph was unavailable), so I stopped rather than "
        "return an unverified answer."
    ),
}


def _hybrid_final_answer_text(ctx) -> str:
    """Compute the user-facing final-answer text from a resolved context.

    Single source of truth for the answered / degraded NL answer, shared by
    :func:`_normalize_tier2` and the in-graph eval answer emitter so the response
    and the span carry identical text. Precedence: a specific degraded detail,
    then the per-state degraded message, then the execution result's rendered
    prose, then a generic row/column count.

    Args:
        ctx: The :class:`WorkflowContext` — populated post-graph, or partially
            populated at a terminal node.

    Returns:
        The plain-English answer the user receives. Never raises.
    """
    if ctx.degraded in _DEGRADED_MESSAGES:
        return (getattr(ctx, "degraded_detail", None)
                or _DEGRADED_MESSAGES[ctx.degraded])
    exec_result = ctx.execution_result or {}
    rows = exec_result.get('rows', [])
    columns = exec_result.get('columns', [])
    return exec_result.get('answer', '') or (
        f"Query returned {len(rows)} row(s) across {len(columns)} column(s)."
    )


def _make_answer_emitter(*, question: str) -> Callable[[Any], None]:
    """Build the in-graph eval ``answer_emitter`` closure for one turn.

    Returns a ``(ctx) -> None`` the Hybrid graph's clarify / degraded terminals
    call to emit the final-answer span WHILE the graph's multiagent span is still
    the active (recording) OTEL context — see ``HybridDeps.answer_emitter``. Those
    terminals make no model call of their own, so without this span the turn is
    dropped from the SESSION ``{context}`` entirely and the judges see a
    conversation that appears to have fewer turns than it had.

    Args:
        question: The question for this turn (the input side of the span).

    Returns:
        A ``(ctx) -> None`` emitter. Fail-soft: ``emit_answer_span`` swallows its
        own errors, so this never breaks a query.
    """
    def _emit(ctx) -> None:
        """Emit one final-answer span from ``ctx`` at a graph terminal."""
        history = getattr(ctx, "conversation_history", None) or None
        if ctx.needs_clarification is not None:
            # Clarify terminal — grade the question + the options the user saw.
            emit_answer_span(
                question=question,
                answer=ctx.needs_clarification.get('clarification_question', '')
                or ctx.needs_clarification.get('answer', ''),
                options=ctx.needs_clarification.get('options'),
                operation_label='clarification',
                conversation_history=history,
            )
            return
        # Degraded terminal — grade the real explanation the user received. No
        # executed_sql / retrieved_schema: a degraded turn ran no query, and
        # claiming otherwise would let the SqlGrounded judge score a query that
        # never executed.
        emit_answer_span(
            question=question,
            answer=_hybrid_final_answer_text(ctx),
            operation_label='final_answer',
            conversation_history=history,
        )

    return _emit


def _normalize_tier2(
    *,
    ctx,
    config: dict,
    question: str = '',
    layer_id: str = '',
    kb_id: str = '',
) -> Dict[str, Any]:
    """Normalize a completed Hybrid :class:`WorkflowContext` into an answer dict.

    Maps each degraded terminal to a user-facing message and exposes the
    executed-vs-generated SQL distinction so the eval harness never scores a
    gate-rejected query as if it had run. On a degraded terminal it makes one last
    attempt to answer from the layer's UPLOADED DOCUMENTS before abstaining.

    Args:
        ctx: The populated :class:`WorkflowContext`.
        config: The metadata-layer config (for provenance source labels).
        question: The user's question — needed for the document terminal. Empty
            skips that terminal (an older caller keeps its exact behaviour).
        layer_id: The semantic layer id, scoping document retrieval.
        kb_id: The semantic-rag Knowledge Base id holding the layer's documents.

    Returns:
        The normalized answer dict.
    """
    # Clarification short-circuit.
    if ctx.needs_clarification is not None:
        payload = dict(ctx.needs_clarification)
        payload.setdefault('answer', payload.get('clarification_question', ''))
        payload['n_quads'] = ctx.kb_sources
        payload['metadata'] = {'usage': ctx.usage}
        return payload

    exec_result = ctx.execution_result or {}
    columns: list = exec_result.get('columns', [])
    rows: list = exec_result.get('rows', [])
    results_list = [
        {col: (row[i] if i < len(row) else '') for i, col in enumerate(columns)}
        for row in rows
    ]

    result_text = _hybrid_final_answer_text(ctx)

    executed = bool(ctx.execution_result) and ctx.degraded is None
    sql_query = ctx.sql or exec_result.get('sql_query', '')
    executed_sql = (exec_result.get('sql_query', '') or sql_query) if executed else ''

    prov_sources = (
        [f"table:{c.split('.')[-1]}" for c in ctx.candidates]
        if ctx.candidates else ["kb"]
    )

    # --- Document terminal: answer FROM the layer's uploaded documents --------
    # Reached ONLY on a degraded terminal — no SQL could serve the question — so a
    # Hybrid layer whose SQL path works is completely unaffected. The call is
    # IDENTICAL to the one in the metadata (Semantic RAG) and ontology (VKG)
    # agents; the modules are copied into this package rather than imported from
    # ``shared/`` because this agent's container ships only its own package (see
    # tests/unit/test_hybrid_isolation.py), and they are byte-identical copies.
    #
    # KNOWN EVAL-FIDELITY GAP (same as the VKG agent): the final-answer span is
    # emitted IN-GRAPH by the answer emitter, which has already run by the time we
    # get here, so on a document-answered turn that span carries the graph's
    # degraded text and the SESSION judges grade the wrong string. The USER-facing
    # response below is correct. Fixing it means moving the terminal inside the
    # graph — out of scope here, recorded in the design doc rather than papered
    # over with a post-graph emit that would orphan into its own trace.
    document_answer: Optional[DocumentAnswer] = None
    if ctx.degraded is not None and question:
        document_answer = try_document_answer(
            question=question,
            kb_id=kb_id,
            layer_id=layer_id,
            synthesize=_advisory_synthesize,
            kb_client=_bedrock_agent_runtime(),
        )
    # `degraded` drives the "this turn failed" badge; a grounded document answer
    # did not fail, so it is reported as None with the tier carrying the fact that
    # no SQL ran (`executed` False, `executed_sql` ""). The SQL-path reason is
    # preserved in the reasoning trace and the log line below.
    degraded_out = ctx.degraded
    data_source = "Athena (hybrid semantic SQL)"
    if document_answer is not None:
        logger.info(
            "Answered from documents after SQL degrade (%s) for layer %s; "
            "citations=%s",
            ctx.degraded, layer_id, document_answer.citations,
        )
        result_text = document_answer.answer
        prov_sources = document_answer.sources
        data_source = (
            "Uploaded documents (Bedrock Knowledge Base) — no SQL path: "
            f"{ctx.degraded}"
        )
        degraded_out = None

    return {
        "answer": result_text,
        "sql_query": sql_query,
        "executed_sql": executed_sql,
        "executed": executed,
        "degraded": degraded_out,
        "grounding_rounds": ctx.grounding_rounds,
        "results": results_list,
        "columns": columns,
        "n_quads": ctx.kb_sources,
        "reasoning": {
            "interpretation": (
                f"Resolved {len(ctx.candidates)} candidate table(s) via KB recall"
            ),
            "graphTraversal": "ontology-verified hybrid grounding",
            "dataSourceSelection": data_source,
            "sqlQuery": executed_sql,
            "summarization": result_text,
        },
        "metadata": {"tier": 2, "usage": ctx.usage},
        "provenance": build_provenance(
            tier="document" if document_answer is not None else "semantic_sql",
            sources=prov_sources,
            degraded=degraded_out,
        ),
    }


def _artifacts_for(config: dict, *, namespace: str) -> Dict[str, bool]:
    """Probe whether BOTH hybrid artifacts exist for a layer.

    The hybrid answer requires a Bedrock Knowledge Base (KB) AND a thin
    virtual-knowledge-graph (VKG) in Neptune. This derives both existence flags
    behind seams so it stays testable:

    * ``kb`` — a KB id is present on the layer config (or the env fallback).
    * ``vkg`` — the thin-VKG existence probe (:func:`_vkg_exists`) confirms a
      structural graph is available for this namespace.

    Args:
        config: The metadata-layer config item.
        namespace: The semantic-layer namespace to probe the VKG for.

    Returns:
        ``{"kb": bool, "vkg": bool}``.
    """
    return {
        "kb": bool(_kb_id_for(config)),
        "vkg": _vkg_exists(namespace=namespace, config=config),
    }


def _vkg_exists(*, namespace: str, config: dict) -> bool:
    """Return whether a thin-VKG structural graph is available for a namespace.

    The probe is conservative and fail-loud by design: it returns True only when
    the Neptune Gateway is configured AND the layer config records that its
    thin VKG was built. When it cannot positively confirm the VKG, it returns
    False so :func:`_resolve` degrades rather than answering from the KB alone.

    Args:
        namespace: The semantic-layer namespace (reserved for a future
            per-namespace probe; the current check is gateway + config-flag).
        config: The metadata-layer config item.

    Returns:
        True when a thin VKG is confirmed available; False otherwise.
    """
    # The gateway is the ONLY Neptune path this runtime has (see
    # _neptune_construct_ttl), so its URL is what "reachable" means here.
    endpoint_configured = bool(os.environ.get('NEPTUNE_GATEWAY_URL', ''))
    # The metadata pipeline stamps the layer config once its thin VKG is
    # materialized in Neptune. Accept either an explicit flag or a recorded id.
    vkg_built = bool(config.get('vkgBuilt') or config.get('vkgGraphId'))
    return endpoint_configured and vkg_built


def _resolve(*, payload: Dict[str, Any], artifacts: Dict[str, bool]) -> Dict[str, Any]:
    """Guard the hybrid artifacts, then delegate to Tier-2.

    FIRST asserts BOTH ``artifacts["kb"]`` and ``artifacts["vkg"]`` are truthy.
    If EITHER is falsy it returns a ``hybrid_artifact_missing`` degraded result
    immediately — fail loud, never a silent single-engine fallback. Only when
    both exist does it delegate to :func:`_run_tier2`.

    Args:
        payload: The request payload (``question`` + layer id).
        artifacts: ``{"kb": bool, "vkg": bool}`` — passed in so tests can inject;
            the real entrypoint derives it via :func:`_artifacts_for`.

    Returns:
        Either the ``hybrid_artifact_missing`` degraded dict or the normalized
        Tier-2 answer dict from :func:`_run_tier2`.
    """
    kb_ok = bool(artifacts.get("kb"))
    vkg_ok = bool(artifacts.get("vkg"))
    if not (kb_ok and vkg_ok):
        missing = [name for name, ok in (("kb", kb_ok), ("vkg", vkg_ok)) if not ok]
        logger.warning(
            "hybrid artifact(s) missing %s — refusing single-engine fallback",
            missing,
        )
        return {
            "answer": (
                "This semantic layer isn't fully prepared for hybrid querying "
                "yet — the required "
                + (" and ".join(
                    {"kb": "knowledge base", "vkg": "ontology graph"}[m]
                    for m in missing))
                + " artifact(s) are not available. Please try again once "
                "preparation completes."
            ),
            "sql_query": "",
            "executed_sql": "",
            "executed": False,
            "degraded": "hybrid_artifact_missing",
            "missing_artifacts": missing,
            "results": [],
            "n_quads": [],
            "reasoning": {
                "interpretation": "hybrid artifact guard failed",
                "graphTraversal": "",
                "dataSourceSelection": "",
                "sqlQuery": "",
                "summarization": "hybrid_artifact_missing",
            },
            "metadata": {"tier": 2},
            "provenance": build_provenance(
                tier="semantic_sql", sources=[], degraded="hybrid_artifact_missing",
            ),
        }
    return _run_tier2(payload)


# ---------------------------------------------------------------------------
# Governed-rule tier — runs between Tier 1 (metrics) and Tier 2 (hybrid graph).
#
# Why this works for a HYBRID layer: a rule compiles to SPARQL, and the gateway's
# Ontop reformulator turns that SPARQL into SQL against whatever graph carries the
# ``vkg:mapsToTable`` / ``vkg:mapsToColumn`` mappings. A hybrid layer's thin VKG
# carries exactly those, so this is the one place the hybrid agent uses Ontop —
# its own Tier 2 generates SQL directly instead.
# ---------------------------------------------------------------------------
_ontology_cache: Dict[str, str] = {}
"""Per-container ontology-JSON cache, keyed by ontology id. Value is the JSON
text (not the dict) so a caller mutating its copy cannot poison the cache."""


def _neptune_gateway_mcp():
    """Build an unopened MCP client for the Neptune Gateway, or None.

    The gateway is this runtime's ONLY Neptune path (see
    :func:`_neptune_construct_ttl`), so an unset URL means the rule graph is
    unreadable. Returns None rather than raising; each caller decides whether
    that is fatal.

    Returns:
        An unopened Strands ``MCPClient``, or None when ``NEPTUNE_GATEWAY_URL``
        is not configured.
    """
    gateway_url = os.environ.get('NEPTUNE_GATEWAY_URL', '')
    if not gateway_url:
        return None
    # Lazy: the transport imports strands/mcp_proxy_for_aws, which the unit-test
    # stubs do not provide.
    from .neptune_gateway import build_gateway_client

    return build_gateway_client(gateway_url=gateway_url, region=region)


def _fetch_published_rules(namespace: str) -> List[Dict[str, Any]]:
    """Read a namespace's rules from Neptune as plain dicts.

    Positional-parameter signature on purpose: this is the ``fetch_rules``
    callable ``rule_lookup`` and ``knn_hydration`` invoke as ``fetch_rules(ns)``.

    ``rule_graph.read_rules`` returns validated ``Rule`` models while the lookup
    contract is raw rows (it re-validates the one row it matched). Dumping back
    to dicts keeps Neptune the single source of truth and the RDF vocabulary in
    exactly one module, at the cost of one round-trip through the model for rules
    the lookup will discard.

    Args:
        namespace: Ontology id owning the ``<ontology-graph>/rules`` graph.

    Returns:
        One dict per rule in the graph; empty when the graph does not exist.

    Raises:
        RuntimeError: The Neptune gateway URL is not configured. The rule tier
            catches this and falls through to Tier 2.
    """
    mcp = _neptune_gateway_mcp()
    if mcp is None:
        raise RuntimeError("NEPTUNE_GATEWAY_URL not set — cannot read rules")
    # Lazy: rule_graph pulls in rdflib, which is only needed on this path.
    from .neptune_gateway import NeptuneGatewayClient
    from .rule_graph import read_rules as read_rules_from_graph

    with mcp:
        gateway = NeptuneGatewayClient(mcp_client=mcp)
        rules = read_rules_from_graph(
            namespace=namespace,
            # read_rules rebuilds an RDF graph from the bindings, so it needs the
            # unflattened SPARQL-results JSON (term types), not flattened rows.
            execute_sparql=lambda *, query, query_type: gateway.select_json(
                sparql=query,
            ),
        )
    logger.info("rule tier: namespace=%s rules_in_graph=%d", namespace, len(rules))
    return [r.model_dump() for r in rules]


def _cached_ontology_json(*, ontology_id: str, namespace: str) -> Dict[str, Any]:
    """Return the layer's ontology JSON (OBDA mappings included), cached.

    The ``mappings`` section is what Ontop reformulates through, and it is
    identical in shape for a hybrid layer's thin VKG and a full VKG.

    Args:
        ontology_id: The ontology / layer id (the ``id`` from the payload).
        namespace: Fallback graph-scoping id when ``ontology_id`` is empty.

    Returns:
        The ``get_ontology_from_neptune`` payload.

    Raises:
        RuntimeError: The Neptune gateway URL is not configured. The rule tier
            converts this into a fall-through.
    """
    key = ontology_id or namespace
    cached = _ontology_cache.get(key)
    if cached:
        return json.loads(cached)
    mcp = _neptune_gateway_mcp()
    if mcp is None:
        raise RuntimeError("NEPTUNE_GATEWAY_URL not set — cannot fetch ontology")
    from .neptune_gateway import NeptuneGatewayClient

    with mcp:
        ontology_json = NeptuneGatewayClient(mcp_client=mcp).fetch_ontology(
            ontology_id=key,
        )
    # Only a clean payload is cached: caching an error envelope would make one
    # transient gateway failure permanent for the life of the container.
    if ontology_json and "error" not in ontology_json:
        try:
            _ontology_cache[key] = json.dumps(ontology_json)
        except (TypeError, ValueError):
            pass
    return ontology_json


def _rule_translate(*, sparql: str, ontology_json: Dict[str, Any],
                    ontology_id: str) -> Dict[str, Any]:
    """Translate a rule's compiled SPARQL to Athena SQL via the Ontop gateway.

    The ``translate`` callable :func:`rule_executor.execute_rule` injects.

    Args:
        sparql: The rule's compiled SPARQL SELECT.
        ontology_json: The ``fetch_ontology`` payload (carries the OBDA mappings).
        ontology_id: Stable id so the Ontop Handler reuses its warm reformulator.

    Returns:
        ``{sql, database, catalog}``, or ``{"error": ...}`` on a failed
        reformulation (``execute_rule`` inspects both).

    Raises:
        RuntimeError: The Neptune gateway URL is not configured.
    """
    mcp = _neptune_gateway_mcp()
    if mcp is None:
        raise RuntimeError("NEPTUNE_GATEWAY_URL not set — cannot translate rule")
    from .neptune_gateway import NeptuneGatewayClient

    with mcp:
        return NeptuneGatewayClient(mcp_client=mcp).translate_sql(
            sparql=sparql, ontology_json=ontology_json, ontology_id=ontology_id,
        )


def _rule_athena_run(*, sql: str, database_name: str,
                     catalog_id: str) -> Dict[str, Any]:
    """Run one rule-derived SELECT through the hybrid Athena seam.

    Split out from :func:`_run_rule_sql` purely as a seam: this is the half that
    touches AWS, so a unit test replaces this and still exercises the
    result-shape translation below it.

    Args:
        sql: The Ontop-translated SQL.
        database_name: Athena database (schema) to run against.
        catalog_id: Athena catalog id ('' / ``AWSDataCatalog`` => default Glue).

    Returns:
        ``athena_run_query``'s dict: ``{columns, rows, query_execution_id}`` or
        ``{error, query_execution_id}``.
    """
    from .execution_agent import athena_run_query

    return athena_run_query(
        sql=sql, database_name=database_name, catalog_id=catalog_id,
        s3_output_location=os.environ.get('ATHENA_OUTPUT_LOCATION', ''),
        workgroup=os.environ.get('ATHENA_WORKGROUP', 'primary'),
        region=region,
    )


def _run_rule_sql(*, sql: str, database_name: str,
                  catalog_id: str) -> Dict[str, Any]:
    """The ``execute_sql`` seam ``execute_rule`` injects, shape-translated.

    The two runtimes report a query failure differently: this agent's
    ``athena_run_query`` returns ``{"error": ...}``, while ``execute_rule``
    inspects ``state_change_reason`` (the VKG agent's key). Without this
    translation a FAILED query would reach ``execute_rule`` looking like a
    successful run with no rows, and a governed rule would answer "0 results"
    for a query that never ran — silently.

    Args:
        sql: The Ontop-translated SQL.
        database_name: Athena database (schema) to run against.
        catalog_id: Athena catalog id.

    Returns:
        ``{columns, rows, state_change_reason, query_execution_id}`` —
        ``state_change_reason`` is None on success and the Athena failure text
        otherwise.
    """
    result = _rule_athena_run(
        sql=sql, database_name=database_name, catalog_id=catalog_id,
    )
    error = result.get("error")
    return {
        "columns": result.get("columns", []) if not error else [],
        "rows": result.get("rows", []) if not error else [],
        "state_change_reason": error or None,
        "query_execution_id": result.get("query_execution_id", ""),
    }


def _advisory_kb_retrieve(*, config: dict, namespace: str) -> Callable[[str], Any]:
    """Build the ``kb_retrieve`` callable an advisory answer grounds itself in.

    Unlike the VKG agent — which has no KB and synthesizes advisory context from
    its ontology annotations — a hybrid layer HAS a Bedrock Knowledge Base, so
    the advisory path reads the real schema corpus. Fail-soft: any retrieval
    error yields an empty context, so advisory still answers from the governed
    metrics alone rather than erroring the turn.

    Args:
        config: The metadata-layer config item (carries the KB id).
        namespace: The semantic-layer namespace (KB-filter scope).

    Returns:
        A ``(query: str) -> dict`` callable returning the
        ``{"context": [{"content", "metadata", "score"}, ...]}`` contract.
    """
    kb_id = _kb_id_for(config)

    def _retrieve(query: str) -> Dict[str, Any]:
        """Retrieve KB chunks for one advisory query."""
        chunks = _kb_few_shots(query, namespace, kb_id=kb_id)
        return {
            "context": [
                {"content": c.get("content", ""), "metadata": {}, "score": 0.0}
                for c in chunks
            ],
        }

    return _retrieve


def _advisory_synthesize(prompt: str) -> str:
    """Synthesis callable for advisory answers — one prose completion.

    Args:
        prompt: The fully-formed advisory prompt.

    Returns:
        The model's text answer ('' on an unexpected response shape).
    """
    agent = Agent(model=_build_query_model(),
                  system_prompt="You are a helpful semantic-layer advisor.")
    response = agent(prompt)
    try:
        return response.message['content'][0]['text']
    except (KeyError, IndexError, TypeError):
        return ''


def _build_response_from_rule(*, rule: Any, result: Dict[str, Any],
                              id: str) -> Dict[str, Any]:
    """Shape an executed governed rule into the standard answer payload.

    Same contract as :func:`_build_response_from_metric`: every short-circuit
    path must carry ``answer``, ``sql_query``, ``results``, ``n_quads``,
    ``reasoning``, ``metadata`` and ``provenance``.

    Args:
        rule: The executed ``Rule``.
        result: The ``execute_rule`` result dict.
        id: The semantic-layer config id.

    Returns:
        The response payload with a ``governed_rule`` provenance tier.
    """
    answer = build_rule_answer(rule=rule, result=result)
    return {
        "answer": answer,
        "sql_query": result.get("sql", ""),
        "results": result.get("rows", []),
        "n_quads": [],
        "reasoning": {
            "interpretation": f"Governed-rule match: {rule.rule_id}",
            # The compiled SPARQL IS the traversal — deterministic, not generated.
            "graphTraversal": rule.compiled_sparql,
            "dataSourceSelection": "Athena (governed rule via Ontop)",
            "sqlQuery": result.get("sql", ""),
            "summarization": answer,
        },
        "metadata": {"rule_id": rule.rule_id, "ontology_id": id},
        "provenance": build_provenance(
            tier="governed_rule", sources=[f"rule:{rule.rule_id}"],
        ),
    }


def _rule_tier_answer(*, question: str, namespace: str, ontology_id: str,
                      id: str, config: dict) -> Optional[Dict[str, Any]]:
    """Try to answer from a PUBLISHED governed rule. None => fall through.

    Runs after Tier 1 (governed metrics) and before Tier 2. Metrics keep
    priority because a governed metric is a more specific and cheaper answer
    than a rule.

    Two outcomes when a rule matches:
      * fully data-mapped and every APPLIED threshold approved → execute, return
        a ``governed_rule`` answer;
      * any unresolved input or unapproved applied threshold → DO NOT execute;
        return an advisory answer carrying a ``knowledge_gap`` that names what is
        missing. Never guess the missing bound.

    Fail-soft throughout: every error returns None so the caller proceeds to
    Tier 2, matching the Tier 1 block's contract.

    Args:
        question: The user's (contextualized) question.
        namespace: Ontology namespace owning the rules graph.
        ontology_id: Ontology id used for the gateway calls.
        id: The semantic-layer config id, echoed on the response.
        config: The metadata-layer config item (KB id for the advisory branch).

    Returns:
        A response payload, or None to fall through to Tier 2.
    """
    # Ships dark: an unset flag is off, and the whole block makes no call at all.
    if os.environ.get("GOVERNED_RULES_ENABLED", "").lower() not in ("true", "1"):
        return None
    try:
        # Imported here, as in _tier1_lookup, so the module stays importable
        # under the unit-test stubs.
        from . import knn_hydration, knn_index

        rule = rule_lookup_fn(
            question=question, namespace=namespace, knn=knn_index,
            knn_endpoint="", knn_index=knn_hydration.RULES_INDEX,
            fetch_rules=_fetch_published_rules,
        )
    except Exception as exc:  # noqa: BLE001 — fall through, never block Tier 2
        logger.warning("rule_lookup error (%s) — falling through", exc)
        return None
    if rule is None:
        return None

    # The SAME predicate rule_executor refuses on, imported rather than
    # re-derived: a caller that disagreed with the executor would either advise
    # forever or be refused and fall to Tier 2 forever, both silently.
    unapproved = unapproved_threshold_ids(rule=rule)
    if rule.missing_inputs or unapproved:
        logger.info("rule_knowledge_gap: %s", rule.rule_id)
        try:
            advisory = build_advisory_answer(
                question=question, layer_id=id,
                kb_retrieve=_advisory_kb_retrieve(
                    config=config, namespace=namespace,
                ),
                metrics_table=metrics_table(),
                synthesize=_advisory_synthesize,
                knowledge_gap={
                    "rule_id": rule.rule_id, "rule_name": rule.name,
                    "missing_inputs": list(rule.missing_inputs),
                    "unapproved_thresholds": unapproved,
                },
            )
        except Exception as exc:  # noqa: BLE001 — advisory is fail-soft too
            logger.warning("rule knowledge-gap advisory failed (%s)", exc)
            return None
        return {
            "answer": advisory.get("answer", ""),
            "sql_query": "",
            "results": [],
            "n_quads": [],
            "reasoning": {
                "interpretation": (
                    f"Governed rule {rule.rule_id} matched but is not evaluable"
                ),
                "graphTraversal": "",
                "dataSourceSelection": "Advisory (rule knowledge gap)",
                "sqlQuery": "",
                "summarization": advisory.get("answer", ""),
            },
            "metadata": {"rule_id": rule.rule_id, "ontology_id": id},
            # Tier is advisory (nothing was executed) but the source still names
            # the rule, so the UI can show WHICH rule stalled and why.
            "provenance": build_provenance(
                tier="advisory", sources=[f"rule:{rule.rule_id}"],
            ),
        }

    logger.info("rule_hit: %s", rule.rule_id)
    try:
        result = rule_execute_fn(
            rule=rule,
            ontology_json=_cached_ontology_json(
                ontology_id=ontology_id, namespace=namespace),
            ontology_id=ontology_id,
            translate=_rule_translate,
            execute_sql=_run_rule_sql,
            # The layer's OWN coordinate, not Ontop's derivation. Ontop takes the
            # database from the part of vkg:mapsToTable before the dot, and a
            # hybrid layer's thin VKG stores the BARE table name on purpose (see
            # kb_table_index), so the derivation yields the alphabetically-first
            # TABLE as the schema and the query dies SCHEMA_NOT_FOUND. This is the
            # same pair Tier-1 metrics and the publish gate already use.
            coordinate=_catalog_database_for_layer(config),
        )
    except Exception as exc:  # noqa: BLE001 — includes RuleExecutionError
        logger.warning("rule execution failed (%s) — falling through to Tier 2",
                       exc)
        return None
    return _build_response_from_rule(rule=rule, result=result, id=id)


# ---------------------------------------------------------------------------
# Request/response core: Tier-1 lookup → Tier-2 (artifact-guarded).
# ---------------------------------------------------------------------------
def _run_query_core(payload: Dict[str, Any], context=None) -> Dict[str, Any]:
    """Resolve one question through the metric → rule → Tier-2 Hybrid cascade.

    Runs the Tier-1 governed-metric lookup (cosine >= 0.85 early-return), then
    the governed-rule tier (a no-op unless ``GOVERNED_RULES_ENABLED`` is set);
    on a miss from both, derives the hybrid artifacts and delegates to
    :func:`_resolve` (which enforces the artifact guard before Tier-2).

    Args:
        payload: The request payload (``question``/``message`` + layer id).
        context: The AgentCore invocation context (for JWT-derived identity).

    Returns:
        The normalized answer dict.
    """
    try:
        question = payload.get('question') or payload.get('message') or ''
        if not question:
            return {'error': 'question is required in payload'}

        layer_id = payload.get('id') or payload.get('ontologyId') or ''
        config = get_latest_metadata_item(layer_id)
        if not config:
            raise ValueError(f"metadata config not found: {layer_id}")
        namespace = config.get('namespace') or layer_id

        # Bind the caller's grant profile for the SQL firewall, before any tier
        # can reach Athena. Both entrypoints (the AG-UI stream and `invoke`)
        # funnel through here, so one binding covers both.
        #
        # The authorization namespace is `namespaceId` off the config row — NOT
        # the `namespace` above, which is the layer's own vocabulary namespace
        # and has nothing to do with grants. Read from the config rather than the
        # payload so a caller cannot name a namespace where their grants happen
        # to be unrestricted.
        _firewall_context.bind_from_context(
            context=context,
            user_id=_user_id_from_context(context, payload) or 'anonymous',
            namespace=str(config.get('namespaceId') or ''),
        )

        # --- Tier 1: governed-metric lookup --------------------------------
        metric = _tier1_lookup(question=question, namespace=namespace)
        if metric is not None:
            try:
                catalog_id, database = _catalog_database_for_layer(config)
                from .metric_executor import execute_metric

                result = execute_metric(
                    metric=metric, filters={}, athena=_athena_client(),
                    workgroup=os.environ.get("ATHENA_WORKGROUP", ""),
                    output_loc=os.environ.get("ATHENA_OUTPUT_LOCATION", ""),
                    catalog_id=catalog_id, database_name=database,
                )
                return _build_response_from_metric(
                    metric=metric, result=result, id=layer_id,
                )
            except Exception as exc:  # noqa: BLE001 — fall through to Tier 2
                logger.warning("tier1_execute failed (%s) — falling through to "
                               "Tier 2", exc)

        # --- Governed rules: after metrics, before the hybrid graph ---------
        # A short-circuit, not a gate: a miss (or an unset GOVERNED_RULES_ENABLED)
        # returns None and the turn continues into Tier 2 untouched.
        rule_answer = _rule_tier_answer(
            question=question, namespace=namespace, ontology_id=layer_id,
            id=layer_id, config=config,
        )
        if rule_answer is not None:
            return rule_answer

        # --- Tier 2: hybrid graph (artifact-guarded) -----------------------
        artifacts = _artifacts_for(config, namespace=namespace)
        return _resolve(payload=payload, artifacts=artifacts)
    except Exception as exc:  # noqa: BLE001 — never a 5xx; degrade to error dict
        logger.exception("hybrid query core error")
        return {'error': f'Agent execution failed: {exc}'}


def _tier1_lookup(*, question: str, namespace: str):
    """Run the Tier-1 governed-metric KNN lookup (fail-soft).

    Args:
        question: The natural-language user question.
        namespace: The semantic-layer namespace to scope the KNN filter to.

    Returns:
        The matched ``Metric`` when the top KNN hit clears the cosine threshold,
        or ``None`` on any miss/error (the caller then proceeds to Tier 2).
    """
    try:
        from . import knn_index
        from .metric_lookup import lookup as tier1_lookup

        return tier1_lookup(
            question=question, namespace=namespace, ddb_table=metrics_table(),
            knn=knn_index, knn_endpoint="", knn_index="metrics",
        )
    except Exception as exc:  # noqa: BLE001 — Tier-1 miss must fall through
        logger.warning("tier1_lookup error (%s) — falling through to Tier 2", exc)
        return None


def _persist_lessons_turn(payload: Dict[str, Any], result: Dict[str, Any]) -> None:
    """Best-effort: persist this turn's (question, answer) into AgentCore Memory.

    The query agent runs a deterministic Tier-2 graph (no conversational Strands
    ``Agent``), so there is no ``MessageAddedEvent`` for a hook to observe. This
    feeds the resolved turn into the same guarded write path directly. No-op when
    ``LESSONS_MEMORY_ID`` is unset, the result is an error, or scope is
    incomplete; never raises.

    Args:
        payload: The request payload (carries question, layer id, session, user).
        result: The normalized answer dict from :func:`_run_query_core`.
    """
    memory_id = os.environ.get('LESSONS_MEMORY_ID', '')
    if not memory_id or not isinstance(result, dict) or result.get('error'):
        return
    question = payload.get('question') or payload.get('message') or ''
    answer = result.get('answer') or ''
    session_id = payload.get('sessionId') or ''
    layer_id = payload.get('id') or payload.get('ontologyId') or ''
    user_id = payload.get('userId') or 'anonymous'
    if not question or not answer or not layer_id or not session_id:
        return
    config = get_latest_metadata_item(layer_id) or {}
    version = config.get('version') or ''
    if not version:
        return
    persist_turn_pair(
        memory_id=memory_id,
        actor_id=f"{layer_id}/{version}/{user_id}",
        session_id=session_id,
        user_text=question,
        assistant_text=answer,
        guardrail=_memory_guardrail,
        region=region,
    )


def _run_query(payload: Dict[str, Any], context=None) -> Dict[str, Any]:
    """Resolve one question, then persist the turn into AgentCore Memory.

    Thin wrapper over :func:`_run_query_core` so every invocation path records
    lessons through a single chokepoint. Persistence is best-effort and never
    alters the result.

    Args:
        payload: The request payload.
        context: The AgentCore invocation context.

    Returns:
        The normalized answer dict.
    """
    result = _run_query_core(payload, context=context)
    try:
        payload = {**payload,
                   'userId': _user_id_from_context(context, payload) or 'anonymous'}
        _persist_lessons_turn(payload, result)
    except Exception as exc:  # noqa: BLE001 — memory write must never break a reply
        logger.warning("lessons-turn persistence failed (non-fatal): %s", exc)
    return result


# ---------------------------------------------------------------------------
# AG-UI streaming chat dispatch.
# ---------------------------------------------------------------------------
def _chunk_text(text: str, *, max_chars: int = 80):
    """Yield ``text`` in fixed-size chunks for streaming the assistant message."""
    if not text:
        return
    for i in range(0, len(text), max_chars):
        yield text[i:i + max_chars]


def _chat_stream(payload: Dict[str, Any], context):
    """AG-UI streaming generator for the Hybrid chat path.

    Emits ``run_started``, applies the INPUT guardrail, enforces session-to-user
    ownership, persists the user turn, resolves the question via
    :func:`_run_query`, streams the answer + tool card, persists the assistant
    turn, and emits ``run_finished``.

    Args:
        payload: The chat request payload (``message`` / ``turnId`` / ``sessionId``).
        context: The AgentCore invocation context.

    Yields:
        AG-UI envelope dicts.
    """
    turn_id = payload.get('turnId') or 't-anon'
    emitter = AGUIEmitter(turn_id=turn_id)

    emitter.run_started(agent='semantic_layer_query', model=QUERY_MODEL_ID)
    for line in emitter.drain():
        yield line

    message = payload.get('message', '')
    session_id = payload.get('sessionId', '')
    user_id = _user_id_from_context(context, payload)

    # INPUT guardrail: screen the user message BEFORE resolving.
    g_in = _guardrails.apply(message, source='INPUT')
    if g_in['blocked']:
        emitter.run_error(error=g_in['message'], reason='GUARDRAIL_INPUT')
        for line in emitter.drain():
            yield line
        return

    # Enforce session-to-user binding, then persist the user turn.
    if session_id:
        try:
            _chat_sessions.get_or_create(
                session_id=session_id,
                ontology_id=payload.get('ontologyId', ''),
                mode=payload.get('mode', 'hybrid'),
                user_id=user_id,
                source=payload.get('source', 'chat'),
            )
        except SessionOwnershipError:
            logger.warning('chat session ownership violation: session=%s user=%s',
                           session_id, user_id)
            emitter.run_error(
                error='This chat session does not belong to the authenticated user.',
                reason='FORBIDDEN_SESSION')
            for line in emitter.drain():
                yield line
            return
        try:
            _chat_sessions.append_turn(
                session_id=session_id, role='user', text=message,
                turn_id=turn_id, user_id=user_id)
        except Exception as exc:  # noqa: BLE001 — never break the stream on DDB error
            logger.warning('chat persist (user) failed (non-fatal) session=%s: %s',
                           session_id, exc)

    query_payload = {
        'question': payload.get('message', ''),
        'id': payload.get('ontologyId', '') or payload.get('id', ''),
        'sessionId': session_id,
        'userId': user_id or payload.get('userId', '') or 'anonymous',
    }
    try:
        result = _run_query(query_payload, context=context)
    except Exception as exc:  # noqa: BLE001
        emitter.run_error(error=f"agent failed: {exc}")
        for line in emitter.drain():
            yield line
        return

    sql_query = result.get('sql_query', '') if isinstance(result, dict) else ''
    if sql_query:
        call_id = f"sql-{turn_id}"
        emitter.tool_call_start(
            tool_name='execute_sql_query', call_id=call_id,
            args={'sql': sql_query[:500]})
        for line in emitter.drain():
            yield line
        emitter.tool_call_end(
            call_id=call_id,
            result={
                'rowCount': len(result.get('results', []) or []),
                'kbSources': len(result.get('n_quads', []) or []),
            })
        for line in emitter.drain():
            yield line

    answer_text = (result.get('answer') or '') if isinstance(result, dict) else str(result)
    for delta in _chunk_text(answer_text):
        emitter.message_chunk(delta=delta)
        for line in emitter.drain():
            yield line

    rows = (result.get('results', []) or []) if isinstance(result, dict) else []
    kb_sources = (result.get('n_quads', []) or []) if isinstance(result, dict) else []
    metadata = result.get('metadata', {}) if isinstance(result, dict) else {}
    _ROW_CAP = 200
    totals = {
        'sql': sql_query,
        'rowCount': len(rows),
        'rows': rows[:_ROW_CAP],
        'truncated': len(rows) > _ROW_CAP,
        'kbSources': kb_sources,
        'usage': metadata.get('usage') or {},
        'provenance': result.get('provenance') if isinstance(result, dict) else None,
        'executedSql': result.get('executed_sql', '') if isinstance(result, dict) else '',
        'degraded': result.get('degraded') if isinstance(result, dict) else None,
    }

    if session_id:
        try:
            _chat_sessions.append_turn(
                session_id=session_id, role='assistant', text=answer_text,
                turn_id=turn_id, totals=totals, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning('chat persist (assistant) failed (non-fatal) session=%s: %s',
                           session_id, exc)

    emitter.run_finished(message_id=f"m-{turn_id}", totals=totals)
    for line in emitter.drain():
        yield line


@app.entrypoint
def invoke(payload: Dict[str, Any], context):
    """Dispatching entrypoint.

    Streams an AG-UI chat response when the payload carries ``turnId`` /
    ``messages``; otherwise runs the request/response path (MCP tools, direct
    runtime invocation).

    Args:
        payload: The request payload.
        context: The AgentCore invocation context.

    Returns:
        A generator of AG-UI envelope dicts (chat path), or the normalized answer
        dict (request/response path).
    """
    is_chat = bool(payload.get('turnId') or 'messages' in payload)
    if is_chat:
        return _chat_stream(payload, context=context)
    return _run_query(payload, context=context)


if __name__ == '__main__':  # pragma: no cover — container entrypoint
    try:
        app.run()
    except Exception as exc:
        import traceback

        logger.error("STARTUP FATAL: app.run() failed: %s\n%s", exc,
                     traceback.format_exc())
        raise
