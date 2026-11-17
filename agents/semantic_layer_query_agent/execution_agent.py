"""Phase 5 execution half (Hybrid): bounded, row-capped Athena SQL executor.

Copied-and-owned (per the design's copy-not-import rule) from the Metadata
agent's ``tier2/execution_agent.py`` and the ``execute_sql_query`` tool in its
``main.py``. Re-shaped around an INJECTED ``run_query`` seam: all boto3/Athena
access lives behind a callable the caller supplies, so importing this module
does NOT touch AWS and the module stays unit-testable without cloud creds.

The public surface:

* :func:`ensure_limit` / :func:`apply_over_limit` — deterministic row-cap
  helpers (rewrite an unbounded SELECT to ``LIMIT cap+1``, then trim + flag any
  over-cap result). Pure functions, no AWS.
* :class:`BoundedSqlExecutor` — runs one grounded SELECT through the injected
  ``run_query`` seam, applies the row cap, and returns a result dict shaped
  ``{columns, rows, answer, usage, over_limit, total_row_count}``.
* :func:`athena_run_query` — the DEFAULT seam wiring boto3 Athena. It imports
  boto3 lazily INSIDE the function (never at module import) so unit tests and
  the isolation guard never pull in AWS.

No unit test drives the executor in this task (the generator test covers the
SQL side); this module only needs to import cleanly and pass ruff.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Dict, Optional, Tuple

from .query_prompts import EXECUTION_PROMPT

logger = logging.getLogger(__name__)

# Re-export so callers can wire the execution-agent system prompt without also
# importing query_prompts. ``EXECUTION_PROMPT`` documents the row cap below.
__all__ = [
    "EXECUTION_PROMPT",
    "ROW_CAP",
    "ensure_limit",
    "apply_over_limit",
    "BoundedSqlExecutor",
    "athena_run_query",
]

# Row cap surfaced to the user. The execution path rewrites an unlimited query
# to LIMIT (ROW_CAP + 1) so it can detect and flag truncation.
ROW_CAP = 100

# Matches an explicit trailing/embedded LIMIT clause so ensure_limit is a no-op
# when the SQL already caps its own rows.
_LIMIT_RE = re.compile(r"\blimit\s+\d+\b", re.IGNORECASE)


def ensure_limit(sql: str, *, cap: int = ROW_CAP) -> Tuple[str, bool]:
    """Return ``(sql_with_limit, injected)``.

    If ``sql`` has no explicit ``LIMIT``, append ``LIMIT cap+1`` so the executor
    can tell whether more than ``cap`` rows would have matched. ``injected`` is
    True when a limit was added (so the caller knows to apply over-limit
    detection on the result).

    Args:
        sql: The grounded SQL.
        cap: The user-facing row cap (default :data:`ROW_CAP`).

    Returns:
        A ``(sql, injected)`` tuple.
    """
    if _LIMIT_RE.search(sql):
        return sql, False
    trimmed = sql.rstrip().rstrip(";")
    return f"{trimmed}\nLIMIT {cap + 1}", True


def apply_over_limit(
    result: Dict[str, Any],
    *,
    injected: bool,
    cap: int = ROW_CAP,
) -> Dict[str, Any]:
    """Trim an over-cap result to ``cap`` rows and stamp over-limit flags.

    When ``injected`` is True and the query came back with ``cap+1`` rows, the
    true result exceeds the cap: trim to ``cap`` rows, set ``over_limit=True``,
    and record ``total_row_count`` as ``> cap``. Mutates and returns ``result``.

    Args:
        result: The parsed Athena result dict (``columns``/``rows``).
        injected: Whether :func:`ensure_limit` added the LIMIT (so cap+1 is
            meaningful).
        cap: The user-facing row cap.

    Returns:
        The same ``result`` dict, with over-limit flags applied.
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


class BoundedSqlExecutor:
    """Run one grounded SELECT with a row cap through an injected query seam.

    The seam keeps all AWS access out of this class: construction and execution
    work with any ``run_query`` callable, so the executor can be exercised with
    a stub in tests and wired to :func:`athena_run_query` in production.
    """

    def __init__(
        self,
        *,
        run_query: Callable[..., Dict[str, Any]],
        cap: int = ROW_CAP,
        render_answer: Optional[Callable[..., str]] = None,
    ) -> None:
        """Construct the executor.

        Args:
            run_query: A callable
                ``(sql: str, database_name: str, catalog_id: str) -> dict``
                that executes ``sql`` and returns a parsed result dict with at
                least ``columns`` and ``rows`` keys (and optionally ``error`` /
                ``usage``). Injected so no boto3 import happens on the
                constructor path. Defaults are NOT supplied — the caller MUST
                wire a seam (fail loud rather than silently no-op).
            cap: The user-facing row cap (default :data:`ROW_CAP`).
            render_answer: Optional callable
                ``(columns, rows, over_limit) -> str`` that turns the real result
                into the user-facing natural-language answer (in production a
                bounded LLM — see ``main._render_answer``). Injected for the same
                reason as ``run_query``: this module must import without Bedrock.
                ``None`` (the default) keeps the deterministic
                :meth:`_summarize` shape — used by unit tests and as the
                fail-soft fallback.
        """
        self.run_query = run_query
        self.cap = cap
        self.render_answer = render_answer

    def execute(
        self,
        *,
        sql: str,
        database_name: str,
        catalog_id: str,
        question: str = "",
        slice_text: str = "",
    ) -> Dict[str, Any]:
        """Execute one grounded SELECT and return a capped result dict.

        Rewrites an unbounded query to ``LIMIT cap+1``, runs it through the
        injected seam, then trims/flags any over-cap result.

        Args:
            sql: The grounded, syntax-checked SELECT.
            database_name: Athena database for execution.
            catalog_id: Athena catalog for execution.
            question: The natural-language question this SQL answers. Handed to
                the injected ``render_answer`` seam so the answer addresses what
                the user actually asked; ignored when no renderer is wired.
            slice_text: The Phase-3 ontology/schema slice the SQL was grounded
                in. Also handed to ``render_answer`` so the renderer's model span
                carries the slice for the SESSION ``SqlGrounded`` judge.

        Returns:
            ``{columns, rows, answer, usage, over_limit, total_row_count}``.
            On an execution error the seam's ``error`` is surfaced under
            ``answer`` and ``error`` (fail loud — never a fabricated result).

        Raises:
            ValueError: If the seam returns something that is not a dict.
        """
        bounded_sql, injected = ensure_limit(sql, cap=self.cap)
        result = self.run_query(
            sql=bounded_sql,
            database_name=database_name,
            catalog_id=catalog_id,
        )
        if not isinstance(result, dict):
            raise ValueError(
                "run_query seam must return a dict with columns/rows; got "
                f"{type(result).__name__}"
            )

        # An execution error short-circuits: surface it verbatim rather than
        # inventing a row set (fail loud).
        if result.get("error"):
            result.setdefault("columns", [])
            result.setdefault("rows", [])
            result.setdefault("answer", f"Query failed: {result['error']}")
            result.setdefault("usage", {})
            result.setdefault("over_limit", False)
            result.setdefault("total_row_count", 0)
            return result

        result = apply_over_limit(result, injected=injected, cap=self.cap)
        result.setdefault("usage", {})
        # The user-facing answer. The injected renderer (a bounded LLM in
        # production) verbalizes the ACTUAL rows — without it the answer was a
        # row-count stub ("The query returned 3 row(s).") that hid a correct
        # result from both the user and the SESSION answer judges. The
        # deterministic :meth:`_summarize` remains the no-renderer / fail-soft
        # shape, so a renderer outage degrades the wording, never the result.
        result.setdefault(
            "answer",
            self._render(result, question=question, slice_text=slice_text),
        )
        return result

    def _render(
        self,
        result: Dict[str, Any],
        *,
        question: str,
        slice_text: str,
    ) -> str:
        """Verbalize ``result`` via the injected renderer, falling back to a stub.

        Args:
            result: The capped result dict (``columns`` / ``rows`` / flags).
            question: The natural-language question the SQL answers.
            slice_text: The Phase-3 slice the SQL was grounded in.

        Returns:
            The natural-language answer. Falls back to :meth:`_summarize` when no
            renderer is wired, when the renderer returns blank, or when it
            raises — an answer-rendering failure must never lose a good result.
        """
        if self.render_answer is None:
            return self._summarize(result)
        try:
            text = self.render_answer(
                question=question,
                columns=result.get("columns", []),
                rows=result.get("rows", []),
                over_limit=bool(result.get("over_limit")),
                usage_sink=result.setdefault("usage", {}),
                slice_text=slice_text,
            )
        except Exception as exc:  # noqa: BLE001 — never break a good result
            logger.warning(
                "answer render failed (%s) — using deterministic summary", exc,
            )
            return self._summarize(result)
        text = (text or "").strip()
        return text or self._summarize(result)

    def _summarize(self, result: Dict[str, Any]) -> str:
        """Build a concise plain-English answer describing the result shape.

        Args:
            result: The capped result dict.

        Returns:
            A 1-sentence summary. Does NOT assert a business-level explanation
            for a 0-row result (per the EXECUTION_PROMPT zero-row rule).
        """
        row_count = len(result.get("rows", []))
        if row_count == 0:
            return (
                "The query returned 0 rows; this may reflect the filter or join "
                "logic rather than a confirmed business fact."
            )
        if result.get("over_limit"):
            return (
                f"The query returned more than {self.cap} rows; showing the "
                f"first {self.cap}."
            )
        return f"The query returned {row_count} row(s)."


def athena_run_query(
    *,
    sql: str,
    database_name: str,
    catalog_id: str,
    s3_output_location: str,
    workgroup: str = "primary",
    region: Optional[str] = None,
    max_wait_seconds: int = 600,
    poll_interval_seconds: int = 2,
) -> Dict[str, Any]:
    """Default execution seam: start + poll one Athena query, parse the result.

    boto3 is imported lazily INSIDE this function so importing the module never
    pulls in AWS — the isolation guard and the generator unit test stay
    cloud-free. Wire this as the ``run_query`` seam of
    :class:`BoundedSqlExecutor` in production.

    Args:
        sql: The grounded, LIMIT-bearing SELECT.
        database_name: Athena database for execution.
        catalog_id: Athena catalog. Standard Glue (``AWSDataCatalog`` / empty)
            omits the catalog from the execution context; any other value (a
            federated or S3 Tables catalog) sets it explicitly.
        s3_output_location: ``s3://…/`` prefix for Athena result output.
        workgroup: Athena workgroup (default ``primary``).
        region: AWS region; ``None`` lets boto3 resolve it from the environment.
        max_wait_seconds: Upper bound on polling for a terminal state.
        poll_interval_seconds: Seconds between status polls.

    Returns:
        ``{columns, rows, query_execution_id}`` on success, or
        ``{error, query_execution_id}`` on a query-level failure/timeout, a
        SQL-firewall denial included.
    """
    # SQL firewall — the last gate before submission, and enforced HERE rather
    # than in BoundedSqlExecutor.execute because main.py's rule tier calls this
    # function directly, bypassing the executor. Gating only the executor would
    # leave that path open.
    #
    # A denial is RETURNED as the standard error dict, not raised: the executor
    # short-circuits on `error` and surfaces it verbatim as the answer, which is
    # how the denial reason reaches the user. There is no repair loop to grind
    # against. `query_execution_id` is empty because nothing was submitted.
    from .firewall_context import enforce_for_caller
    from .sql_firewall import UnsafeSQLError

    try:
        sql = enforce_for_caller(sql=sql)
    except UnsafeSQLError as denial:
        logger.warning("athena_run_query denied by firewall: %s", denial)
        return {"error": f"Query denied: {denial}", "query_execution_id": ""}

    import boto3  # lazy: never import AWS at module load

    client = boto3.client("athena", region_name=region)

    # Standard Glue omits the catalog; a federated / S3 Tables catalog sets it.
    if catalog_id and catalog_id not in ("AWSDataCatalog", "AwsDataCatalog"):
        query_context: Dict[str, str] = {
            "Database": database_name,
            "Catalog": catalog_id,
        }
    else:
        query_context = {"Database": database_name}

    resp = client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext=query_context,
        ResultConfiguration={"OutputLocation": s3_output_location},
        WorkGroup=workgroup,
    )
    query_execution_id = resp["QueryExecutionId"]
    logger.info("athena_run_query submitted: execution_id=%s", query_execution_id)

    # Poll until the query reaches a terminal state or the wait budget elapses.
    elapsed = 0
    while elapsed < max_wait_seconds:
        execution = client.get_query_execution(QueryExecutionId=query_execution_id)
        state = execution["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = execution["QueryExecution"]["Status"].get(
                "StateChangeReason", "Unknown error"
            )
            return {"error": reason, "query_execution_id": query_execution_id}
        # nosemgrep: arbitrary-sleep — intentional polling interval for Athena.
        time.sleep(poll_interval_seconds)
        elapsed += poll_interval_seconds
    else:
        return {
            "error": "Query timed out",
            "query_execution_id": query_execution_id,
        }

    # Paginate the full result set — a single get_query_results call truncates
    # around 1000 rows, so we MUST loop the paginator.
    paginator = client.get_paginator("get_query_results")
    columns: list[str] = []
    rows: list[list[str]] = []
    first_row = True
    for page in paginator.paginate(QueryExecutionId=query_execution_id):
        for row in page["ResultSet"]["Rows"]:
            values = [col.get("VarCharValue", "") for col in row["Data"]]
            # The very first row of the first page is the column header.
            if first_row and not columns:
                columns = values
                first_row = False
                continue
            rows.append(values)

    logger.info("athena_run_query returned %d rows", len(rows))
    return {
        "columns": columns,
        "rows": rows,
        "query_execution_id": query_execution_id,
    }
