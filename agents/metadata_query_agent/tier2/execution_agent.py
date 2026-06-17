"""Phase 5 execution half — bounded Strands agent that runs the grounded SQL.

The grounding gate (``grounding.check_grounding``) runs first in the Phase 5
node; only once the SQL is grounded does this execution agent run. It is scoped
to a single tool (``execute_sql_query``) and prompted (``EXECUTION_PROMPT``) to
execute the provided SQL, fix Athena errors within a tight budget, re-check
zero-row results, and respect the LIMIT 100 / over-limit contract — it cannot
re-discover schema.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Row cap surfaced to the user. The execution path rewrites an unlimited query
# to LIMIT (ROW_CAP + 1) so it can detect and flag truncation.
ROW_CAP = 100


_LIMIT_RE = re.compile(r"\blimit\s+\d+\b", re.IGNORECASE)


def ensure_limit(sql: str, *, cap: int = ROW_CAP) -> Tuple[str, bool]:
    """Return ``(sql_with_limit, injected)``.

    If ``sql`` has no explicit ``LIMIT``, append ``LIMIT cap+1`` so the executor
    can tell whether more than ``cap`` rows would have matched. ``injected`` is
    True when a limit was added (so the caller knows to apply over-limit
    detection on the result).

    Args:
        sql: The grounded SQL.
        cap: The user-facing row cap (default 100).
    """
    if _LIMIT_RE.search(sql):
        return sql, False
    trimmed = sql.rstrip().rstrip(";")
    return f"{trimmed}\nLIMIT {cap + 1}", True


def apply_over_limit(result: Dict[str, Any], *, injected: bool,
                     cap: int = ROW_CAP) -> Dict[str, Any]:
    """Trim an over-cap result to ``cap`` rows and stamp over-limit flags.

    When ``injected`` is True and the query came back with ``cap+1`` rows, the
    true result exceeds the cap: trim to ``cap`` rows, set ``over_limit=True``,
    and record ``total_row_count`` as ``> cap``. Mutates and returns ``result``.

    Args:
        result: The parsed Athena result dict (``columns``/``rows``).
        injected: Whether ``ensure_limit`` added the LIMIT (so cap+1 is meaningful).
        cap: The user-facing row cap.
    """
    rows = result.get("rows", [])
    if injected and len(rows) > cap:
        result["rows"] = rows[:cap]
        result["over_limit"] = True
        result["total_row_count"] = f">{cap}"
    else:
        result.setdefault("over_limit", False)
        result.setdefault("total_row_count", len(rows))
    return result


def build_execution_agent(*, model_factory: Callable[[], Any],
                          execute_tool: Any, system_prompt: str) -> Any:
    """Construct the tightly-scoped Strands execution agent.

    Args:
        model_factory: Zero-arg callable returning a Strands BedrockModel.
        execute_tool: The ``execute_sql_query`` ``@tool`` (its only tool).
        system_prompt: The ``EXECUTION_PROMPT``.
    """
    from strands import Agent  # local import keeps unit tests light

    return Agent(
        model=model_factory(),
        system_prompt=system_prompt,
        tools=[execute_tool],
    )


def run_execution(*, agent: Any, sql: str, database_name: str, catalog_id: str,
                  slice_text: Optional[str] = None) -> Dict[str, Any]:
    """Run the execution agent on the grounded SQL; return text + token usage.

    Args:
        agent: The agent from :func:`build_execution_agent`.
        sql: The grounded (LIMIT-bearing) SQL.
        database_name: Athena database for execution.
        catalog_id: Athena catalog for execution.
        slice_text: The Phase-3 retrieved schema slice (JSON string) the SQL was
            grounded in. When supplied it is prepended to the prompt as a
            read-only ``[retrieved_schema_context]`` block. This is the ONLY way
            the slice reaches an OTEL span: the deterministic graph emits it only
            to the UI ``phase_sink``, so the SESSION-level ``SqlGrounded`` judge —
            which reads the ``execute_sql_query`` span's ``gen_ai.input.messages``
            — would otherwise have no retrieved schema to verify the SQL against
            and fail closed. The execution agent must NOT use it to author or
            rewrite SQL (``EXECUTION_PROMPT`` already forbids that); it is
            grounding context for the trace/eval only.

    Returns:
        ``{"answer": <text>, "usage": {inputTokens, outputTokens, totalTokens}}``.
    """
    # Read-only grounding block: surfaces the retrieved schema slice in the
    # execute_sql_query span so the SqlGrounded judge can verify the SQL against
    # it. Labelled explicitly as non-actionable so the model doesn't treat it as
    # a license to rewrite the query.
    schema_block = ""
    if slice_text:
        schema_block = (
            "The SQL below was already grounded in this retrieved schema slice. "
            "It is provided for trace/eval grounding only — do NOT use it to "
            "modify, extend, or rewrite the SQL.\n"
            f"[retrieved_schema_context]\n{slice_text}\n[/retrieved_schema_context]\n\n"
        )
    prompt = (
        f"{schema_block}"
        "Execute this query and report the result.\n\n"
        f"[sql]\n{sql}\n[/sql]\n"
        f"database_name={database_name}\n"
        f"catalog_id={catalog_id}\n"
    )
    result = agent(prompt)
    try:
        answer = result.message["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        answer = str(result)
    # Reuse the shared extractor so cache-read/write tokens are captured too —
    # Bedrock folds them into totalTokens (cache_config=auto), so omitting them
    # made the turn footer's total diverge from its in/out breakdown.
    try:
        from agents.shared.tier2_graph import extract_usage
    except ImportError:  # container path: agents/ is on PYTHONPATH
        from shared.tier2_graph import extract_usage  # type: ignore
    usage = extract_usage(result)
    return {"answer": answer, "usage": usage}
