"""Advisory answers — describe the layer, never query it.

The advisory tier answers questions ABOUT a semantic layer ("what can I ask?",
"what metrics could I calculate?", "explain the coverage table") rather than
FROM it. It grounds every answer in two metadata sources only:

  1. the layer's Bedrock Knowledge Base (schema chunks), via an injected
     ``kb_retrieve`` callable, and
  2. the governed-metric catalog (DynamoDB ``semantic-layer-metrics``), keyed by
     layer id only (no version — the namespace IS the layer id).

**No-SQL guarantee is structural, not a prompt promise.** This module is handed a
KB-retrieve callable and a metrics DynamoDB table — it has NO Athena client and
no way to execute SQL. Its return dict always carries ``executed_sql == ""`` and
``results == []``. The chat intent-router (in each query agent) and the
``QuerySuggestions`` runtime both call ``build_advisory_answer`` so there is a
single advisory implementation.

This module imports no agent-specific code so both query agents and the
suggestions agent can import it from the shared package.
"""
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

# Synthesis callable: takes a fully-formed prompt string, returns the model's
# text answer. Injected so this module stays free of any Strands/Bedrock import
# and is unit-testable with a stub.
SynthesizeFn = Callable[[str], str]

# KB-retrieve callable: takes a query string, returns the raw retrieval payload
# (the suggestions agent's ``retrieve_kb_context`` returns a JSON string; the
# query agents pass a thin wrapper). We accept either a JSON string or a dict.
KbRetrieveFn = Callable[[str], Any]

# Regex fast-path for the intent router: clear capability/discovery phrasing that
# is unambiguously advisory, so the obvious cases skip the model classifier call.
# Conservative on purpose — anything not matched here is treated as a data query.
_ADVISORY_PATTERNS = [
    re.compile(r"\bwhat\s+can\s+(i|you|we)\s+(ask|do|query)", re.IGNORECASE),
    re.compile(r"\bwhat\s+(could|can)\s+(i|you|we)\s+(calculate|compute|measure)",
               re.IGNORECASE),
    re.compile(r"\bwhat\s+(kind\s+of\s+)?metrics?\b", re.IGNORECASE),
    # Passive/discovery phrasing — "what common metrics could be calculated",
    # "metrics that can be computed". The screenshot question is this passive
    # form, so match "metric(s) ... could/can be calculated/computed/derived"
    # even when "metrics" doesn't sit right after "what".
    re.compile(r"\bmetrics?\b.{0,40}\b(could|can|should)\s+be\s+"  # nosemgrep: string-concat-in-list — intentional multi-line regex/prompt pattern
               r"(calculated|computed|measured|derived)", re.IGNORECASE),
    re.compile(r"\bwhat\s+(questions|kinds?\s+of\s+questions)\b", re.IGNORECASE),
    # "explain the coverage table", "describe the schema", "explain party_type"
    # — allow an optional entity word between the article and the noun, and also
    # match when the explained thing IS the trailing noun (table/column/field).
    re.compile(r"\b(explain|describe)\s+(the\s+)?(\w+\s+)?"  # nosemgrep: string-concat-in-list — intentional multi-line regex/prompt pattern
               r"(schema|data|layer|dataset|table|column|field)\b",
               re.IGNORECASE),
    re.compile(r"\bwhat('?s| is)\s+(in|available\s+in)\s+(this\s+)?(layer|data|dataset|schema)",
               re.IGNORECASE),
    re.compile(r"\bwhat\s+data\s+(is|do\s+you)\s+(available|have)", re.IGNORECASE),
]


def regex_is_advisory(question: str) -> bool:
    """Return True when the question is unambiguously an advisory/meta question.

    This is the router's cheap pre-filter — a positive match routes to advisory
    with no model call. A negative match does NOT mean "data query"; it means
    "not obviously advisory, ask the classifier". Kept conservative so a real
    data query is never pulled out of the SQL cascade by a loose pattern.

    :param question: The user's (contextualized) question text.
    :returns: True if any advisory pattern matches.
    """
    if not question:
        return False
    return any(p.search(question) for p in _ADVISORY_PATTERNS)


# Classifier callable: takes the question, returns the model's structured verdict
# dict ``{"intent": ..., "confidence": ...}``. Injected so this module needs no
# Strands/Bedrock import and is unit-testable with a stub.
ClassifyFn = Callable[[str], Dict[str, Any]]

# Confidence floor below which a model "advisory" verdict is NOT trusted — we
# fall back to data_query. Conservative: a real query mis-flagged as advisory is
# worse than an advisory question taking the (still-correct) data path.
_ADVISORY_CONFIDENCE_FLOOR = 0.7


def classify_intent(
    *,
    question: str,
    classify_fn: Optional[ClassifyFn] = None,
) -> Dict[str, Any]:
    """Classify a question's intent: ``data_query`` | ``advisory`` | ``metric_named``.

    Two-stage, conservative by construction:
      1. **Regex fast-path** — unambiguous meta phrasing returns ``advisory`` with
         confidence 1.0 and NO model call.
      2. **Model gray-zone** — if a ``classify_fn`` is supplied, its verdict is
         used, but an ``advisory`` verdict below the confidence floor is downgraded
         to ``data_query``. With no ``classify_fn`` (or on any error) the default is
         ``data_query`` — today's behavior — so a real query is never pulled out of
         the SQL cascade by the router.

    :param question: The (contextualized) user question.
    :param classify_fn: Optional model classifier ``(question) -> {intent, confidence}``.
    :returns: ``{"intent": str, "confidence": float}``.
    """
    if regex_is_advisory(question):
        return {"intent": "advisory", "confidence": 1.0}

    if classify_fn is None:
        return {"intent": "data_query", "confidence": 1.0}

    try:
        verdict = classify_fn(question) or {}
        intent = verdict.get("intent", "data_query")
        confidence = float(verdict.get("confidence", 0.0))
    except Exception as exc:  # noqa: BLE001 — router must never hard-fail the turn
        logger.warning("classify_intent: classify_fn failed (non-fatal): %s", exc)
        return {"intent": "data_query", "confidence": 0.0}

    # Only honor an advisory verdict above the confidence floor; everything else
    # routes to the unchanged data path.
    if intent == "advisory" and confidence >= _ADVISORY_CONFIDENCE_FLOOR:
        return {"intent": "advisory", "confidence": confidence}
    return {"intent": "data_query", "confidence": confidence}


def list_governed_metrics(*, layer_id: str, metrics_table: Any) -> List[Dict[str, str]]:
    """Return the PUBLISHED governed metrics defined for a layer.

    Governed metrics are keyed by layer id only (the metrics namespace IS the
    layer id — there is no version dimension). Reads definitions only; never
    compiles or executes a metric's SQL.

    :param layer_id: The semantic-layer / ontology config id (== metric namespace).
    :param metrics_table: A boto3 DynamoDB Table resource for the metrics catalog.
    :returns: A list of ``{"metric_id", "name", "description"}`` dicts (PUBLISHED
        metrics only), empty when the layer has none.
    :raises Exception: propagates a DDB error to the caller, which fails soft.
    """
    resp = metrics_table.query(
        KeyConditionExpression=Key("pk").eq(f"NS#{layer_id}")
        & Key("sk").begins_with("METRIC#"),
    )
    metrics: List[Dict[str, str]] = []
    for item in resp.get("Items", []):
        # Only surface metrics the admin has PUBLISHED — DRAFT rows are not part
        # of the layer's official, advertisable surface.
        if item.get("lifecycle") != "PUBLISHED":
            continue
        metrics.append({
            "metric_id": item.get("metric_id", ""),
            "name": item.get("name", ""),
            "description": item.get("description", ""),
        })
    return metrics


def _parse_kb_context(raw: Any) -> List[Dict[str, Any]]:
    """Normalize an injected kb_retrieve return value into a list of chunks.

    The suggestions agent's ``retrieve_kb_context`` returns a JSON STRING shaped
    ``{"context": [{"content", "metadata", "score"}, ...]}``; a query-agent
    wrapper may hand back the dict directly. Accept either. Returns ``[]`` on any
    parse failure or an error payload — the caller treats empty as "KB empty".

    :param raw: The kb_retrieve return value (JSON string or dict).
    :returns: A list of context chunk dicts (possibly empty).
    """
    if raw is None:
        return []
    payload: Any = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("advisory: kb_retrieve returned non-JSON string")
            return []
    if not isinstance(payload, dict):
        return []
    if payload.get("error"):
        logger.warning("advisory: kb_retrieve returned error: %s", payload.get("error"))
        return []
    ctx = payload.get("context", [])
    return ctx if isinstance(ctx, list) else []


def _build_prompt(*, question: str, layer_name: str,
                  metrics: List[Dict[str, str]],
                  kb_chunks: List[Dict[str, Any]]) -> str:
    """Compose the synthesis prompt from the question + grounded metadata.

    The prompt instructs the model to answer ONLY about what is computable and
    what things mean, grounded strictly in the supplied metrics + KB chunks, and
    to never emit SQL or a numeric result.

    :param question: The user's advisory question.
    :param layer_name: Human-readable layer name for context.
    :param metrics: Governed metric definitions (from ``list_governed_metrics``).
    :param kb_chunks: KB context chunks (from ``_parse_kb_context``).
    :returns: The full prompt string.
    """
    metric_lines = (
        "\n".join(
            f"- {m['name']} ({m['metric_id']}): {m['description']}"
            for m in metrics
        )
        or "(no governed metrics are defined for this layer yet)"
    )
    schema_text = (
        "\n\n".join(
            (c.get("content") or "") for c in kb_chunks if c.get("content")
        )
        or "(the schema knowledge base returned no content for this layer)"
    )
    return (
        f"You are a semantic-layer advisor for the layer named '{layer_name}'. "
        "Answer the user's question ABOUT this layer — what can be asked, what "
        "metrics are available, or what a table/column means. Ground your answer "
        "STRICTLY in the governed metrics and schema context below.\n\n"
        "HARD RULES:\n"
        "- Never write SQL and never invent a numeric result. You describe what "
        "is computable and what things mean — you do not compute.\n"
        "- If the schema context is empty, say so plainly and fall back to "
        "describing the governed metrics that exist.\n"
        "- Be concise and use business language, not column names.\n\n"
        f"GOVERNED METRICS:\n{metric_lines}\n\n"
        f"SCHEMA CONTEXT:\n{schema_text}\n\n"
        f"USER QUESTION: {question}\n\n"
        "Advisory answer:"
    )


def build_advisory_answer(
    *,
    question: str,
    layer_id: str,
    kb_retrieve: KbRetrieveFn,
    metrics_table: Any,
    synthesize: SynthesizeFn,
    layer_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a grounded advisory answer about a layer. Never executes SQL.

    Retrieves schema chunks + governed metrics, synthesizes a natural-language
    answer, and returns it in the query agents' standard response shape with
    ``executed_sql == ""`` and ``results == []`` so the no-SQL guarantee is
    visible in the payload, not just the prompt.

    :param question: The user's advisory/meta question.
    :param layer_id: The semantic-layer / ontology config id.
    :param kb_retrieve: Callable ``(query_str) -> JSON str | dict`` for KB context.
    :param metrics_table: boto3 DynamoDB Table resource for the metrics catalog.
    :param synthesize: Callable ``(prompt_str) -> str`` that runs the model.
    :param layer_name: Optional human-readable layer name (defaults to the id).
    :returns: ``{"answer", "metrics", "executed_sql": "", "results": [],
        "kb_empty": bool}``.
    """
    # Governed metrics — fail soft: an advisory answer grounded only in the KB is
    # still useful, and a DDB hiccup must not error the turn.
    try:
        metrics = list_governed_metrics(layer_id=layer_id, metrics_table=metrics_table)
    except Exception as exc:  # noqa: BLE001 — advisory must never hard-fail the turn
        logger.warning("advisory: list_governed_metrics failed (non-fatal): %s", exc)
        metrics = []

    # Schema context from the KB. Empty is a KNOWN condition (un-ingested layers),
    # handled explicitly below rather than producing a blank answer.
    kb_chunks = _parse_kb_context(kb_retrieve(question))
    kb_empty = len(kb_chunks) == 0

    prompt = _build_prompt(
        question=question,
        layer_name=layer_name or layer_id,
        metrics=metrics,
        kb_chunks=kb_chunks,
    )
    answer = synthesize(prompt)

    # Defense in depth for the empty-KB case: if the model returned nothing
    # usable AND the KB was empty, give a deterministic, honest fallback rather
    # than a blank bubble.
    if not (answer or "").strip() and kb_empty:
        if metrics:
            names = ", ".join(m["name"] for m in metrics)
            answer = (
                "The schema knowledge base for this layer is empty, so I can't "
                f"describe its tables — but it defines {len(metrics)} governed "
                f"metric(s) you can ask about: {names}."
            )
        else:
            answer = (
                "I can't answer that yet: this layer has no schema knowledge "
                "base content and no governed metrics defined."
            )

    return {
        "answer": answer,
        "metrics": metrics,
        # Structural no-SQL guarantee — advisory never runs a query.
        "executed_sql": "",
        "results": [],
        "kb_empty": kb_empty,
    }
