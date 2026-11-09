"""codeBased AgentCore evaluator: server-side text-to-SQL execution metrics.

ONE Lambda, THREE evaluators. AgentCore's codeBased evaluators invoke this
handler once per (evaluator, evaluation-target) with an event of the shape:

    {
      "schemaVersion": "1.0",
      "evaluatorId": "...", "evaluatorName": "...",
      "evaluationLevel": "TRACE",
      "evaluationInput": {"sessionSpans": [ <OTel span dicts> ]},
      "evaluationReferenceInputs": [ {                       # level-filtered gold
         "context": {"spanContext": {"sessionId": ..., "traceId": ...}},
         "assertions": [{"text": ...}],
         "expectedResponse": {"text": ...},
         "expectedTrajectory": {"toolNames": [...]}
      } ],
      "evaluationTarget": {"traceIds": [...], "spanIds": [...]}   # None for SESSION
    }

Because ONE evaluator returns ONE score, we compute EX vs. R-VES vs. Soft-F1 by
deploying the SAME handler as THREE Lambda functions, each with ``EVAL_METRIC``
baked into its environment (``ex`` / ``rves`` / ``soft_f1``), and registering one
evaluator per function ARN. AgentCore's batch-evaluation path does NOT deliver
``evaluatorName`` in the event (confirmed against a live run and the SDK's own
``custom_code_based_evaluators`` decorator, which never reads it), so a single
shared Lambda cannot self-identify its metric from the event — the env var is the
reliable per-function discriminator. ``evaluatorName`` is honored only as a
fallback for the direct-invoke / documented-schema path where it is present.

Contract for the return value (AgentCore codeBased evaluator):
  * success → {"label": "PASS"|"FAIL", "value": <float>, "explanation": "..."}
  * error   → {"errorCode": "...", "errorMessage": "..."}
A wrong PREDICTED answer is a SCORE (value 0.0), not a Lambda error. An errorCode
is returned only when the row is genuinely UNMEASURABLE:
  * ``MetricUndetermined`` / ``BadEvalMetricEnv`` — evaluator misconfiguration,
  * ``GoldNotFound`` / ``GoldExecutionFailed``   — the reference side is broken,
  * ``TelemetryUnavailable``                     — ``sessionSpans`` is EMPTY.

That last case matters for metric validity. These metrics are span-derived, so an
empty span list means the trace had not landed in CloudWatch yet, NOT that the
agent failed. Scoring it 0.0 folds telemetry loss into the accuracy figure; an
errorCode removes the row from the denominator so the number describes only rows
we observed. Spans that ARE present but carry no SQL remain a score of 0.0 — that
is a real agent failure.

We use a raw ``def handler(event, context)`` — NOT the
``bedrock_agentcore.evaluation.custom_code_based_evaluators`` decorator — because
that decorator drops ``evaluationReferenceInputs`` (our gold channel).

DEFENSIVE PARSING: the exact key-casing of ``evaluationReferenceInputs`` and the
span attribute names is documented by AWS but not 100% confirmed against a live
run. So on EVERY invocation we log the raw event (truncated) for shape
verification, and the extractors try multiple key spellings and fail loud (clear
errorMessage) if the predicted SQL or gold cannot be located.

The scoring logic lives in the VENDORED sibling modules ``exec_metrics`` (pure
EX/R-VES/Soft-F1) and ``athena_bench`` (``run_sql``). They are byte-identical
copies of ``agents/shared/{exec_metrics,athena_bench}.py`` (their source of
truth), committed here so the Lambda asset is self-contained.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

import boto3

# The vendored copies sit next to this file. When AgentCore loads the handler the
# asset dir is on sys.path already, but under local unit tests (file-path import)
# it is not — insert it so ``import exec_metrics`` / ``import athena_bench``
# resolve to the SELF-CONTAINED copies regardless of the loader.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from athena_bench import run_sql  # noqa: E402 — after sys.path insert (vendored copy)
from exec_metrics import (  # noqa: E402 — after sys.path insert (vendored copy)
    execution_accuracy,
    reward_ves,
    soft_f1,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Naming convention for the three evaluator registrations (Part B notebooks
# create these via create_evaluator). The metric is chosen by CANONICAL SUBSTRING
# match on the evaluator name, separator- and case-insensitively:
#   * "...ExecutionAccuracy..." → EX      (execution_accuracy)
#   * "...RewardVES..."         → R-VES   (reward_ves)
#   * "...SoftF1..."            → Soft-F1 (soft_f1)
# The name may carry a project/dataset prefix or suffix (e.g. "bird_SoftF1_vkg").
_METRIC_SUBSTRINGS = {
    "executionaccuracy": "ex",
    "rewardves": "rves",
    "softf1": "soft_f1",
}


def _canon(text: str) -> str:
    """Lower-case ``text`` and strip separators, for tolerant substring matching.

    Args:
        text: The raw string to canonicalize.

    Returns:
        The string lower-cased with ``_``, ``-`` and spaces removed.
    """
    return text.lower().replace("_", "").replace("-", "").replace(" ", "")


def _metric_for_name(evaluator_name: str) -> str:
    """Map an evaluator name to its metric key: ``ex`` / ``rves`` / ``soft_f1``.

    Matching is by canonical substring (see ``_METRIC_SUBSTRINGS``), so a
    project/dataset prefix or suffix on the name does not defeat it.

    Args:
        evaluator_name: The ``evaluatorName`` from the AgentCore event.

    Returns:
        One of ``"ex"``, ``"rves"``, ``"soft_f1"``.

    Raises:
        ValueError: The name matches none of the known metric substrings — fail
            loud rather than silently defaulting to a metric.
    """
    canon = _canon(evaluator_name)
    for needle, metric in _METRIC_SUBSTRINGS.items():
        if needle in canon:
            return metric
    raise ValueError(
        f"evaluatorName {evaluator_name!r} maps to no known metric "
        f"(expected a substring of ExecutionAccuracy / RewardVES / SoftF1)"
    )


# Span attribute keys that may carry the predicted SQL, most-specific first. The
# runtime's exact semconv key is not 100% confirmed live, so we probe several.
_SQL_ARG_KEYS = (
    "gen_ai.tool.input",
    "gen_ai.request.arguments",
    "gen_ai.tool.arguments",
    "tool.input",
    "tool.arguments",
    "input.value",
    "gen_ai.input.messages",
)
# Tool-name attribute keys (the SQL-executor tool span carries one of these).
_TOOL_NAME_KEYS = ("gen_ai.tool.name", "tool.name", "gen_ai.operation.target")
# Tool names whose input carries the predicted SQL.
_SQL_TOOL_NAMES = ("execute_sql_query", "execute_athena_query", "run_athena_sql")


def _coerce_dict(value: Any) -> Optional[Any]:
    """Return ``value`` parsed as JSON when it is a JSON string, else ``value``.

    Args:
        value: A span-attribute value (often a JSON-encoded string).

    Returns:
        The parsed object when ``value`` is a JSON string; the original ``value``
        for non-strings; ``None`` when a string fails to parse as JSON.
    """
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return None


def _sql_from_arg_value(value: Any) -> str:
    """Best-effort extraction of a SQL string from a tool-argument value.

    Handles the common shapes: a bare SQL string, ``{"sql": ...}`` (or ``query``/
    ``statement``), and a gen_ai ``messages`` list whose text carries the SQL.

    Args:
        value: The raw attribute value (string / dict / list).

    Returns:
        The SQL string, or ``""`` when none is found.
    """
    parsed = _coerce_dict(value)
    # A dict of tool arguments — the SQL is under one of these keys.
    if isinstance(parsed, dict):
        for key in ("sql", "query", "statement", "sql_query", "queryString"):
            candidate = parsed.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""
    # A messages list ([{role, content:[{text}]}]) — scan text for a SQL-looking
    # payload. We only accept it when it clearly contains a SELECT/WITH, to avoid
    # returning a natural-language prompt as "SQL".
    if isinstance(parsed, list):
        for msg in parsed:
            if not isinstance(msg, dict):
                continue
            for part in msg.get("content", []) or []:
                text = part.get("text") if isinstance(part, dict) else None
                if isinstance(text, str) and _looks_like_sql(text):
                    return text.strip()
        return ""
    # A bare string that itself looks like SQL.
    if isinstance(value, str) and _looks_like_sql(value):
        return value.strip()
    return ""


def _looks_like_sql(text: str) -> bool:
    """Heuristic: does ``text`` contain a SQL SELECT/WITH query?

    Args:
        text: Candidate text.

    Returns:
        True when a ``select`` or ``with`` keyword appears (case-insensitive).
    """
    lowered = text.lower()
    return "select " in lowered or lowered.strip().startswith("with ")


def _extract_predicted_sql(spans: List[Dict[str, Any]]) -> str:
    """Walk OTel session spans and return the agent's predicted SQL, or ``""``.

    Strategy (tolerant of the not-fully-confirmed live shape):
      1. Prefer a SQL-executor TOOL span (``gen_ai.tool.name`` in
         ``_SQL_TOOL_NAMES``): read its argument attributes for the SQL.
      2. Otherwise, scan EVERY span's argument attributes for a ``{"sql": ...}``
         or a SELECT/WITH-looking message payload.
    Returns ``""`` when no SQL is present. The CALLER decides what that means, and
    the distinction is load-bearing: ``""`` from a NON-empty span list is a real
    agent failure (score 0.0), whereas an EMPTY span list is telemetry loss and is
    reported as ``TelemetryUnavailable`` so the row leaves the denominator.

    Args:
        spans: The ``evaluationInput.sessionSpans`` list of OTel span dicts.

    Returns:
        The predicted SQL string, or ``""`` when none can be located.
    """
    # Pass 1: SQL-executor tool spans (strongest signal).
    for span in spans:
        attrs = span.get("attributes") if isinstance(span, dict) else None
        if not isinstance(attrs, dict):
            continue
        tool_name = next(
            (attrs.get(k) for k in _TOOL_NAME_KEYS if isinstance(attrs.get(k), str)), ""
        )
        if tool_name in _SQL_TOOL_NAMES:
            for key in _SQL_ARG_KEYS:
                if key in attrs:
                    sql = _sql_from_arg_value(attrs[key])
                    if sql:
                        return sql

    # Pass 2: any span carrying a SQL argument (covers deterministic executors
    # that emit no recognized tool-name attribute).
    for span in spans:
        attrs = span.get("attributes") if isinstance(span, dict) else None
        if not isinstance(attrs, dict):
            continue
        for key in _SQL_ARG_KEYS:
            if key in attrs:
                sql = _sql_from_arg_value(attrs[key])
                if sql:
                    return sql
    return ""


def _parse_gold_payload(text: str) -> Optional[Dict[str, Any]]:
    """Parse a gold JSON payload from a reference-input text field.

    Accepts a raw JSON object, or a ``prefix:{...}`` form (an assertion may carry
    a short prefix before the JSON). Returns the dict only when it carries a
    ``gold_sql`` key.

    Args:
        text: The candidate text (``expectedResponse.text`` or an assertion text).

    Returns:
        The parsed gold dict when it contains ``gold_sql``, else ``None``.
    """
    if not isinstance(text, str):
        return None
    candidates = [text]
    # Prefix fallback: take the substring from the first '{' so a labelled
    # assertion like "gold: {...}" still parses.
    brace = text.find("{")
    if brace > 0:
        candidates.append(text[brace:])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and "gold_sql" in obj:
            return obj
    return None


def _extract_gold(reference_inputs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract the gold payload dict from ``evaluationReferenceInputs``.

    We pack the gold at dataset-build time (Part B) as
    ``expectedResponse.text = json.dumps({"gold_sql":..., "db_id":...,
    "catalog":..., "workgroup"?:...})``. This walks the reference inputs and
    returns the first parseable payload, checking (tolerantly):
      1. ``expectedResponse.text`` (the primary channel),
      2. any ``assertions[].text`` (a JSON assertion, prefix-tolerant).

    Args:
        reference_inputs: The ``evaluationReferenceInputs`` list.

    Returns:
        The gold payload dict (guaranteed to contain ``gold_sql``).

    Raises:
        ValueError: No gold payload could be located — fail loud so the first
            live run surfaces a real key-shape mismatch instead of scoring noise.
    """
    if not isinstance(reference_inputs, list):
        raise ValueError(
            f"evaluationReferenceInputs is not a list (got {type(reference_inputs).__name__})"
        )
    for ref in reference_inputs:
        if not isinstance(ref, dict):
            continue
        # Primary channel: expectedResponse.text (tolerate a couple casings).
        expected = ref.get("expectedResponse") or ref.get("expected_response") or {}
        if isinstance(expected, dict):
            gold = _parse_gold_payload(expected.get("text", ""))
            if gold:
                return gold
        # Fallback: a JSON assertion.
        for assertion in ref.get("assertions", []) or []:
            if isinstance(assertion, dict):
                gold = _parse_gold_payload(assertion.get("text", ""))
                if gold:
                    return gold
    raise ValueError(
        "no gold payload found in evaluationReferenceInputs "
        "(expected expectedResponse.text = json.dumps({'gold_sql':...}))"
    )


def _score(metric: str, *, pred_rows, gold_rows, pred_ms: int, gold_ms: int) -> float:
    """Compute the requested metric from executed result sets.

    Args:
        metric: One of ``"ex"`` / ``"rves"`` / ``"soft_f1"``.
        pred_rows: Rows returned by the predicted SQL.
        gold_rows: Rows returned by the gold SQL.
        pred_ms: Predicted-query engine time (ms) — used by R-VES.
        gold_ms: Gold-query engine time (ms) — used by R-VES.

    Returns:
        The metric value as a float.

    Raises:
        ValueError: ``metric`` is not a known key (defensive; callers pre-map it).
    """
    if metric == "ex":
        return float(execution_accuracy(pred_rows=pred_rows, gold_rows=gold_rows))
    if metric == "rves":
        # R-VES needs positive timings; clamp a zero/absent engine time to 1ms so
        # a fast query does not raise (the ratio is then a lower bound).
        return reward_ves(
            pred_rows=pred_rows,
            gold_rows=gold_rows,
            pred_ms=max(pred_ms, 1),
            gold_ms=max(gold_ms, 1),
        )
    if metric == "soft_f1":
        return soft_f1(pred_rows=pred_rows, gold_rows=gold_rows)
    raise ValueError(f"unknown metric {metric!r}")


def _label(metric: str, value: float) -> str:
    """Map a metric value to a PASS/FAIL label.

    Convention:
      * EX      → PASS iff value == 1 (exact result-set match).
      * R-VES   → PASS iff value > 0 (correct AND some efficiency reward).
      * Soft-F1 → PASS iff value > 0 (any row overlap).

    Args:
        metric: The metric key.
        value: The computed metric value.

    Returns:
        ``"PASS"`` or ``"FAIL"``.
    """
    if metric == "ex":
        return "PASS" if value >= 1.0 else "FAIL"
    return "PASS" if value > 0.0 else "FAIL"


def _preview_row(rows: Sequence[Sequence[Any]], *, max_chars: int = 200) -> str:
    """Render the first result row compactly for a failure explanation.

    Args:
        rows: The result rows (each a sequence of cell values); may be empty.
        max_chars: Hard cap on the returned string length; longer output is
            truncated with a trailing ``…`` so a wide row cannot bloat the payload.

    Returns:
        A ``repr``-style list of the first row's cells, ``"<no rows>"`` when empty,
        truncated to ``max_chars``.
    """
    if not rows:
        return "<no rows>"
    rendered = repr(list(rows[0]))
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "…"
    return rendered


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:  # noqa: ARG001
    """AgentCore codeBased evaluator entrypoint: score one text-to-SQL trace.

    Extracts the predicted SQL from the trace spans and the gold SQL from the
    reference inputs, executes both on Athena, and returns a single metric score
    (EX / R-VES / Soft-F1 chosen by ``evaluatorName``). A missing/failing PREDICTED
    query scores 0 (not an error); a failing GOLD query is an evaluator error.

    Args:
        event: The AgentCore evaluator event (see the module docstring).
        context: The Lambda context (unused).

    Returns:
        On success: ``{"label", "value", "explanation"}``.
        On evaluator fault: ``{"errorCode", "errorMessage"}``.
    """
    # DEFENSIVE: log the raw event (truncated) so the first live run reveals the
    # true delivered key-casing of sessionSpans / evaluationReferenceInputs.
    try:
        logger.info("benchmark-eval raw event: %s", json.dumps(event)[:4000])
    except (TypeError, ValueError):
        logger.info("benchmark-eval raw event (unserializable): %r", str(event)[:4000])

    # sessionSpans dominates the 4000-char cap above and truncates the gold
    # channel out of the log, so log the (small) evaluationReferenceInputs and the
    # top-level keys separately — cheap, and invaluable for confirming the gold
    # shape and which top-level keys AgentCore actually delivers on a live run.
    try:
        logger.info("benchmark-eval event top-level keys: %s", list(event.keys()))
        logger.info(
            "benchmark-eval evaluationReferenceInputs: %s",
            json.dumps(event.get("evaluationReferenceInputs"))[:3000],
        )
    except (TypeError, ValueError):
        logger.info("benchmark-eval refInputs (unserializable)")

    # ── Choose the metric ────────────────────────────────────────────────────
    # AgentCore's batch-evaluation path does NOT deliver ``evaluatorName`` in the
    # event (confirmed live + against the SDK's own decorator, which never reads
    # it), so a single Lambda cannot self-identify its metric from the event. We
    # therefore deploy THREE functions, each with EVAL_METRIC baked into the
    # environment, and register one evaluator per function ARN. The event's
    # ``evaluatorName`` is used only as a fallback for the direct-invoke / doc
    # path where it IS present.
    metric = os.environ.get("EVAL_METRIC", "").strip().lower()
    if metric:
        if metric not in ("ex", "rves", "soft_f1"):
            return {
                "errorCode": "BadEvalMetricEnv",
                "errorMessage": (
                    f"EVAL_METRIC={metric!r} is not one of ex / rves / soft_f1"
                ),
            }
    else:
        # Fallback: derive from evaluatorName when present (direct invoke / docs).
        try:
            metric = _metric_for_name(event["evaluatorName"])
        except (KeyError, ValueError) as exc:
            return {
                "errorCode": "MetricUndetermined",
                "errorMessage": (
                    "no EVAL_METRIC env var and no usable evaluatorName in event: "
                    f"{exc}"
                ),
            }

    # ── Locate the gold payload (missing gold is an evaluator fault) ─────────
    try:
        reference_inputs = event.get("evaluationReferenceInputs", [])
        gold = _extract_gold(reference_inputs)
    except ValueError as exc:
        return {"errorCode": "GoldNotFound", "errorMessage": str(exc)}

    gold_sql = gold.get("gold_sql", "")
    if not isinstance(gold_sql, str) or not gold_sql.strip():
        return {
            "errorCode": "GoldNotFound",
            "errorMessage": "gold payload has no non-empty 'gold_sql'",
        }
    database_name = gold.get("db_id", "") or gold.get("database", "")
    catalog_id = gold.get("catalog", "") or gold.get("catalog_id", "")
    workgroup = gold.get("workgroup", "") or os.environ.get(
        "ATHENA_WORKGROUP", "primary"
    )

    # ── Extract the predicted SQL (empty is a score, not a fault) ────────────
    session_spans = (event.get("evaluationInput", {}) or {}).get(
        "sessionSpans", []
    ) or []
    predicted_sql = _extract_predicted_sql(session_spans)

    athena = boto3.client("athena")

    # ── Execute the GOLD query first — its failure is an evaluator fault ─────
    try:
        gold_result = run_sql(
            athena=athena,
            sql=gold_sql,
            workgroup=workgroup,
            output_loc="",
            database_name=database_name,
            catalog_id=catalog_id,
        )
    except Exception as exc:  # noqa: BLE001 — gold failure is a genuine evaluator error
        return {
            "errorCode": "GoldExecutionFailed",
            "errorMessage": f"gold SQL failed on Athena: {exc}",
        }

    # ── No spans at all is UNMEASURABLE, not a score of 0 ────────────────────
    # These metrics are span-derived: the predicted SQL is read out of the session
    # spans. So an empty ``sessionSpans`` means the evaluator ran before CloudWatch
    # ingested the trace — the agent may well have emitted perfect SQL. Scoring 0.0
    # here silently folds telemetry loss into the accuracy number and is
    # indistinguishable from a wrong answer. Measured on the 2026-08-09 BIRD 11-DB
    # run: 142/500 rows had an empty session context and every one of the 126 that
    # were scored came back EX 0.0 — including 14 whose SQL was recorded in the
    # agent detail. That dragged pooled EX from 0.0625 (intact rows) to 0.0448.
    # Returning an errorCode drops the row from the DENOMINATOR instead, so the
    # reported metric describes only rows we actually observed.
    if not session_spans:
        return {
            "errorCode": "TelemetryUnavailable",
            "errorMessage": (
                "evaluationInput.sessionSpans is empty — the trace had not landed in "
                "CloudWatch when the evaluator ran, so no prediction could be read. "
                "This row is unmeasurable and is excluded rather than scored 0. "
                "Raise ingestion_delay_seconds (it is applied AFTER the last agent "
                "invocation) or reduce per-row latency."
            ),
        }

    # ── Spans present but carrying no SQL scores 0 (a real agent failure) ────
    if not predicted_sql:
        value = 0.0
        return {
            "label": _label(metric, value),
            "value": value,
            "explanation": (
                f"Spans present ({len(session_spans)}) but none carried SQL; "
                "the agent produced no query. Scored 0."
            ),
        }

    # ── Execute the PREDICTED query — a failure here scores 0 ────────────────
    try:
        pred_result = run_sql(
            athena=athena,
            sql=predicted_sql,
            workgroup=workgroup,
            output_loc="",
            database_name=database_name,
            catalog_id=catalog_id,
        )
    except Exception as exc:  # noqa: BLE001 — a bad predicted query is a score of 0
        value = 0.0
        return {
            "label": _label(metric, value),
            "value": value,
            "explanation": f"Predicted SQL failed to execute ({exc}); scored 0.",
        }

    # ── Score ────────────────────────────────────────────────────────────────
    value = _score(
        metric,
        pred_rows=pred_result.rows,
        gold_rows=gold_result.rows,
        pred_ms=pred_result.elapsed_ms,
        gold_ms=gold_result.elapsed_ms,
    )
    explanation = (
        f"metric={metric} value={value:.4f} "
        f"(pred_rows={len(pred_result.rows)} in {pred_result.elapsed_ms}ms, "
        f"gold_rows={len(gold_result.rows)} in {gold_result.elapsed_ms}ms)"
    )
    # On a MISS, record the first row of each side. Row COUNTS alone cannot
    # distinguish the failure modes that matter: a same-count zero-score is most
    # often an extra/reordered COLUMN in the projection (EX is a strict set-match,
    # sensitive to column count and order within a row), but it looks identical in
    # the results JSON to a genuine value disagreement. Diagnosing the
    # codebase_community EX=0.0 batch required replaying queries through Athena
    # history by hand purely because this string omitted the values. Truncated so a
    # wide row cannot bloat the evaluation payload.
    if value < 1.0:
        explanation += (
            f" | pred_row0={_preview_row(pred_result.rows)}"
            f" gold_row0={_preview_row(gold_result.rows)}"
        )
    return {"label": _label(metric, value), "value": value, "explanation": explanation}
