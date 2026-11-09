"""Generic Snowflake run-and-fetch wrapper for benchmark execution.

The direct-connection sibling of ``athena_bench``: same call shape, same result
contract (``columns`` / ``rows`` / ``elapsed_ms``), so ``exec_metrics`` scores a
Snowflake run exactly as it scores an Athena run and the two are swappable at the
call site.

WHY a second executor exists at all. Everything else in this repo reaches SQL
through Athena, and Snowflake is also registered there as a federated catalog
(``snowflake_<database>``), which is the right transport for product queries —
it is the only way to join Snowflake to the lake, and it needs no agent changes.
It is the WRONG transport for Spider 2.0-snow:

* **It would change the dialect under test.** Spider 2.0-snow's gold SQL *is*
  Snowflake SQL. Routing it through Athena means transpiling Snowflake→Trino,
  where ``QUALIFY``, ``LATERAL FLATTEN`` over ``VARIANT`` and several date
  functions have no clean equivalent. Every transpile failure is an item that
  cannot be scored, so the transport injects measurement error into the metric —
  and what gets measured is the agent's Trino generation, not its Snowflake
  generation.
* **One catalog is bound to one Snowflake database** (a connector's connection
  string pins exactly one ``db=``), so N benchmark databases mean N connector
  Lambdas and N catalogs — roughly 4-5 CloudFormation resources each, against a
  500-resource stack ceiling. The CDK stack does support this (see
  ``SNOWFLAKE_DATABASES`` in ``cdk/README.md``); it is the per-database cost that
  makes it wrong for a benchmark sweep, not the lack of support.
* **Spider 2.0's query profile is the worst case for federation**: long, nested,
  aggregate- and window-heavy. The JDBC connectors push down projections and
  simple predicates only, so joins and aggregates run in Athena and Snowflake
  ships raw rows through a Lambda into S3 spill.

Nothing here duplicates the connector's credential setup: this module reads the
SAME Secrets Manager secret the Athena connector uses, written by
``npm run sync:snowflake-secret`` from ``cdk/`` — one credential to rotate, not two.

TIMING IS NOT DIRECTLY COMPARABLE TO ``athena_bench`` BY DEFAULT. Athena reports
``EngineExecutionTimeInMillis`` — server-side engine time, excluding queueing and
result fetch. The default here is client wall-clock, which includes the round
trip and row fetch, so it reads slower for the same work. Pass
``server_stats=True`` for Snowflake's own server-side ``EXECUTION_TIME`` (plus
bytes scanned) when the comparison has to be apples-to-apples — e.g. R-VES, which
is a speed metric.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Snowflake object names are folded to upper case unless quoted, and the benchmark
# manifests carry them in mixed case. Rather than quote (which would force exact
# case and break a lower-cased manifest entry against an upper-case Snowflake
# object), identifiers are validated against this pattern and interpolated bare so
# Snowflake applies its own folding. Anything else is refused — `USE DATABASE` and
# `USE SCHEMA` take an identifier, not a bind parameter, so there is no
# placeholder to bind and an unvalidated value would be injectable.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_$]+$")


@dataclass
class SnowflakeResult:
    """Outcome of one Snowflake query.

    Field-for-field compatible with ``athena_bench.AthenaResult`` for the first
    three attributes, so callers and ``exec_metrics`` treat them alike.

    Args:
        columns: Result column names in order.
        rows: Result rows as tuples of native Python cell values.
        elapsed_ms: Query time in milliseconds. Client wall-clock by default;
            Snowflake's server-side ``EXECUTION_TIME`` when ``server_stats=True``
            and the lookup succeeded (see ``server_time`` for which one it is).
        query_id: Snowflake query id, for correlating against QUERY_HISTORY.
        server_time: True when ``elapsed_ms`` came from Snowflake server-side
            stats; False when it is client wall-clock. Never guess which — R-VES
            comparisons against Athena are only valid on server time.
        bytes_scanned: Bytes scanned per Snowflake's own accounting, or None when
            stats were not requested or unavailable.
        credits_cloud_services: CLOUD-SERVICES credits only. This is NOT the
            query's compute cost: warehouse compute credits are not attributed
            per query by Snowflake, only per warehouse per hour via
            ``SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY``. Treat this as
            a lower bound, never as "what the query cost".
    """

    columns: List[str]
    rows: List[Tuple]
    elapsed_ms: int
    query_id: str = ""
    server_time: bool = False
    bytes_scanned: Optional[int] = None
    credits_cloud_services: Optional[float] = None


def _private_key_der(*, pem: str, passphrase: str = "") -> bytes:
    """Decode a PEM private key to the DER bytes the Snowflake driver wants.

    ``snowflake-connector-python`` takes ``private_key`` as DER (PKCS#8,
    unencrypted), so the encrypted PEM held in Secrets Manager is decrypted here
    and never written to disk.

    Args:
        pem: PEM text, encrypted ("BEGIN ENCRYPTED PRIVATE KEY") or not.
        passphrase: Passphrase for an encrypted PEM; empty for an unencrypted one.

    Returns:
        DER-encoded PKCS#8 private key bytes.

    Raises:
        ValueError: The PEM could not be loaded (usually a wrong passphrase).
    """
    # Imported here, not at module scope, so importing this module (and unit-testing
    # run_sql against a stub connection) needs neither cryptography nor the
    # Snowflake driver installed.
    from cryptography.hazmat.primitives import serialization

    try:
        key = serialization.load_pem_private_key(
            pem.encode("utf-8"),
            password=passphrase.encode("utf-8") if passphrase else None,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Could not load the Snowflake private key: {exc}. An encrypted PEM "
            "needs pem_private_key_passphrase in the secret."
        ) from exc
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def load_credentials(*, secret_id: str, region: str = "") -> Dict[str, str]:
    """Read the Snowflake key-pair credential out of Secrets Manager.

    Reads the exact payload ``npm run sync:snowflake-secret`` writes, which is
    also what the Athena federated connector consumes — the same secret serves
    both transports, so there is one credential to rotate, not two.

    Args:
        secret_id: Secret name or ARN, e.g. ``semantic-layer/snowflake/connector``.
        region: AWS region; empty uses the ambient boto3 default.

    Returns:
        The parsed secret: ``username`` / ``sfUser``, ``pem_private_key`` and
        optionally ``pem_private_key_passphrase``.

    Raises:
        ValueError: The secret holds no usable key material — which is what an
            un-populated placeholder looks like, so it is called out explicitly
            rather than surfacing later as an opaque driver error.
    """
    import json

    import boto3

    client = boto3.client("secretsmanager", **({"region_name": region} if region else {}))
    raw = client.get_secret_value(SecretId=secret_id)["SecretString"]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        # CDK deliberately never writes a real key into this secret, so an unsynced
        # secret holds a placeholder rather than credentials. Kept for secrets created
        # by other means (e.g. a bare-string placeholder); this stack's placeholder is
        # valid JSON, so it falls through to the field check below instead.
        raise ValueError(
            f"Secret {secret_id} is not JSON — it still holds a placeholder. "
            "Run `npm run sync:snowflake-secret` from cdk/ first."
        ) from exc
    if not payload.get("pem_private_key"):
        # This is the path the CDK placeholder document actually takes.
        raise ValueError(
            f"Secret {secret_id} has no 'pem_private_key' field — it still holds the "
            "CDK placeholder. Run `npm run sync:snowflake-secret` from cdk/ first."
        )
    return payload


def connect(
    *,
    account: str,
    warehouse: str,
    secret_id: str = "",
    credentials: Optional[Dict[str, str]] = None,
    region: str = "",
    database: str = "",
    schema: str = "",
    role: str = "",
) -> Any:
    """Open a key-pair-authenticated Snowflake connection.

    Args:
        account: Snowflake account identifier, e.g. ``elphbmx-awspartner``.
        warehouse: Warehouse to run in. Snowflake fails every query with no
            warehouse in session context, so this is required.
        secret_id: Secrets Manager secret to read credentials from. Ignored when
            ``credentials`` is supplied.
        credentials: Pre-fetched credential dict (see ``load_credentials``), to
            avoid a Secrets Manager call per connection across a benchmark sweep.
        region: AWS region for the Secrets Manager lookup.
        database: Optional initial database.
        schema: Optional initial schema.
        role: Optional role to assume.

    Returns:
        An open ``snowflake.connector`` connection.

    Raises:
        ValueError: Neither ``secret_id`` nor ``credentials`` was given.
    """
    if credentials is None:
        if not secret_id:
            raise ValueError("Pass either secret_id or credentials.")
        credentials = load_credentials(secret_id=secret_id, region=region)

    import snowflake.connector  # lazy: benchmark-only dependency

    # `sfUser` is the spelling the Athena docs use and what sync-snowflake-secret
    # writes for key-pair mode; `username` is what the connector's own credentials
    # provider reads and what password mode writes. Accept either — which spelling is
    # present depends on the auth mode the secret was synced in.
    user = credentials.get("username") or credentials.get("sfUser", "")
    kwargs: Dict[str, Any] = {
        "account": account,
        "user": user,
        "warehouse": warehouse,
        "private_key": _private_key_der(
            pem=credentials["pem_private_key"],
            passphrase=credentials.get("pem_private_key_passphrase", ""),
        ),
    }
    for name, value in (("database", database), ("schema", schema), ("role", role)):
        if value:
            kwargs[name] = value
    return snowflake.connector.connect(**kwargs)


def _use_context(*, cursor: Any, database: str, schema: str) -> None:
    """Point the session at a database/schema — the Snowflake analogue of Athena's
    ``QueryExecutionContext``.

    Args:
        cursor: An open cursor on the connection.
        database: Database to select; skipped when empty.
        schema: Schema to select; skipped when empty.

    Raises:
        ValueError: An identifier contains anything outside ``[A-Za-z0-9_$]``.
    """
    for kind, name in (("DATABASE", database), ("SCHEMA", schema)):
        if not name:
            continue
        if not _IDENTIFIER.match(name):
            raise ValueError(
                f"Refusing to USE {kind} {name!r}: not a bare Snowflake identifier. "
                "USE takes an identifier, which cannot be passed as a bind parameter."
            )
        cursor.execute(f"USE {kind} {name}")


def _server_stats(*, conn: Any, query_id: str) -> Dict[str, Any]:
    """Look up Snowflake's own execution stats for a finished query.

    Uses the ``QUERY_HISTORY_BY_SESSION`` table function rather than
    ``SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY``: the ACCOUNT_USAGE view lags by up
    to ~45 minutes and needs extra grants, which makes it useless inside a
    benchmark loop.

    Args:
        conn: The connection the query ran on (same session).
        query_id: The query id to look up.

    Returns:
        ``{execution_time_ms, bytes_scanned, credits_cloud_services}``, or ``{}``
        when the row is not available — the stats row can lag the query slightly,
        and INFORMATION_SCHEMA needs a database in session context. Callers fall
        back to wall-clock rather than failing the benchmark row over telemetry.
    """
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT execution_time, bytes_scanned, credits_used_cloud_services "
                "FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION("
                "  RESULT_LIMIT => 100)) WHERE query_id = %s",
                (query_id,),
            )
            row = cur.fetchone()
        finally:
            cur.close()
    except Exception:  # noqa: BLE001 — stats are best-effort, never fatal
        return {}
    if not row:
        return {}
    return {
        "execution_time_ms": int(row[0]) if row[0] is not None else None,
        "bytes_scanned": int(row[1]) if row[1] is not None else None,
        "credits_cloud_services": float(row[2]) if row[2] is not None else None,
    }


def run_sql(
    *,
    conn: Any,
    sql: str,
    database_name: str = "",
    schema_name: str = "",
    max_wait_s: float = 120.0,
    fetch_size: int = 1000,
    server_stats: bool = False,
) -> SnowflakeResult:
    """Execute SQL on Snowflake and return rows-as-tuples plus elapsed ms.

    Mirrors ``athena_bench.run_sql``, including its error contract: a query that
    fails raises ``RuntimeError`` and one that outruns ``max_wait_s`` raises
    ``TimeoutError``, so a caller can swap executors without reworking its
    exception handling.

    Cell values are the driver's native Python types (int/Decimal/datetime/None)
    rather than Athena's strings. ``exec_metrics._norm_cell`` already handles both
    — numerics compare by rounded float value, bools case-fold — but note that
    date/time cells render differently across the two engines, so gold and
    predicted SQL for one row must execute through the SAME executor.

    Args:
        conn: An open Snowflake connection (or a stand-in exposing ``cursor()``).
        sql: The query to run.
        database_name: Session database, applied via ``USE DATABASE``.
        schema_name: Session schema, applied via ``USE SCHEMA``.
        max_wait_s: Server-side statement timeout; Snowflake cancels past it.
        fetch_size: Rows per ``fetchmany`` batch.
        server_stats: Also fetch Snowflake's server-side execution time and bytes
            scanned, and report that time in ``elapsed_ms``. Costs one extra
            round trip per query.

    Returns:
        A ``SnowflakeResult``.

    Raises:
        RuntimeError: Snowflake rejected or failed the query.
        TimeoutError: The query was cancelled for exceeding ``max_wait_s``.
    """
    cursor = conn.cursor()
    started = time.time()
    try:
        _use_context(cursor=cursor, database=database_name, schema=schema_name)
        try:
            # The driver enforces the timeout server-side and cancels the query,
            # surfacing errno 604 ("SQL execution canceled").
            cursor.execute(sql, timeout=int(max_wait_s))
        except Exception as exc:  # noqa: BLE001 — normalized to athena_bench's contract
            if getattr(exc, "errno", None) == 604 or "canceled" in str(exc).lower():
                raise TimeoutError(
                    f"Snowflake query unfinished in {max_wait_s}s: {exc}"
                ) from exc
            raise RuntimeError(f"Snowflake error: {exc}") from exc

        # cursor.description is (name, type_code, ...) per column; only the name is
        # needed to match Athena's ResultSetMetadata handling.
        columns = [d[0] for d in (cursor.description or [])]

        # Drain EVERY batch. Athena's sibling loops NextToken for the same reason:
        # a silently truncated result set corrupts EX scoring, turning a correct
        # answer into a mismatch that looks like a model failure.
        rows: List[Tuple] = []
        while True:
            batch = cursor.fetchmany(fetch_size)
            if not batch:
                break
            rows.extend(tuple(r) for r in batch)

        elapsed_ms = int((time.time() - started) * 1000)
        query_id = str(getattr(cursor, "sfqid", "") or "")
    finally:
        cursor.close()

    result = SnowflakeResult(
        columns=columns, rows=rows, elapsed_ms=elapsed_ms, query_id=query_id
    )
    if server_stats and query_id:
        stats = _server_stats(conn=conn, query_id=query_id)
        result.bytes_scanned = stats.get("bytes_scanned")
        result.credits_cloud_services = stats.get("credits_cloud_services")
        server_ms = stats.get("execution_time_ms")
        if server_ms is not None:
            # Only now is elapsed_ms comparable with Athena's
            # EngineExecutionTimeInMillis; server_time records which clock it is so
            # a mixed sweep cannot silently compare wall-clock against engine time.
            result.elapsed_ms = server_ms
            result.server_time = True
    return result
