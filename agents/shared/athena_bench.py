"""Generic Athena run-and-fetch wrapper for benchmark execution.

Starts a query, polls to completion, and returns rows as tuples plus the engine
execution time — the exact shape ``exec_metrics`` consumes. Distilled from
``metric_executor.execute_metric`` (same polling + federated-catalog context
logic) but generic: arbitrary SQL, no ``Metric`` object.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass
class AthenaResult:
    """Outcome of one Athena query.

    Args:
        columns: Result column names in order.
        rows: Result rows as tuples of string cell values.
        elapsed_ms: Engine execution time in milliseconds (for R-VES).
    """

    columns: List[str]
    rows: List[Tuple]
    elapsed_ms: int


def _context(*, catalog_id: str, database_name: str) -> Dict[str, str]:
    """Build the Athena QueryExecutionContext (federated catalog + database).

    Args:
        catalog_id: Athena catalog; empty/``AwsDataCatalog`` ⇒ default Glue.
        database_name: Database/schema the SQL references.

    Returns:
        A context dict; ``Catalog`` is included only for a federated catalog.
    """
    ctx: Dict[str, str] = {}
    if database_name:
        ctx["Database"] = database_name
    if catalog_id and catalog_id not in ("AWSDataCatalog", "AwsDataCatalog"):
        ctx["Catalog"] = catalog_id
    return ctx


def run_sql(
    *,
    athena: Any,
    sql: str,
    workgroup: str,
    output_loc: str,
    database_name: str = "",
    catalog_id: str = "",
    poll_interval_s: float = 1.0,
    max_wait_s: float = 120.0,
) -> AthenaResult:
    """Execute SQL on Athena and return rows-as-tuples plus elapsed ms.

    Args:
        athena: boto3 ``athena`` client (or stand-in).
        sql: The query to run.
        workgroup: Athena workgroup name.
        output_loc: ``s3://`` result location; empty ⇒ workgroup-enforced.
        database_name: Query-context database/schema.
        catalog_id: Query-context catalog (federated S3 Tables catalog).
        poll_interval_s: Seconds between status polls.
        max_wait_s: Hard ceiling on total polling time.

    Returns:
        An ``AthenaResult``.

    Raises:
        RuntimeError: Athena reports FAILED/CANCELLED (carries the reason).
        TimeoutError: The query did not finish within ``max_wait_s``.
    """
    start_kwargs: Dict[str, Any] = {"QueryString": sql, "WorkGroup": workgroup}
    ctx = _context(catalog_id=catalog_id, database_name=database_name)
    if ctx:
        start_kwargs["QueryExecutionContext"] = ctx
    if output_loc:
        start_kwargs["ResultConfiguration"] = {"OutputLocation": output_loc}
    qid = athena.start_query_execution(**start_kwargs)["QueryExecutionId"]

    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        info = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = info["Status"]["State"]
        if state == "SUCCEEDED":
            elapsed = int(
                info.get("Statistics", {}).get("EngineExecutionTimeInMillis", 0)
            )
            break
        if state in {"FAILED", "CANCELLED"}:
            raise RuntimeError(
                f"Athena {state}: {info['Status'].get('StateChangeReason', state)}"
            )
        time.sleep(poll_interval_s)  # nosemgrep: arbitrary-sleep — Athena poll
    else:
        raise TimeoutError(f"Athena query {qid} unfinished in {max_wait_s}s")

    # Athena returns <=1000 rows per page; loop NextToken to fetch the FULL
    # result set. Only page 1 carries the header row (index 0) — subsequent
    # pages are all data. Silent truncation here would corrupt EX scoring, so
    # we always drain every page.
    cols: List[str] = []
    rows: List[Tuple] = []
    next_token: str = ""
    first_page = True
    while True:
        kwargs: Dict[str, Any] = {"QueryExecutionId": qid}
        if next_token:
            kwargs["NextToken"] = next_token
        page = athena.get_query_results(**kwargs)
        rs = page["ResultSet"]
        if first_page:
            cols = [c["Name"] for c in rs["ResultSetMetadata"]["ColumnInfo"]]
            data_rows = rs["Rows"][1:]  # page 1 row 0 is the header
            first_page = False
        else:
            data_rows = rs["Rows"]
        rows.extend(
            tuple(cell.get("VarCharValue") for cell in r["Data"]) for r in data_rows
        )
        next_token = page.get("NextToken", "") or ""
        if not next_token:
            break
    return AthenaResult(columns=cols, rows=rows, elapsed_ms=elapsed)
