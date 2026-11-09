"""Per-row execution scorer glue for the benchmark eval notebooks.

Given one ``BenchmarkRow``, the agent's predicted SQL, and Athena resolution
info, this module executes BOTH the predicted and gold SQL and computes the
execution-based metrics (EX / R-VES / Soft-F1) from ``exec_metrics``.

Injected run_sql seam
---------------------
Athena execution is supplied as a ``run_sql`` callable rather than importing
boto3 here, so the scorer is unit-testable without any AWS client: the notebook
passes a ``functools.partial(athena_bench.run_sql, athena=..., workgroup=...,
output_loc=...)`` and tests pass a scripted stand-in. The callable is invoked
by keyword only and must return an object exposing ``.rows`` and
``.elapsed_ms`` (i.e. an ``AthenaResult``).

Predicted-first ordering
------------------------
The PREDICTED SQL is executed FIRST, then the gold SQL. This ordering is
deliberate:
  * A broken prediction (syntax error, bad table) is SCORED as a zero rather
    than being fatal to the whole eval run — its exception is captured into the
    returned ``error`` field and the row scores EX=0 / R-VES=0 / Soft-F1=0.
  * When the prediction already failed, gold is NOT executed — running gold
    would only waste an Athena query since the row can no longer earn a
    non-zero score.
Gold SQL failures are NOT swallowed: if a valid prediction runs but gold raises,
the exception propagates (a broken gold query is a benchmark-data bug that must
be fixed loudly, not silently scored as zero).
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from agents.shared.benchmark_adapter import BenchmarkRow
from agents.shared.exec_metrics import execution_accuracy, reward_ves, soft_f1


def score_row(
    *,
    row: BenchmarkRow,
    predicted_sql: str,
    run_sql: Callable[..., Any],
    catalog_id: str,
    database_name: str,
) -> Dict[str, Any]:
    """Execute predicted + gold SQL on Athena and compute EX/R-VES/Soft-F1.

    Args:
        row: The benchmark example; its ``gold_sql`` is the reference query.
        predicted_sql: SQL produced by the agent under evaluation.
        run_sql: Injected Athena execution seam, called by keyword as
            ``run_sql(sql=..., catalog_id=..., database_name=...)`` and returning
            an object with ``.rows`` and ``.elapsed_ms`` (an ``AthenaResult``).
        catalog_id: Athena query-context catalog passed through to ``run_sql``.
        database_name: Athena query-context database passed through to
            ``run_sql``.

    Returns:
        A dict with keys:
            ``ex`` (int): 1 on exact result-set match, else 0.
            ``rves`` (float): Reward-based Valid Efficiency Score (0.0 on any
                failure or incorrect result).
            ``soft_f1`` (float): Soft-F1 over result rows (0.0 on failure).
            ``error`` (str): captured predicted-query error message; "" on
                success.
            ``db_id`` (str): the row's source database id (for aggregation).
            ``question`` (str): the row's question (for reporting).

    Raises:
        ValueError: The row has no gold SQL, so it cannot be execution-scored.
        Exception: Any error raised while executing the GOLD SQL propagates —
            a broken gold query is a benchmark-data bug, not a zero score.
    """
    # Fail loudly: a row with no gold SQL cannot be execution-scored at all.
    if not row.gold_sql:
        raise ValueError(
            f"cannot score row without gold SQL (question={row.question!r})"
        )

    # Predicted-first: run the (possibly broken) prediction before gold so a bad
    # prediction is captured as a zero-scored row instead of aborting the run.
    try:
        pred_result = run_sql(
            sql=predicted_sql,
            catalog_id=catalog_id,
            database_name=database_name,
        )
    except Exception as exc:  # noqa: BLE001 — any execution failure => zero score
        # Prediction failed: score zeros and DO NOT touch gold (it would only
        # waste an Athena query — the row can no longer earn a non-zero score).
        return {
            "ex": 0,
            "rves": 0.0,
            "soft_f1": 0.0,
            "error": str(exc),
            "db_id": row.db_id,
            "question": row.question,
        }

    # Prediction succeeded — now run gold. A gold failure propagates (loud).
    gold_result = run_sql(
        sql=row.gold_sql,
        catalog_id=catalog_id,
        database_name=database_name,
    )

    ex = execution_accuracy(pred_rows=pred_result.rows, gold_rows=gold_result.rows)
    sf1 = soft_f1(pred_rows=pred_result.rows, gold_rows=gold_result.rows)
    # Guard against a zero timing: Athena can report 0 ms for a cached/instant
    # query, but reward_ves raises on non-positive timings. Clamp to >= 1 ms so a
    # legitimately instant query still scores instead of blowing up the run.
    rves = reward_ves(
        pred_rows=pred_result.rows,
        gold_rows=gold_result.rows,
        pred_ms=max(pred_result.elapsed_ms, 1),
        gold_ms=max(gold_result.elapsed_ms, 1),
    )

    return {
        "ex": ex,
        "rves": rves,
        "soft_f1": sf1,
        "error": "",
        "db_id": row.db_id,
        "question": row.question,
    }
