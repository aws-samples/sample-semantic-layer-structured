"""Run a governed metric's compiled SQL on Athena and shape rows.

Filters and dimensions are applied as ``WHERE`` predicates appended to
the compiled SQL via sqlglot AST manipulation — never string concatenation
— so user-controlled keys can't escape the parser.
"""
from __future__ import annotations

import time
from typing import Any, Dict

import sqlglot
from sqlglot import exp

try:  # Container layouts differ: flat (`shared.*`) vs repo-rooted (`agents.shared.*`).
    from agents.shared.firewall_context import check_metric_allowed, enforce_for_caller
except ImportError:  # pragma: no cover - exercised only inside the agent images
    from shared.firewall_context import (  # type: ignore
        check_metric_allowed,
        enforce_for_caller,
    )


def _apply_filters(sql: str, dialect: str, filters: Dict[str, Any]) -> str:
    """Return ``sql`` with each ``filters`` entry appended as a WHERE predicate.

    The predicate is built through the sqlglot AST so user-supplied column
    names and values cannot smuggle additional SQL into the statement.
    """
    if not filters:
        return sql
    tree = sqlglot.parse_one(sql, read=dialect)
    for col, val in filters.items():
        cond = exp.EQ(
            this=exp.column(col),
            expression=exp.Literal.string(str(val)),
        )
        tree = tree.where(cond, copy=False)
    return tree.sql(dialect=dialect)


def _build_query_context(*, catalog_id: str, database_name: str) -> Dict[str, str]:
    """Build the Athena ``QueryExecutionContext`` for a metric query.

    Mirrors the metadata-query agent's ``execute_sql_query`` logic: a federated
    catalog (S3 Tables ``s3tablescatalog/<bucket>`` or any non-Glue catalog)
    MUST be named in the context, otherwise Athena resolves the metric SQL's
    schema (e.g. ``normalized``) against the default ``AwsDataCatalog`` and
    fails with ``SCHEMA_NOT_FOUND``. Standard Glue catalogs need only the
    database.

    Args:
        catalog_id: Athena catalog the metric's tables live in (e.g.
            ``s3tablescatalog/<bucket>``). Empty/``AwsDataCatalog`` ⇒ default.
        database_name: Athena database/schema the metric SQL references.

    Returns:
        A ``QueryExecutionContext`` dict — always carries ``Database`` when
        given; carries ``Catalog`` only for a non-default federated catalog.
    """
    context: Dict[str, str] = {}
    if database_name:
        context["Database"] = database_name
    if catalog_id and catalog_id not in ("AWSDataCatalog", "AwsDataCatalog"):
        context["Catalog"] = catalog_id
    return context


def execute_metric(*, metric, filters: Dict[str, Any], athena,
                   workgroup: str, output_loc: str,
                   catalog_id: str = "", database_name: str = "",
                   poll_interval_s: float = 1.0,
                   max_wait_s: float = 60.0) -> Dict[str, Any]:
    """Execute the metric's compiled SQL on Athena and return shaped rows.

    Args:
        metric: A ``Metric`` (or duck-typed) object with ``compiled_sql``,
            ``dialect``, ``metric_id``, and ``supported_filters``.
        filters: Mapping of filter column → value to inject as WHERE
            predicates. Keys must all be in ``metric.supported_filters``.
        athena: boto3 ``athena`` client (or stand-in).
        workgroup: Athena workgroup name.
        output_loc: ``s3://`` URI for the Athena result configuration. Empty ⇒
            rely on the workgroup's enforced result location (no
            ``ResultConfiguration`` is sent).
        catalog_id: Athena catalog the metric's tables live in (e.g. an S3
            Tables ``s3tablescatalog/<bucket>`` federated catalog). REQUIRED for
            federated catalogs — without it the metric SQL's schema resolves
            against the default ``AwsDataCatalog`` and fails ``SCHEMA_NOT_FOUND``.
        database_name: Athena database/schema for the query execution context.
        poll_interval_s: Seconds between Athena status polls.
        max_wait_s: Hard ceiling on total polling time.

    Returns:
        ``{"columns": [...], "rows": [{col: val, ...}], "metric_id": ...}``.

    Raises:
        ValueError: A filter key is not in ``metric.supported_filters``.
        PermissionError: The caller's grant profile declares ``allowedMetrics``
            and this metric is not in it.
        UnsafeSQLError: The compiled statement failed the SQL firewall's
            structural checks, or the caller's profile denies a table or column
            it references.
        RuntimeError: Athena reports ``FAILED`` or ``CANCELLED``.
        TimeoutError: The query did not finish within ``max_wait_s``.
    """
    bad = [k for k in filters if k not in metric.supported_filters]
    if bad:
        raise ValueError(f"unsupported filter(s): {bad}")

    # A metric is authorized by identity, not by the shape of its SQL, so this
    # gate is separate from the firewall below. Checked before the filters are
    # applied — no point parsing SQL the caller may not run at all.
    check_metric_allowed(metric_id=metric.metric_id)

    sql = _apply_filters(metric.compiled_sql, metric.dialect, filters)
    # Firewall the FINAL statement, after the filters are merged in. The
    # predicates are AST-built rather than concatenated, but the whole point of
    # the firewall is to be the last gate before start_query_execution, so it
    # validates what actually runs. Parsed in the metric's own dialect —
    # `athena` would reject a statement compiled for another engine as
    # unparseable, which inside a restrictive profile means denied.
    sql = enforce_for_caller(sql=sql, dialect=metric.dialect)
    # Name the federated catalog (S3 Tables) in the execution context so Athena
    # resolves the metric SQL's schema against it rather than the default Glue
    # catalog. This is the Tier 1 analogue of execute_sql_query's context.
    query_context = _build_query_context(
        catalog_id=catalog_id, database_name=database_name,
    )
    start_kwargs: Dict[str, Any] = {"QueryString": sql, "WorkGroup": workgroup}
    if query_context:
        start_kwargs["QueryExecutionContext"] = query_context
    # Only send a result location when we have one; an empty OutputLocation
    # would override the workgroup's enforced location with an invalid value.
    if output_loc:
        start_kwargs["ResultConfiguration"] = {"OutputLocation": output_loc}
    start = athena.start_query_execution(**start_kwargs)
    qid = start["QueryExecutionId"]

    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        info = athena.get_query_execution(QueryExecutionId=qid)
        state = info["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in {"FAILED", "CANCELLED"}:
            reason = info["QueryExecution"]["Status"].get(
                "StateChangeReason", state,
            )
            raise RuntimeError(f"Athena {state}: {reason}")
        time.sleep(poll_interval_s)  # nosemgrep: arbitrary-sleep — intentional Athena poll interval
    else:
        raise TimeoutError(
            f"Athena query {qid} did not finish in {max_wait_s}s"
        )

    # Athena returns <=1000 rows per page; loop NextToken to fetch the FULL result
    # set. Only page 1 carries the header row (index 0). A single call would
    # silently truncate a metric returning >1000 rows.
    cols: list = []
    rows: list = []
    next_token = ""
    first_page = True
    while True:
        page_kwargs: Dict[str, Any] = {"QueryExecutionId": qid}
        if next_token:
            page_kwargs["NextToken"] = next_token
        page = athena.get_query_results(**page_kwargs)
        rs = page["ResultSet"]
        if first_page:
            cols = [c["Name"] for c in rs["ResultSetMetadata"]["ColumnInfo"]]
            data_rows = rs["Rows"][1:]  # row 0 is the column header
            first_page = False
        else:
            data_rows = rs["Rows"]
        rows.extend(
            {cols[i]: cell.get("VarCharValue") for i, cell in enumerate(r["Data"])}
            for r in data_rows
        )
        next_token = page.get("NextToken", "") or ""
        if not next_token:
            break
    return {"columns": cols, "rows": rows, "metric_id": metric.metric_id}
