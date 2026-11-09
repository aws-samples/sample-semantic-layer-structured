"""Athena column-value probing for the thin VKG builder.

Distinct-value probing (build-time enum discovery) copied in shape from
``ontology_agent.main.select_distinct_values``: run a
``SELECT <col>, COUNT(*) GROUP BY <col> ORDER BY count DESC LIMIT n+1`` query so
a true enum returns within the cap while a high-cardinality column trips the
truncation flag.

boto3 is kept behind an injected ``run_query`` seam so callers (and tests) can
supply their own query executor with no AWS access at import time. When no seam
is injected, a boto3-backed default executor is lazily constructed on first use
— never at import time.

Copy-and-own: standard library + a lazily-imported ``boto3`` only. No import
from ``agents.shared`` or any sibling agent package (enforced by
``tests/unit/test_hybrid_isolation.py``).
"""

from typing import Callable, Dict, List, Optional, Sequence

# Default cardinality cap for enum discovery. A column with more than this many
# distinct values is treated as high-cardinality (not an enum) and truncated.
DEFAULT_MAX_CARDINALITY: int = 50

# A run_query seam takes a fully-formed SQL string and returns a list of row
# dicts (column-name -> value). This mirrors what the ontology_agent's
# _run_athena_query yields after result-set parsing.
RunQuery = Callable[[str], List[Dict[str, object]]]


def _build_default_run_query(
    *,
    database: str,
    catalog: str,
    workgroup: str,
    output_location: Optional[str],
) -> RunQuery:
    """Construct a boto3-backed ``run_query`` executor (lazy — no import-time AWS).

    The returned callable starts an Athena query, polls to completion, and
    parses the result set into a list of row dicts. boto3 is imported inside
    this function so importing the module never touches AWS.

    Args:
        database: Glue/Athena database the query runs against.
        catalog: Athena data-catalog name.
        workgroup: Athena workgroup name.
        output_location: Optional ``s3://`` result location; when omitted the
            workgroup's configured output location is used.

    Returns:
        A ``RunQuery`` callable that executes SQL and returns row dicts.

    Raises:
        RuntimeError: If a submitted query does not reach ``SUCCEEDED``.
    """
    import time

    import boto3  # Lazy import: never at module import time.

    athena = boto3.client("athena")

    def _run(query: str) -> List[Dict[str, object]]:
        """Execute *query* on Athena and return parsed row dicts."""
        start_kwargs: Dict[str, object] = {
            "QueryString": query,
            "QueryExecutionContext": {"Database": database, "Catalog": catalog},
            "WorkGroup": workgroup,
        }
        if output_location:
            start_kwargs["ResultConfiguration"] = {"OutputLocation": output_location}

        query_id = athena.start_query_execution(**start_kwargs)["QueryExecutionId"]

        # Poll until the query terminates. Fail loud on non-success states.
        while True:
            status = athena.get_query_execution(QueryExecutionId=query_id)[
                "QueryExecution"
            ]["Status"]["State"]
            if status in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(1)
        if status != "SUCCEEDED":
            raise RuntimeError(
                f"Athena query {query_id} ended in state {status}: {query!r}"
            )

        # Parse the result set: the first row is the column header. Loop
        # NextToken so large result sets are not truncated.
        rows: List[Dict[str, object]] = []
        headers: List[str] = []
        next_token: Optional[str] = None
        first_page = True
        while True:
            page_kwargs: Dict[str, object] = {"QueryExecutionId": query_id}
            if next_token:
                page_kwargs["NextToken"] = next_token
            page = athena.get_query_results(**page_kwargs)
            result_rows = page["ResultSet"]["Rows"]
            for idx, row in enumerate(result_rows):
                cells = [cell.get("VarCharValue") for cell in row.get("Data", [])]
                if first_page and idx == 0:
                    headers = [c or "" for c in cells]
                    continue
                rows.append(dict(zip(headers, cells)))
            first_page = False
            next_token = page.get("NextToken")
            if not next_token:
                break
        return rows

    return _run


def probe_unique_columns(
    *,
    table: str,
    columns: Sequence[str],
    database: str,
    catalog: str,
    run_query: Optional[RunQuery] = None,
    workgroup: str = "primary",
    output_location: Optional[str] = None,
) -> List[str]:
    """Measure which of ``columns`` is a VERIFIED single-column key of ``table``.

    A column qualifies when, over the FULL table,
    ``COUNT(DISTINCT col) = COUNT(col) = COUNT(*)``. Both equalities are required:
    ``COUNT(DISTINCT)`` ignores NULLs, so a column with one NULL and otherwise
    distinct values would pass the distinctness test while being unusable as row
    identity (Ontop cannot template a NULL into an IRI and those rows would
    silently vanish from the class).

    This measurement is the ONLY sound basis for declaring ``vkg:hasPrimaryKey``:
    the declaration is also a licence for Ontop to drop de-duplication, so it must
    never be inferred from a column NAME (``*_id`` is measurably non-unique on the
    many side of a relationship). Every candidate is measured in ONE query so a
    layer costs one Athena scan per table, not one per column.

    Args:
        table: Physical table name.
        columns: Candidate column names to measure (typically the columns other
            tables reference — see ``fk_inference.key_columns_by_table``).
        database: Glue/Athena database name.
        catalog: Athena data-catalog name.
        run_query: Injected query-executor seam. When ``None`` a boto3-backed
            default is lazily built (no AWS access happens at import time).
        workgroup: Athena workgroup (only used by the default executor).
        output_location: Optional S3 result location (default executor only).

    Returns:
        list[str]: The subset of ``columns`` measured unique-and-NOT-NULL, in the
            input order. Empty when nothing qualifies — the caller must then
            declare NO key at all rather than fall back to a name guess.

    Raises:
        RuntimeError: Propagated from the query executor when the probe itself
            fails. Callers decide the posture: a failed measurement means "key
            unknown", which is a degraded build, not a wrong one.
    """
    if not columns:
        return []
    if run_query is None:
        run_query = _build_default_run_query(
            database=database,
            catalog=catalog,
            workgroup=workgroup,
            output_location=output_location,
        )

    # One aggregate pair per candidate, plus a single COUNT(*) for the row total.
    # Aliases are positional (d_0/nn_0) so a column name can never collide with an
    # alias or need quoting in the label.
    selects: List[str] = ["COUNT(*) AS n"]
    for index, column in enumerate(columns):
        selects.append(f'COUNT(DISTINCT "{column}") AS d_{index}')
        selects.append(f'COUNT("{column}") AS nn_{index}')
    # Column/table names come from the Glue catalog, not user free-text.
    query = (  # nosec B608
        f'SELECT {", ".join(selects)} FROM "{database}"."{table}"'
    )
    rows = run_query(query)
    if not rows:
        raise RuntimeError(
            f"uniqueness probe for {database}.{table} returned no rows; refusing "
            f"to treat an empty result as 'not unique' — that would silently "
            f"downgrade row identity to a name guess"
        )

    row = rows[0]
    total = int(row["n"])
    unique: List[str] = []
    for index, column in enumerate(columns):
        distinct = int(row[f"d_{index}"])
        non_null = int(row[f"nn_{index}"])
        if total > 0 and distinct == total and non_null == total:
            unique.append(column)
    return unique


def probe_column_values(
    *,
    table: str,
    column: str,
    database: str,
    catalog: str,
    run_query: Optional[RunQuery] = None,
    limit: int = DEFAULT_MAX_CARDINALITY,
    workgroup: str = "primary",
    output_location: Optional[str] = None,
) -> Dict[str, object]:
    """Probe a column's distinct sample values with a cardinality cap.

    Runs ``SELECT <col> AS v, COUNT(*) AS c GROUP BY <col> ORDER BY c DESC
    LIMIT <limit>+1`` so a true enum returns within the cap while a
    high-cardinality column trips the ``truncated`` flag.

    Args:
        table: Physical table name.
        column: Column to probe.
        database: Glue/Athena database name.
        catalog: Athena data-catalog name.
        run_query: Injected query-executor seam. When ``None`` a boto3-backed
            default is lazily built (no AWS access happens at import time).
        limit: Cardinality cap; up to ``limit`` distinct values are returned.
        workgroup: Athena workgroup (only used by the default executor).
        output_location: Optional S3 result location (default executor only).

    Returns:
        ``{"values": List[str], "distinct_count": int, "truncated": bool}`` —
        ``values`` are the distinct sample values (capped at ``limit``).
    """
    if run_query is None:
        run_query = _build_default_run_query(
            database=database,
            catalog=catalog,
            workgroup=workgroup,
            output_location=output_location,
        )

    # Fetch one extra row beyond the cap so we can detect truncation.
    # Column/table names come from the Glue catalog, not user free-text.
    query = (
        f'SELECT "{column}" AS v, COUNT(*) AS c '  # nosec B608
        f'FROM "{database}"."{table}" '
        f'GROUP BY "{column}" ORDER BY c DESC LIMIT {limit + 1}'
    )
    rows = run_query(query)

    values: List[str] = [
        str(row["v"]) for row in rows if row.get("v") is not None
    ]
    truncated = len(values) > limit
    if truncated:
        values = values[:limit]

    return {
        "values": values,
        "distinct_count": len(values),
        "truncated": truncated,
    }
