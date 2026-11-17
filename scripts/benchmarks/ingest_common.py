"""Shared helpers for benchmark ingestion pipelines.

Pure/local building blocks used by ``ingest_bird.py`` / ``ingest_cwd.py`` /
``ingest_spider2.py``: SQLite introspection + Parquet export, gold-SQL transpile
(SQLite->Trino/Athena) with failure CAPTURE (never silent-drop), and a
``manifest.json`` model mapping db_id -> Athena location. The AWS side
(S3 upload + Athena CTAS) is added in a later task.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pyarrow as pa
import pyarrow.parquet as pq
import sqlglot

# A safe SQL identifier: letter/underscore start, then alnum/underscore. Benchmark
# table/column names come from downloaded third-party data, so every name that is
# interpolated into DDL is validated against this first (fail loudly on violation)
# -- the repo convention (see metric_executor.py) is never to build SQL from
# unvalidated names.
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def safe_ident(*, name: str) -> str:
    """Return ``name`` if it is a safe SQL identifier, else raise.

    Args:
        name: A table or column name sourced from benchmark data.

    Returns:
        The unchanged ``name`` when it matches ``_SAFE_IDENT``.

    Raises:
        ValueError: The name is not a plain ``[A-Za-z_][A-Za-z0-9_]*`` identifier
            (fail loudly rather than emit injectable/broken DDL).
    """
    if not _SAFE_IDENT.match(name or ""):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


# A column name is emitted ONLY as (a) a pyarrow Parquet column name and (b) a
# BACKTICK-quoted Hive DDL identifier, so it may contain spaces/punctuation that a
# bare SQL identifier cannot. We reject only what backtick-quoting cannot make
# safe: a backtick itself, control chars, or an empty name. Table/db/namespace
# names, by contrast, are used UNQUOTED and MUST use the strict `safe_ident`.
_UNSAFE_COLUMN = re.compile(r"[`\x00-\x1f]")


def safe_column(*, name: str) -> str:
    """Return ``name`` if safe to backtick-quote as a column, else raise.

    Args:
        name: A column name sourced from benchmark data (may contain spaces).

    Returns:
        The unchanged ``name`` when it has no backtick / control char and is non-empty.

    Raises:
        ValueError: The name is empty or contains a backtick / control character
            (which backtick-quoting cannot neutralize).
    """
    if not name or _UNSAFE_COLUMN.search(name):
        raise ValueError(f"unsafe column name: {name!r}")
    return name


def sqlite_table_names(*, db_path: Path) -> List[str]:
    """List user table names in a SQLite database.

    Args:
        db_path: Path to a ``.sqlite`` file.

    Returns:
        Table names, excluding SQLite internal ``sqlite_%`` tables, sorted.

    Raises:
        FileNotFoundError: ``db_path`` does not exist. sqlite3.connect() would
            otherwise silently create an empty database and return ``[]`` -- a
            silent fallback that violates the repo's fail-loud convention.
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def transpile_gold(*, gold_sql: str, read: str, write: str) -> Tuple[bool, str, str]:
    """Transpile a gold SQL string between dialects, capturing failures.

    Args:
        gold_sql: The source SQL (benchmark gold query).
        read: Source dialect (e.g. ``"sqlite"``).
        write: Target dialect (e.g. ``"trino"`` for Athena).

    Returns:
        ``(ok, transpiled_sql, error)``. On failure ``ok`` is False, the SQL is
        the original (unchanged), and ``error`` carries the reason -- the caller
        records this in the manifest and excludes the row (design: log, never
        silently drop).
    """
    try:
        out = sqlglot.transpile(gold_sql, read=read, write=write)[0]
        return True, out, ""
    except Exception as exc:  # noqa: BLE001 -- any parse/transpile error is captured
        return False, gold_sql, f"{type(exc).__name__}: {exc}"


@dataclass
class ManifestEntry:
    """One ingested database's resolution info.

    Args:
        db_id: Benchmark database id.
        database: Athena database/schema name it was ingested into.
        catalog: Athena catalog (federated S3 Tables catalog id).
        tables: Table names ingested.
        row_counts: Table name -> row count.
        transpile_ok: Whether all gold SQL for this db transpiled cleanly.
    """

    db_id: str
    database: str
    catalog: str
    tables: List[str]
    row_counts: Dict[str, int]
    transpile_ok: bool


@dataclass
class Manifest:
    """A benchmark's ingestion manifest (db_id -> ManifestEntry).

    Args:
        benchmark: Benchmark slug ("bird" / "cwd" / "spider2").
        entries: The per-database entries.
    """

    benchmark: str
    entries: List[ManifestEntry] = field(default_factory=list)

    def add(self, *, entry: ManifestEntry) -> None:
        """Append or replace the entry for ``entry.db_id`` (dedupe on re-ingest).

        Adding an entry whose ``db_id`` already exists REPLACES the prior one
        rather than appending a duplicate. This keeps the manifest idempotent
        under a resumed deploy: the deploy driver writes the manifest BEFORE it
        commits the resumable state, so a crash between those two writes causes
        the next run to re-ingest and re-add the same db — which must yield one
        entry, not two.

        Args:
            entry: The ``ManifestEntry`` to record.

        Returns:
            None.
        """
        self.entries = [e for e in self.entries if e.db_id != entry.db_id]
        self.entries.append(entry)

    def entry(self, *, db_id: str) -> ManifestEntry:
        """Return the entry for ``db_id``.

        Args:
            db_id: Database id to look up.

        Returns:
            The matching ``ManifestEntry``.

        Raises:
            KeyError: No entry with that id (fail loudly).
        """
        for e in self.entries:
            if e.db_id == db_id:
                return e
        raise KeyError(f"no manifest entry for db_id={db_id}")

    def save(self, *, path: Path) -> None:
        """Write the manifest to JSON.

        Args:
            path: Destination path (parent dirs created).

        Returns:
            None.
        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, *, path: Path) -> "Manifest":
        """Load a manifest from JSON.

        Args:
            path: Source JSON path.

        Returns:
            The parsed ``Manifest``.
        """
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = [ManifestEntry(**e) for e in raw.get("entries", [])]
        return cls(benchmark=raw["benchmark"], entries=entries)


# SQLite storage class -> Athena/Trino type. SQLite is dynamically typed, so we
# map by the declared column type prefix and fall back to varchar. BLOB maps to
# varchar (not varbinary): the module stringifies all non-numeric cells for result
# comparison, so declaring varbinary DDL over string-valued Parquet would make
# Athena raise HIVE_BAD_DATA — keep the DDL type consistent with the written data.
_SQLITE_TO_ATHENA = {
    "INT": "bigint",
    "INTEGER": "bigint",
    "REAL": "double",
    "FLOAT": "double",
    "NUMERIC": "double",
    "TEXT": "varchar",
    "CHAR": "varchar",
    "BLOB": "varchar",
}

# Athena type -> the Arrow type we coerce the column to. Everything else falls
# back to string, because SQLite is dynamically typed and a "declared INTEGER"
# column can still contain strings — coercing to string is lossless for result
# comparison (exec_metrics normalizes numerics from strings anyway).
_ATHENA_TO_ARROW = {
    "bigint": pa.int64(),
    "integer": pa.int64(),
    "double": pa.float64(),
}


def _athena_type(*, declared: str) -> str:
    """Map a SQLite declared column type to an Athena/Trino type.

    Args:
        declared: The SQLite ``PRAGMA table_info`` declared type (may be empty).

    Returns:
        An Athena type string; defaults to ``varchar`` for unknown/blank types.
    """
    up = (declared or "").upper()
    for prefix, atype in _SQLITE_TO_ATHENA.items():
        if up.startswith(prefix):
            return atype
    return "varchar"


def _coerce(*, value: Any, arrow_type: Any) -> Any:
    """Coerce one SQLite cell to the target Arrow column type, else fall back.

    Args:
        value: The raw SQLite cell (may be int/float/str/bytes/None).
        arrow_type: The intended pyarrow type for the column.

    Returns:
        ``None`` for NULL; the numerically-cast value when the column is numeric
        and the cast succeeds; otherwise the value as ``str`` (so a stray string
        in a numeric column doesn't blow up the whole file).
    """
    if value is None:
        return None
    if arrow_type in (pa.int64(), pa.float64()):
        try:
            return int(value) if arrow_type == pa.int64() else float(value)
        except (ValueError, TypeError):
            return str(value)  # dynamic-typed outlier — keep as string
    return str(value)


def sqlite_to_parquet(
    *, db_path: Path, table: str, out_path: Path
) -> List[Tuple[str, str]]:
    """Export one SQLite table to a Parquet file with an EXPLICIT Arrow schema.

    SQLite's dynamic typing means a column can mix types or be entirely NULL, both
    of which break ``pa.table(dict)`` type inference. We derive each column's Arrow
    type from its declared type and coerce cells (falling back to string on numeric
    outliers), so real benchmark data ingests deterministically. Columns whose
    coerced numeric values include any string outlier collapse to string.

    Args:
        db_path: Source SQLite file.
        table: Table name to export (validated as a safe identifier).
        out_path: Destination ``.parquet`` path (parent dirs created).

    Returns:
        Column schema as ``[(name, athena_type), ...]`` in table order.
    """
    safe_ident(name=table)
    con = sqlite3.connect(str(db_path))
    try:
        info = con.execute(f'PRAGMA table_info("{table}")').fetchall()
        cols = [(safe_column(name=c[1]), _athena_type(declared=c[2])) for c in info]
        rows = con.execute(f'SELECT * FROM "{table}"').fetchall()
    finally:
        con.close()

    arrays = []
    for i, (name, atype) in enumerate(cols):
        arrow_type = _ATHENA_TO_ARROW.get(atype, pa.string())
        coerced = [_coerce(value=r[i], arrow_type=arrow_type) for r in rows]
        # If any numeric coercion fell back to a string, downgrade the whole
        # column to string so the Arrow array is homogeneous.
        needs_downgrade = arrow_type != pa.string() and any(
            isinstance(v, str) for v in coerced if v is not None
        )
        if not needs_downgrade and arrow_type != pa.string():
            # A value may still be numeric-but-unrepresentable (e.g. an integer
            # beyond int64 range, stored as TEXT and parsed by Python's arbitrary-
            # precision int). pa.array would raise OverflowError/ArrowInvalid, so
            # try the typed build and fall back to string on ANY failure rather
            # than aborting the whole ingestion.
            try:
                arrays.append(pa.array(coerced, type=arrow_type))
                continue
            except (pa.ArrowInvalid, OverflowError, ValueError):
                needs_downgrade = True
        if needs_downgrade:
            arrow_type = pa.string()
            coerced = [None if v is None else str(v) for v in coerced]
            cols[i] = (name, "varchar")
        arrays.append(pa.array(coerced, type=arrow_type))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(arrays, names=[n for n, _ in cols]), str(out_path))
    return cols


# Athena/Trino type -> Hive DDL type. CREATE EXTERNAL TABLE uses Athena's HIVE
# DDL parser (backtick identifiers, Hive type names); double-quoted identifiers
# route to the Trino parser which rejects EXTERNAL (verified live). Our column
# types are only {bigint, double, varchar}, and Hive spells varchar as `string`.
_ATHENA_TO_HIVE = {"varchar": "string", "bigint": "bigint", "double": "double"}


def build_external_table_sql(
    *, database: str, table: str, columns: List[Tuple[str, str]], s3_prefix: str
) -> str:
    """Build a Glue EXTERNAL TABLE (Parquet) DDL over a staging S3 prefix.

    Emits Athena HIVE DDL: BACKTICK identifiers and Hive type names. Double-quoted
    identifiers route CREATE EXTERNAL TABLE to Athena's Trino parser, which rejects
    ``EXTERNAL`` (verified live). ``safe_ident`` still validates every name.

    Args:
        database: Staging Glue database.
        table: External table name.
        columns: ``[(name, athena_type), ...]`` where athena_type in {bigint, double, varchar}.
        s3_prefix: ``s3://`` prefix holding the Parquet file(s).

    Returns:
        A Hive ``CREATE EXTERNAL TABLE`` statement (STORED AS PARQUET).

    Raises:
        ValueError: A table or column name is not a safe identifier.
    """
    safe_ident(name=database)
    safe_ident(name=table)
    col_ddl = ", ".join(
        f"`{safe_column(name=n)}` {_ATHENA_TO_HIVE.get(t, 'string')}"
        for n, t in columns
    )
    return (
        f"CREATE EXTERNAL TABLE IF NOT EXISTS `{database}`.`{table}` ({col_ddl}) "
        f"STORED AS PARQUET LOCATION '{s3_prefix}'"
    )


def build_drop_sql(*, target_db: str, target_table: str) -> str:
    """Build a ``DROP TABLE IF EXISTS`` for idempotent re-ingestion.

    Args:
        target_db: Iceberg database/namespace.
        target_table: Iceberg table name.

    Returns:
        A ``DROP TABLE IF EXISTS`` statement. Callers run this BEFORE
        ``build_ctas_sql`` on re-runs, because CTAS has no ``OR REPLACE`` /
        ``IF NOT EXISTS`` — a second CTAS onto an existing table fails
        AlreadyExists. Drop-then-create is the idempotency contract.
    """
    safe_ident(name=target_db)
    safe_ident(name=target_table)
    # Unquoted identifiers: the quoted 2-part form fails to parse under the
    # federated S3 Tables catalog context (verified live, Task 0 spike). safe_ident
    # above guarantees the names are injection-safe, so unquoting is safe.
    # NOTE: if ``DROP TABLE IF EXISTS db.table`` turns out unsupported on this
    # catalog at ingest time, the fallback is a plain ``DROP TABLE db.table``
    # guarded by a catch of the not-found error — not implemented now; IF EXISTS
    # unquoted is the primary form.
    return f"DROP TABLE IF EXISTS {target_db}.{target_table}"


def build_ctas_sql(
    *,
    target_db: str,
    target_table: str,
    source_db: str,
    source_table: str,
    source_catalog: str = "",
    table_type: str = "ICEBERG",
) -> str:
    """Build a CTAS that materializes a staging table into an Iceberg table.

    Args:
        target_db: Iceberg database/namespace.
        target_table: Iceberg table name to create.
        source_db: Staging Glue database.
        source_table: Staging external table name.
        source_catalog: Catalog of the staging source; when set, the SELECT
            source is 3-part ``catalog.db.table`` so it resolves across the
            catalog boundary. Pass the Glue default (e.g. ``AwsDataCatalog``)
            when the target is a federated catalog. The Task 0 spike must confirm
            the exact working form. Defaults ``""`` (2-part source).
        table_type: Iceberg table-type property. Defaults to ``"ICEBERG"`` (the
            Glue-Iceberg idiom); the Task 0 spike confirms whether the target
            S3 Tables catalog requires, ignores, or rejects this — pass ``""`` to
            omit the ``WITH`` clause if the spike says so.

    Returns:
        A ``CREATE TABLE [WITH (table_type=..., is_external=false)] AS SELECT``
        statement. When ``table_type`` is non-empty the ``WITH`` clause emits BOTH
        ``table_type`` and ``is_external = false``; the latter is REQUIRED for
        S3 Tables managed Iceberg tables — CTAS otherwise fails with
        ``NOT_SUPPORTED: Only managed table is supported for Iceberg table type,
        please set 'is_external' property to false`` (verified live via the
        Task 0 spike). NOT idempotent on its own — pair with ``build_drop_sql``
        on re-runs.

    Raises:
        ValueError: A database or table name is not a safe identifier.
    """
    for ident in (target_db, target_table, source_db, source_table):
        safe_ident(name=ident)
    src = f'"{source_db}"."{source_table}"'
    if source_catalog:
        # 3-part name so the staging table resolves in its OWN catalog (Glue
        # default) even when the execution-context Catalog is the federated
        # S3 Tables catalog the TARGET lives in. Without this the 2-part source
        # resolves inside the federated catalog and fails TABLE_NOT_FOUND.
        safe_ident(name=source_catalog)
        src = f'"{source_catalog}".{src}'
    with_clause = (
        f"WITH (table_type = '{table_type}', is_external = false) "
        if table_type
        else ""
    )
    return (
        f'CREATE TABLE "{target_db}"."{target_table}" '
        f"{with_clause}AS "
        f"SELECT * FROM {src}"
    )


def upload_parquet(*, s3: Any, local_path: Path, bucket: str, key: str) -> str:
    """Upload a local Parquet file to S3.

    Args:
        s3: boto3 ``s3`` client.
        local_path: Local ``.parquet`` file.
        bucket: Destination bucket.
        key: Destination key (should end in ``.parquet``).

    Returns:
        The ``s3://bucket/key`` URI.
    """
    s3.upload_file(str(local_path), bucket, key)
    return f"s3://{bucket}/{key}"
