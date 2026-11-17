"""Ingest data.world CWD (ACME_Insurance) into S3 Tables/Athena.

Idempotent CLI mirroring ``ingest_bird.py``. Unlike BIRD, CWD ships CSV/TSV + a
DDL schema (no SQLite), so ingestion parses the DDL for column types (via
sqlglot), loads the CSVs as Parquet, and DROP+CTASes into a ``cwd_acme`` Iceberg
namespace. Ontology / R2RML / TTL artifacts are downloaded to
``data/eval/benchmarks/cwd/raw/`` for REFERENCE ONLY and never loaded as DATA —
the metadata + ontology agents build the semantic layer separately.

The benchmark QUESTIONS also ship as RDF/Turtle (``investigation/acme-benchmark.ttl``),
not JSON: each ``dwt:SqlQuery`` node carries the NL question (``dct:description``)
and gold SQL (``QandA:queryText``). ``extract_cwd_questions`` reads ONLY that
question TTL (never the ontology TTLs) to build the ``cwd.jsonl`` question set;
the TTL is still never ingested as table data.

Usage:
    python scripts/benchmarks/ingest_cwd.py --bucket <bkt> \\
        --catalog s3tablescatalog/<bench-bkt> --workgroup <wg> [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pyarrow as pa
import pyarrow.parquet as pq
import rdflib
import sqlglot
from sqlglot import exp

from agents.shared.athena_bench import run_sql
from agents.shared.benchmark_adapter import BenchmarkRow, write_jsonl
from scripts.benchmarks import deploy_datasets as dd
from scripts.benchmarks import ingest_common as ic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BENCH_DIR = Path("data/eval/benchmarks/cwd")
# Pinned to an immutable commit SHA (see BIRD note in ingest_bird.py). Replace
# <PINNED_SHA> with the SHA you download and record it in the runbook; never
# refs/heads/main.
CWD_COMMIT_SHA = "0b75eb62eaf7ea315a863cd7611ebc908149f7e0"
CWD_ARCHIVE_URL = (
    f"https://github.com/datadotworld/cwd-benchmark-data/archive/{CWD_COMMIT_SHA}.zip"
)

# Ontology / mapping artifacts CWD ships. These are downloaded to raw/ for the
# metadata + ontology agents to consume as REFERENCE, but are NEVER loaded into
# Athena by this pipeline. Matched (case-insensitively) by suffix.
_REFERENCE_ONLY_SUFFIXES = (".ttl", ".owl", ".rdf", ".r2rml", ".rml", ".sparql")

# RDF namespaces used by the CWD benchmark-question TTL (``acme-benchmark.ttl``).
# The NL-question + gold-SQL pairs are RDF triples, NOT JSON: each ``dwt:SqlQuery``
# node carries ``QandA:queryText`` (the gold SQL) and ``dct:description`` (the NL
# question). ``dwt:SparqlQuery`` nodes are SKIPPED — the semantic layer is scored
# on SQL only. These IRIs are stable (fixed by the data.world benchmark schema).
_NS_QANDA = rdflib.Namespace("http://models.data.world/benchmarks/QandA#")
_NS_DWT = rdflib.Namespace("https://templates.data.world/")
_NS_DCT = rdflib.Namespace("http://purl.org/dc/terms/")

# sqlglot type family -> Athena/Trino type STRING. Every emitted type is one of
# {bigint, double, varchar} — exactly the three physical forms csv_to_parquet
# actually writes (int64 / float64 / utf8). This keeps the DECLARED external-table
# type identical to the PHYSICAL Parquet type; a rich declaration (date/timestamp/
# boolean/decimal) over physically-string Parquet makes Athena throw
# HIVE_BAD_DATA on the CTAS read. This is the same invariant the sqlite path relies
# on (it, too, emits only bigint/double/varchar). NOTE: sqlglot normalizes
# NUMERIC->DECIMAL and REAL->FLOAT at parse time, so those source spellings need no
# entry here. (Verified against sqlglot 30.8.0 — there is no
# ``DataType.Type.NUMERIC`` member; referencing it raises AttributeError.)
_SQLGLOT_TO_ATHENA = {
    exp.DataType.Type.VARCHAR: "varchar",
    exp.DataType.Type.CHAR: "varchar",
    exp.DataType.Type.TEXT: "varchar",
    exp.DataType.Type.INT: "bigint",
    exp.DataType.Type.BIGINT: "bigint",
    exp.DataType.Type.SMALLINT: "bigint",
    exp.DataType.Type.DOUBLE: "double",
    exp.DataType.Type.FLOAT: "double",
    # date/timestamp/boolean are stored as UTF-8 strings in Parquet (lossless for
    # exec-metric string comparison), so they MUST be declared varchar — declaring
    # them date/timestamp/boolean over physically-string Parquet makes Athena throw
    # HIVE_BAD_DATA on the CTAS read. Keeping the physical/declared types identical
    # is the same invariant the sqlite path relies on (bigint/double/varchar only).
    exp.DataType.Type.DATE: "varchar",
    exp.DataType.Type.TIMESTAMP: "varchar",
    exp.DataType.Type.BOOLEAN: "varchar",
    # T-SQL ``datetime`` (the CWD DDL dialect) parses to DATETIME. It is stored as
    # a UTF-8 string in Parquet, so — like DATE/TIMESTAMP — it MUST be declared
    # varchar to keep the declared type identical to the physical type (declaring
    # it datetime/timestamp over string Parquet raises HIVE_BAD_DATA on CTAS read).
    exp.DataType.Type.DATETIME: "varchar",
}

# Athena type -> the Arrow type we coerce a CSV column to. Anything not listed
# (varchar, date, timestamp, decimal, boolean) is read as a string: CSV cells are
# already strings, and exec_metrics normalizes numerics from strings at scoring
# time, so string storage is lossless for result comparison and never raises a
# HIVE_BAD_DATA cast error on load.
_ATHENA_TO_ARROW = {
    "bigint": pa.int64(),
    "integer": pa.int64(),
    "double": pa.float64(),
}


def _athena_type_from_coldef(*, coldef: exp.ColumnDef) -> str:
    """Map a sqlglot ``ColumnDef`` to an Athena/Trino type string.

    The returned type is ALWAYS one of ``{bigint, double, varchar}`` — the three
    physical forms ``csv_to_parquet`` writes (int64 / float64 / utf8). Integer
    families → ``bigint``; float families → ``double``; everything else — including
    ``DECIMAL`` (and ``NUMERIC``, which sqlglot folds into ``DECIMAL``), ``DATE``,
    ``TIMESTAMP``, ``BOOLEAN`` and parameterized string types — → ``varchar``,
    because those are physically stored as UTF-8 strings in Parquet. Declaring a
    richer type (e.g. ``decimal(10,2)``/``date``) over string Parquet would make
    Athena throw ``HIVE_BAD_DATA`` on the CTAS read; keeping the declared type
    identical to the physical type is the invariant that avoids it.

    Args:
        coldef: A parsed column definition from a ``CREATE TABLE``.

    Returns:
        One of ``{bigint, double, varchar}``.
    """
    dtype = coldef.args["kind"]
    fam = dtype.this
    if fam == exp.DataType.Type.DECIMAL:  # NUMERIC parses to DECIMAL too
        # Decimals are written as UTF-8 strings (decimal is not in _ATHENA_TO_ARROW),
        # so declare varchar to match the physical Parquet type (else HIVE_BAD_DATA).
        return "varchar"
    return _SQLGLOT_TO_ATHENA.get(fam, "varchar")


def _split_create_table_blocks(*, ddl: str) -> List[str]:
    """Split a DDL file into individual ``CREATE TABLE ... ( ... )`` blocks.

    The REAL CWD (ACME_Insurance) DDL is T-SQL with quirks a single
    ``sqlglot.parse(whole_file)`` cannot survive: there are NO semicolons between
    statements (they are blank-line separated) and ``PRIMARY KEY (Col ASC)``
    constraints appear inside the parens. Rather than parse the whole file, we
    isolate each statement by locating every ``CREATE TABLE`` (case-insensitive)
    and balancing parentheses from its first ``(`` to the matching ``)``. Each
    returned block is a self-contained ``CREATE TABLE name ( ... )`` string that
    ``parse_one`` can handle on its own.

    Args:
        ddl: The full DDL text (one or more ``CREATE TABLE`` statements).

    Returns:
        A list of ``CREATE TABLE ... ( ... )`` substrings, in file order.
    """
    blocks: List[str] = []
    for match in re.finditer(r"CREATE\s+TABLE", ddl, flags=re.IGNORECASE):
        start = match.start()
        open_paren = ddl.find("(", start)
        if open_paren == -1:
            # A ``CREATE TABLE`` with no column list — skip it (nothing to parse).
            continue
        depth = 0
        end = -1
        for i in range(open_paren, len(ddl)):
            char = ddl[i]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            # Unbalanced parens — the block never closes; skip it (the per-block
            # parse below would fail anyway and be logged there if reconstructed).
            continue
        blocks.append(ddl[start : end + 1])
    return blocks


def parse_ddl_tables(*, ddl: str) -> Dict[str, List[Tuple[str, str]]]:
    """Parse ``{table: [(column, athena_type), ...]}`` from one or more CREATE TABLEs.

    Robust to the REAL data.world CWD (ACME_Insurance) DDL, which is T-SQL with
    quirks that a single generic ``sqlglot.parse(whole_file)`` cannot handle:

    * NO semicolons between statements (they are blank-line separated), and
    * ``PRIMARY KEY (Col ASC)`` / ``FOREIGN KEY`` constraints inside the parens —
      the ``ASC``/``DESC`` keyword breaks the generic parser with ``Expecting )``.

    So this splits the file into individual ``CREATE TABLE ( ... )`` blocks (by
    balancing parens), strips standalone ``ASC``/``DESC`` tokens, then parses each
    block with ``read="tsql"`` (do NOT use ``read="ansi"`` — 30.8.0 has no such
    dialect). A block that still fails to parse is logged and SKIPPED (fail-loud-
    but-continue) rather than aborting the whole file — but an all-empty result
    raises, since that means the DDL format changed entirely.

    Every emitted Athena type is one of ``{bigint, double, varchar}`` — the three
    physical forms ``csv_to_parquet`` writes. Decimals/dates/datetimes/timestamps/
    booleans map to ``varchar`` because they are stored as UTF-8 strings in
    Parquet, so the declared external-table type stays identical to the physical
    type (else the CTAS read raises ``HIVE_BAD_DATA``). See
    ``_athena_type_from_coldef``.

    Args:
        ddl: DDL text containing one or more ``CREATE TABLE`` statements.

    Returns:
        Ordered per-table column schemas ``{table: [(col, athena_type), ...]}``
        where every ``athena_type`` ∈ ``{bigint, double, varchar}``; table-level
        constraints (PK/FK/…) are naturally excluded because they are not
        ``ColumnDef`` nodes.

    Raises:
        ValueError: No ``CREATE TABLE`` block parsed successfully — the DDL format
            changed entirely; fail loud rather than return an empty schema.
    """
    tables: Dict[str, List[Tuple[str, str]]] = {}
    blocks = _split_create_table_blocks(ddl=ddl)
    for block in blocks:
        # Strip standalone ASC/DESC tokens (as in ``PRIMARY KEY (Col ASC)``) — the
        # T-SQL reader rejects them inside the constraint parens with ``Expecting )``.
        cleaned = re.sub(r"\b(ASC|DESC)\b", "", block, flags=re.IGNORECASE)
        try:
            stmt = sqlglot.parse_one(cleaned, read="tsql")
        except Exception as err:  # noqa: BLE001 — capture any sqlglot parse failure
            # Fail-loud-but-continue: log the offending block and skip THIS table
            # so one malformed statement never aborts the whole DDL file.
            logger.warning(
                "Skipping a CREATE TABLE block that failed to parse (read=tsql): "
                "%s | block=%r",
                err,
                block[:200],
            )
            continue
        if not isinstance(stmt, exp.Create) or not isinstance(stmt.this, exp.Schema):
            continue
        table_name = stmt.this.this.name
        cols: List[Tuple[str, str]] = []
        for coldef in stmt.this.expressions:
            if isinstance(coldef, exp.ColumnDef):
                cols.append((coldef.name, _athena_type_from_coldef(coldef=coldef)))
        tables[table_name] = cols

    if not tables:
        raise ValueError(
            "parse_ddl_tables produced no tables — no CREATE TABLE block parsed "
            "successfully. The CWD DDL format may have changed entirely; refusing "
            "to return an empty schema (would silently ingest nothing)."
        )
    return tables


def download_cwd(*, dest: Path, dry_run: bool) -> None:
    """Download and extract the pinned CWD archive into ``dest``.

    Fails loudly when the commit SHA is still the ``<PINNED_SHA>`` placeholder so
    an operator never silently downloads a drifting ``main`` snapshot. Ontology /
    R2RML / TTL artifacts land in ``dest`` alongside the CSV + DDL but are NEVER
    loaded by this pipeline — the metadata/ontology agents consume them.

    Args:
        dest: The ``raw/`` directory the archive is extracted into.
        dry_run: When True, only log the planned download and return without
            touching the network or filesystem.

    Returns:
        None.

    Raises:
        RuntimeError: ``CWD_COMMIT_SHA`` is unset/placeholder — the operator must
            pin a real immutable SHA (see the ingestion runbook) before ingesting.
    """
    if "<" in CWD_COMMIT_SHA or not CWD_COMMIT_SHA:
        raise RuntimeError(
            "CWD_COMMIT_SHA is a placeholder. Pin an immutable "
            "cwd-benchmark-data commit SHA in scripts/benchmarks/ingest_cwd.py "
            "(record it in the runbook) before ingesting, or pre-populate "
            f"{dest} manually. Refusing to download refs/heads/main."
        )
    if dry_run:
        logger.info("[DRY RUN] would download %s -> %s", CWD_ARCHIVE_URL, dest)
        return

    # Imported lazily: requests is only needed for the live download path, so the
    # module (and its unit-tested pure function) imports cleanly without it.
    import requests

    logger.info("Downloading CWD from %s", CWD_ARCHIVE_URL)
    resp = requests.get(CWD_ARCHIVE_URL, timeout=300)
    resp.raise_for_status()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        zf.extractall(dest)
    logger.info("Extracted CWD into %s", dest)

    # Log (but never load) the reference-only semantic artifacts so the operator
    # can confirm the ontology/R2RML files are present for the agents.
    ref_files = [
        p for p in dest.rglob("*") if p.suffix.lower() in _REFERENCE_ONLY_SUFFIXES
    ]
    if ref_files:
        logger.info(
            "Found %d reference-only artifact(s) (ontology/R2RML/TTL) — NOT "
            "loaded into Athena; the metadata/ontology agents consume these: %s",
            len(ref_files),
            ", ".join(sorted(p.name for p in ref_files)),
        )


def find_ddl(*, raw_dir: Path) -> Path:
    """Locate the single CWD DDL schema file under the extracted ``raw/`` tree.

    Args:
        raw_dir: The ``raw/`` directory holding the extracted CWD tree.

    Returns:
        Path to the first ``*.sql`` / ``*.ddl`` file found (sorted).

    Raises:
        FileNotFoundError: No DDL file is present — fail loudly rather than
            silently infer a schema from the CSV headers (a later enhancement).
    """
    matches = sorted(raw_dir.rglob("*.sql")) + sorted(raw_dir.rglob("*.ddl"))
    if not matches:
        raise FileNotFoundError(
            f"No *.sql / *.ddl schema file found under {raw_dir}. CWD ingestion "
            "requires a DDL file to derive column types; schema inference from "
            "CSV headers is not implemented. Confirm the CWD archive layout and "
            "pin the correct commit SHA."
        )
    return matches[0]


def find_table_csv(*, raw_dir: Path, table: str) -> Tuple[Path, str]:
    """Locate the CSV/TSV data file for a DDL table under ``raw/``.

    Args:
        raw_dir: The ``raw/`` directory holding the extracted CWD tree.
        table: The table name (from the DDL) whose data file to find.

    Returns:
        ``(path, delimiter)`` — the data file and its delimiter (``\\t`` for
        ``.tsv``, ``,`` otherwise).

    Raises:
        FileNotFoundError: No ``<table>.csv`` / ``<table>.tsv`` exists — fail
            loudly rather than materialize an empty table.
    """
    # Case-insensitive match on the file stem so a "Policy.csv" satisfies a
    # "policy" DDL table (CWD file casing is not guaranteed to match the DDL).
    for suffix, delimiter in ((".csv", ","), (".tsv", "\t")):
        for path in sorted(raw_dir.rglob(f"*{suffix}")):
            if path.stem.lower() == table.lower():
                return path, delimiter
    raise FileNotFoundError(
        f"No CSV/TSV data file found for table {table!r} under {raw_dir} "
        "(looked for <table>.csv / <table>.tsv, case-insensitive)."
    )


def warn_uncovered_csvs(
    *, raw_dir: Path, tables: Dict[str, List[Tuple[str, str]]]
) -> List[str]:
    """Warn about CSV/TSV data files that have NO matching DDL table.

    The real CWD tree ships ~29 CSVs but the ACME_small DDL declares only 13
    tables, so many CSVs have no schema. This pipeline ingests ONLY the tables
    present in the DDL (it needs declared types), which is acceptable for the
    spike — but silently ignoring the extra CSVs would hide a coverage gap, so we
    LOG each uncovered file loudly. Matching is case-insensitive on the file stem
    (mirrors ``find_table_csv``).

    Args:
        raw_dir: The ``raw/`` directory holding the extracted CWD tree.
        tables: The parsed DDL schemas ``{table: [...]}`` — their names define
            what is "covered".

    Returns:
        The sorted list of uncovered CSV/TSV file names (empty if all are covered).
    """
    covered = {t.lower() for t in tables}
    uncovered: List[str] = []
    for suffix in (".csv", ".tsv"):
        for path in raw_dir.rglob(f"*{suffix}"):
            if path.stem.lower() not in covered:
                uncovered.append(path.name)
    uncovered = sorted(set(uncovered))
    if uncovered:
        logger.warning(
            "%d CSV/TSV file(s) have NO DDL table and will NOT be ingested "
            "(the DDL declares only %d tables; a schema is required to load): %s",
            len(uncovered),
            len(tables),
            ", ".join(uncovered),
        )
    return uncovered


def csv_to_parquet(
    *,
    csv_path: Path,
    columns: List[Tuple[str, str]],
    out_path: Path,
    delimiter: str = ",",
) -> List[Tuple[str, str]]:
    """Convert a CWD CSV/TSV to Parquet using the DDL-declared column schema.

    Reads the CSV with the DDL column order/types and writes Parquet via pyarrow,
    coercing numeric columns to their Arrow type with a string fallback on any
    parse failure (mirroring ``ingest_common.sqlite_to_parquet``: a stray string
    in a numeric column collapses the whole column to string rather than aborting
    the file). All other declared types (varchar/date/decimal/…) are stored as
    strings — CSV cells are already strings and exec_metrics normalizes at scoring
    time, so this is lossless and never raises HIVE_BAD_DATA on load.

    A HEADERED CSV/TSV is assumed: row 0 is the column header and is VALIDATED
    (case-insensitive, trimmed) to match the DDL column names in order before the
    body is read. A mismatch fails loud rather than silently populating columns
    from the wrong source. If CWD ships headerless files (verify at the live gate),
    this raises a clear error and the operator can add a ``--no-header`` flag then;
    it does NOT guess.

    Args:
        csv_path: Source CSV/TSV file. Its header row is validated then skipped;
            column ORDER is taken from the DDL (``columns``).
        columns: DDL column schema ``[(name, athena_type), ...]`` in table order.
        out_path: Destination ``.parquet`` path (parent dirs created).
        delimiter: Field delimiter (``","`` for CSV, ``"\\t"`` for TSV).

    Returns:
        The (possibly downgraded) column schema ``[(name, athena_type), ...]`` —
        a column whose numeric coercion fell back to string is reported as
        ``varchar`` so the external-table DDL matches the written Parquet.

    Raises:
        ValueError: The file is empty, the CSV header does not match the DDL
            column order, or a body row's field count differs from the DDL column
            count — all fail loud rather than silently mis-align/corrupt data.
    """
    # Column names are emitted only as pyarrow names and BACKTICK-quoted Hive
    # identifiers (see build_external_table_sql), so they may contain spaces/
    # punctuation — validate with safe_column, not the strict safe_ident used for
    # tables/namespaces.
    for name, _ in columns:
        ic.safe_column(name=name)

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        all_rows = list(reader)
    if not all_rows:
        raise ValueError(f"CSV {csv_path} is empty (no header row)")

    # Validate the header against the DDL column order (case-insensitive, trimmed).
    # Trusting DDL order while discarding the header would silently populate every
    # column from the wrong source if the physical order differs — corrupt data,
    # no error. Fail loud instead.
    header = all_rows[0]
    ddl_names = [c[0] for c in columns]

    def _norm(names: List[str]) -> List[str]:
        """Lowercase + strip each name for order-preserving header comparison."""
        return [str(n).strip().lower() for n in names]

    if _norm(header) != _norm(ddl_names):
        raise ValueError(
            f"CSV header does not match DDL column order for {csv_path.name}: "
            f"header={header} ddl={ddl_names}. Refusing to ingest (silent column "
            "misalignment would corrupt data)."
        )
    # Row 0 is the (validated) header; the DDL defines the authoritative order.
    data_rows = all_rows[1:]
    for line_no, row in enumerate(data_rows, start=2):
        if len(row) != len(columns):
            raise ValueError(
                f"{csv_path}:{line_no}: row has {len(row)} fields but DDL declares "
                f"{len(columns)} columns; refusing to mis-align data."
            )

    # Build one Arrow array per column, coercing numerics with a string fallback.
    out_cols: List[Tuple[str, str]] = list(columns)
    arrays = []
    for i, (name, atype) in enumerate(columns):
        arrow_type = _ATHENA_TO_ARROW.get(atype, pa.string())
        coerced = [
            _coerce_cell(value=row[i], arrow_type=arrow_type) for row in data_rows
        ]
        # If any numeric coercion fell back to a string, downgrade the whole
        # column to string so the Arrow array is homogeneous and the DDL matches.
        needs_downgrade = arrow_type != pa.string() and any(
            isinstance(v, str) for v in coerced if v is not None
        )
        if not needs_downgrade and arrow_type != pa.string():
            # A value may be numeric-but-unrepresentable (e.g. an int beyond
            # int64 range); try the typed build and fall back to string on ANY
            # failure rather than aborting the whole ingestion.
            try:
                arrays.append(pa.array(coerced, type=arrow_type))
                continue
            except (pa.ArrowInvalid, OverflowError, ValueError):
                needs_downgrade = True
        if needs_downgrade:
            arrow_type = pa.string()
            coerced = [None if v is None else str(v) for v in coerced]
            out_cols[i] = (name, "varchar")
        arrays.append(pa.array(coerced, type=arrow_type))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(arrays, names=[n for n, _ in out_cols]), str(out_path))
    logger.info("[%s] wrote %d rows -> %s", csv_path.name, len(data_rows), out_path)
    return out_cols


def _coerce_cell(*, value: str, arrow_type: Any) -> Any:
    """Coerce one CSV cell (always a string) to the target Arrow type, else fall back.

    Args:
        value: The raw CSV cell string.
        arrow_type: The intended pyarrow type for the column.

    Returns:
        ``None`` for an empty cell (CSV NULL convention); the numerically-cast
        value when the column is numeric and the cast succeeds; otherwise the
        cell as ``str`` (so a stray string in a numeric column doesn't blow up
        the whole file).
    """
    if value == "":
        return None  # empty CSV cell == NULL
    if arrow_type in (pa.int64(), pa.float64()):
        try:
            return int(value) if arrow_type == pa.int64() else float(value)
        except (ValueError, TypeError):
            return str(value)  # non-numeric outlier — keep as string
    return str(value)


def extract_cwd_questions(*, ttl_path: Path, db_id: str) -> List[BenchmarkRow]:
    """Extract CWD NL-question + gold-SQL pairs from the benchmark Turtle file.

    CWD's benchmark questions live in an RDF/Turtle file (``acme-benchmark.ttl``),
    NOT JSON. Each ``dwt:SqlQuery`` node carries ``QandA:queryText`` (the gold SQL)
    and ``dct:description`` (the natural-language question). ``dwt:SparqlQuery``
    nodes are SKIPPED — the semantic layer is executed as SQL at eval time, so the
    SPARQL variants are not scored.

    Pure/testable: takes a path, returns rows, touches no AWS. The file is parsed
    with rdflib; a SqlQuery node missing either ``queryText`` or ``description`` is
    logged and skipped (a partial node would corrupt scoring) rather than emitting
    a half-populated row.

    Args:
        ttl_path: Path to the CWD benchmark Turtle file (``acme-benchmark.ttl``).
        db_id: The namespace applied as each row's ``db_id`` (e.g. ``cwd_acme``).

    Returns:
        One ``BenchmarkRow`` per ``dwt:SqlQuery`` node that has both a description
        and query text. Returns ``[]`` (with a warning) if ``ttl_path`` does not
        exist, so a data-only ingest still succeeds.

    Raises:
        ValueError: The TTL file EXISTS but yields ZERO ``dwt:SqlQuery`` pairs —
            the benchmark format changed (or the wrong .ttl was selected). Fail
            loud rather than silently emit an empty question set.
    """
    if not ttl_path.exists():
        logger.warning(
            "CWD benchmark TTL not found at %s; emitting an empty question set so "
            "data-only ingest still succeeds. Confirm the CWD archive layout / "
            "commit SHA if questions are expected.",
            ttl_path,
        )
        return []

    graph = rdflib.Graph()
    # format="turtle" — the CWD benchmark file is Turtle (.ttl). Parsing failures
    # propagate (fail loud) rather than being swallowed.
    graph.parse(str(ttl_path), format="turtle")

    rows: List[BenchmarkRow] = []
    skipped_partial = 0
    # Iterate every node typed as a SQL query; SparqlQuery nodes are never yielded
    # by this subjects() query, so they are skipped structurally.
    for subject in graph.subjects(rdflib.RDF.type, _NS_DWT.SqlQuery):
        query_text = graph.value(subject, _NS_QANDA.queryText)
        description = graph.value(subject, _NS_DCT.description)
        if query_text is None or description is None:
            # A SqlQuery node missing the question or the gold SQL cannot be scored;
            # skip it loudly rather than emit a half-populated row.
            skipped_partial += 1
            logger.warning(
                "Skipping SqlQuery node %s: missing %s.",
                subject,
                "queryText" if query_text is None else "description",
            )
            continue
        rows.append(
            BenchmarkRow(
                question=str(description), gold_sql=str(query_text), db_id=db_id
            )
        )

    if skipped_partial:
        logger.warning(
            "Skipped %d SqlQuery node(s) missing queryText/description while "
            "extracting CWD questions from %s.",
            skipped_partial,
            ttl_path.name,
        )

    if not rows:
        raise ValueError(
            f"extract_cwd_questions found ZERO dwt:SqlQuery pairs in {ttl_path} "
            "(the file exists but yielded no NL-question + gold-SQL rows). The CWD "
            "benchmark TTL format may have changed, or the wrong .ttl was selected "
            "(e.g. an ontology TTL). Refusing to emit an empty question set."
        )

    logger.info(
        "Extracted %d CWD SQL question pair(s) from %s (SPARQL variants skipped).",
        len(rows),
        ttl_path.name,
    )
    return rows


def find_benchmark_ttl(*, raw_dir: Path) -> Path:
    """Locate the CWD benchmark-QUESTION Turtle file under the extracted tree.

    CWD ships MULTIPLE .ttl files: ontology/mapping artifacts (e.g.
    ``ontology/insurance.ttl``) AND the benchmark question file
    (``investigation/acme-benchmark.ttl``). Only the LATTER carries the
    ``dwt:SqlQuery`` nodes we score, so this must NOT return an ontology TTL.

    Selection strategy (in priority order):

    1. Among all ``*.ttl`` files, keep those that actually contain a
       ``dwt:SqlQuery`` triple (parsed with rdflib) — this is the definitive
       marker of a question file and excludes ontology/R2RML TTLs.
    2. If several qualify, prefer one literally named ``acme-benchmark.ttl``.
    3. If none contain ``dwt:SqlQuery`` (e.g. rdflib cannot parse them), fall
       back to a filename match on ``*acme*benchmark*.ttl`` / ``*benchmark*.ttl``.

    The selected file is logged so the operator can confirm the ontology TTLs are
    NOT being read as questions.

    Args:
        raw_dir: The ``raw/`` directory holding the extracted CWD tree.

    Returns:
        Path to the benchmark question TTL.

    Raises:
        FileNotFoundError: No candidate benchmark TTL could be located — fail loud
            rather than silently skip the entire question set.
    """
    all_ttls = sorted(raw_dir.rglob("*.ttl"))
    if not all_ttls:
        raise FileNotFoundError(
            f"No *.ttl file found under {raw_dir}. The CWD benchmark questions live "
            "in a Turtle file (acme-benchmark.ttl); confirm the archive layout / "
            "commit SHA."
        )

    # 1. Keep TTLs that contain at least one dwt:SqlQuery triple — the definitive
    #    marker of a question file (excludes ontology/mapping TTLs).
    with_sql: List[Path] = []
    for candidate in all_ttls:
        try:
            graph = rdflib.Graph()
            graph.parse(str(candidate), format="turtle")
        except Exception as err:  # noqa: BLE001 — any parse failure => not a match
            logger.warning("Could not parse %s as Turtle: %s", candidate.name, err)
            continue
        if next(graph.subjects(rdflib.RDF.type, _NS_DWT.SqlQuery), None) is not None:
            with_sql.append(candidate)

    if with_sql:
        # 2. Prefer the canonical acme-benchmark.ttl if multiple qualify.
        chosen = next(
            (p for p in with_sql if p.name == "acme-benchmark.ttl"), with_sql[0]
        )
        if len(with_sql) > 1:
            logger.warning(
                "Multiple TTLs contain dwt:SqlQuery triples (%s); using %s.",
                ", ".join(p.name for p in with_sql),
                chosen.name,
            )
        logger.info(
            "Selected CWD benchmark question TTL: %s (ontology/R2RML TTLs are NOT "
            "read as questions).",
            chosen,
        )
        return chosen

    # 3. Fallback: filename match (e.g. rdflib could not parse the file above).
    for pattern in ("*acme*benchmark*.ttl", "*benchmark*.ttl", "*acme*.ttl"):
        named = sorted(raw_dir.rglob(pattern))
        if named:
            logger.warning(
                "No TTL contained dwt:SqlQuery triples; falling back to filename "
                "match %r -> %s.",
                pattern,
                named[0],
            )
            return named[0]

    raise FileNotFoundError(
        f"No benchmark question TTL found under {raw_dir} (looked for a TTL with "
        "dwt:SqlQuery triples, then *benchmark*.ttl / *acme*.ttl by name)."
    )


def ingest_cwd_tables(
    *,
    tables: Dict[str, List[Tuple[str, str]]],
    raw_dir: Path,
    namespace: str,
    bucket: str,
    catalog: str,
    workgroup: str,
    stage_db: str,
    output_loc: str,
    s3: Any,
    athena: Any,
    dry_run: bool,
) -> ic.ManifestEntry:
    """Ingest every CWD table (CSV -> Parquet -> S3 -> external -> DROP+CTAS).

    Mirrors ``ingest_bird.ingest_database`` but sources rows from CSV/TSV instead
    of SQLite. The staging external table is namespaced (``<namespace>__<table>``)
    so re-runs and multi-table loads never collide in the shared ``stage_db``.

    Args:
        tables: DDL column schemas ``{table: [(col, athena_type), ...]}``.
        raw_dir: The ``raw/`` directory holding the CSV/TSV data files.
        namespace: Iceberg namespace (e.g. ``cwd_acme``).
        bucket: S3 staging bucket for Parquet exports.
        catalog: Athena catalog id (federated S3 Tables catalog).
        workgroup: Athena workgroup name.
        stage_db: Glue database holding the transient external tables.
        output_loc: Athena result ``s3://`` location (empty ⇒ workgroup default).
        s3: boto3 ``s3`` client (unused under ``dry_run``).
        athena: boto3 ``athena`` client (unused under ``dry_run``).
        dry_run: When True, log planned actions and skip all AWS/filesystem work.

    Returns:
        A ``ManifestEntry`` recording the namespace, tables and row counts.
    """
    table_names = sorted(tables)
    logger.info(
        "[%s] %d table(s): %s", namespace, len(table_names), ", ".join(table_names)
    )

    row_counts: Dict[str, int] = {}
    for table in table_names:
        columns = tables[table]
        stage_table = f"{namespace}__{table}"
        s3_prefix_dir = f"benchmarks/cwd/{namespace}/{table}"
        s3_key = f"{s3_prefix_dir}/{table}.parquet"
        s3_prefix = f"s3://{bucket}/{s3_prefix_dir}/"

        if dry_run:
            logger.info(
                "[DRY RUN] [%s.%s] would locate CSV, convert to Parquet, upload "
                "to %s, create external table, DROP+CTAS into Iceberg",
                namespace,
                table,
                s3_prefix,
            )
            row_counts[table] = -1  # unknown under dry-run (no CSV read)
            continue

        # 1. Locate the CSV/TSV and convert to Parquet using the DDL schema. The
        #    returned schema reflects any string-downgrade so the DDL matches.
        csv_path, delimiter = find_table_csv(raw_dir=raw_dir, table=table)
        with tempfile.TemporaryDirectory() as tmp:
            local_parquet = Path(tmp) / f"{table}.parquet"
            written_cols = csv_to_parquet(
                csv_path=csv_path,
                columns=columns,
                out_path=local_parquet,
                delimiter=delimiter,
            )
            # 2. Upload the Parquet to the S3 stage.
            uri = ic.upload_parquet(
                s3=s3, local_path=local_parquet, bucket=bucket, key=s3_key
            )
            logger.info("[%s.%s] uploaded %s", namespace, table, uri)

        # 3. External (staging) table over the uploaded Parquet.
        # DROP first: build_external_table_sql emits CREATE ... IF NOT EXISTS, which
        # SILENTLY REUSES a pre-existing external table even if its LOCATION points at
        # a different (stale) staging bucket — the table then reads 0 rows and every
        # downstream CTAS materializes an empty Iceberg table (observed 2026-07-29:
        # a bench_stage external table left over from a prior run still pointed at the
        # old athena-results staging prefix, so all 13 CWD tables ingested as 0 rows).
        # Dropping the Hive external table only removes the catalog entry, never the
        # underlying S3 data, so this is safe and makes re-ingest truly idempotent.
        run_sql(
            athena=athena,
            sql=f"DROP TABLE IF EXISTS `{stage_db}`.`{stage_table}`",
            workgroup=workgroup,
            output_loc=output_loc,
            database_name=stage_db,
        )
        run_sql(
            athena=athena,
            sql=ic.build_external_table_sql(
                database=stage_db,
                table=stage_table,
                columns=written_cols,
                s3_prefix=s3_prefix,
            ),
            workgroup=workgroup,
            output_loc=output_loc,
            database_name=stage_db,
        )
        # 4. DROP the prior Iceberg table (idempotency), then CTAS the fresh one.
        run_sql(
            athena=athena,
            sql=ic.build_drop_sql(target_db=namespace, target_table=table),
            workgroup=workgroup,
            output_loc=output_loc,
            database_name=namespace,
            catalog_id=catalog,
        )
        run_sql(
            athena=athena,
            sql=ic.build_ctas_sql(
                target_db=namespace,
                target_table=table,
                source_db=stage_db,
                source_table=stage_table,
                # Staging external table lives in the Glue default catalog while
                # the CTAS target lives in the federated S3 Tables catalog; the
                # 3-part source name resolves across that catalog boundary.
                source_catalog="AwsDataCatalog",
            ),
            workgroup=workgroup,
            output_loc=output_loc,
            database_name=namespace,
            catalog_id=catalog,
        )
        # 5. Row count from the materialized Iceberg table.
        count_res = run_sql(
            athena=athena,
            sql=f'SELECT count(*) FROM "{namespace}"."{ic.safe_ident(name=table)}"',
            workgroup=workgroup,
            output_loc=output_loc,
            database_name=namespace,
            catalog_id=catalog,
        )
        count = int(count_res.rows[0][0]) if count_res.rows else 0
        row_counts[table] = count
        logger.info("[%s.%s] materialized %d rows", namespace, table, count)

    return ic.ManifestEntry(
        db_id=namespace,
        database=namespace,
        catalog=catalog,
        tables=table_names,
        row_counts=row_counts,
        transpile_ok=True,
    )


def _parse_args() -> argparse.Namespace:
    """Parse the CWD ingestion CLI arguments.

    Returns:
        The parsed ``argparse.Namespace``.
    """
    parser = argparse.ArgumentParser(
        description="Ingest data.world CWD (ACME_Insurance) into S3 Tables/Athena "
        "and emit a normalized JSONL dataset + manifest.",
    )
    parser.add_argument(
        "--bucket", required=True, help="S3 staging bucket for Parquet exports."
    )
    parser.add_argument(
        "--catalog",
        required=True,
        help="Athena catalog id (federated S3 Tables catalog, "
        "e.g. s3tablescatalog/<bench-bucket>).",
    )
    parser.add_argument("--workgroup", required=True, help="Athena workgroup name.")
    parser.add_argument(
        "--stage-db",
        default="bench_stage",
        help="Glue database for transient external tables (default: bench_stage).",
    )
    parser.add_argument(
        "--output-loc",
        default="",
        help="Athena result s3:// location (default: workgroup-enforced).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log planned per-table actions without touching AWS.",
    )
    parser.add_argument(
        "--db-prefix",
        default="cwd_",
        help="Namespace prefix for the CWD database (default: cwd_).",
    )
    parser.add_argument(
        "--db-name",
        default="acme",
        help="CWD database slug appended to --db-prefix for the namespace "
        "(default: acme => namespace cwd_acme).",
    )
    parser.add_argument(
        "--gold-dialect",
        default="",
        help="Source SQL dialect of the CWD gold SQL for transpile to Trino. "
        "Empty (default) = PASS-THROUGH (no transpile) — set this after "
        "inspecting the real CWD gold SQL at the live gate (e.g. 'postgres', "
        "'mysql', 'sqlite').",
    )
    return parser.parse_args()


def main() -> None:
    """Parse args and run the CWD ingestion pipeline. Returns None."""
    args = _parse_args()
    raw_dir = BENCH_DIR / "raw"
    namespace = f"{args.db_prefix}{args.db_name}"

    # 1. Ensure the raw CWD tree exists (idempotent: skipped if present).
    if raw_dir.exists() and any(raw_dir.iterdir()):
        logger.info("CWD raw data present at %s (skipping download)", raw_dir)
    else:
        # download_cwd fails loudly on the placeholder SHA; under --dry-run it
        # only logs the planned download.
        download_cwd(dest=raw_dir, dry_run=args.dry_run)
        if args.dry_run and not (raw_dir.exists() and any(raw_dir.iterdir())):
            logger.info(
                "[DRY RUN] raw/ absent — would ingest tables and questions after "
                "download; nothing to enumerate. Populate %s to preview per-table "
                "actions.",
                raw_dir,
            )
            return

    # 2. Parse the DDL schema (fail loud if absent) and load question rows up
    #    front so gold-SQL handling is reported even if a later AWS step fails.
    ddl_path = find_ddl(raw_dir=raw_dir)
    tables = parse_ddl_tables(ddl=ddl_path.read_text(encoding="utf-8"))
    logger.info("Parsed %d table(s) from DDL %s", len(tables), ddl_path.name)
    warn_uncovered_csvs(raw_dir=raw_dir, tables=tables)
    # CWD's questions live in a Turtle file (acme-benchmark.ttl), NOT JSON. Locate
    # the benchmark-QUESTION TTL (the one carrying dwt:SqlQuery nodes — NOT the
    # ontology/R2RML TTLs, which are reference-only and never read as questions),
    # then extract the NL-question + gold-SQL pairs from it.
    try:
        ttl_path = find_benchmark_ttl(raw_dir=raw_dir)
        rows = extract_cwd_questions(ttl_path=ttl_path, db_id=namespace)
    except FileNotFoundError as err:
        # No benchmark TTL at all — log loudly but let the DATA ingest succeed
        # (an empty question set). A TTL that EXISTS but yields zero SQL pairs
        # still fails loud inside extract_cwd_questions (format changed).
        logger.warning("No CWD benchmark question TTL located: %s", err)
        rows = []
    logger.info("Loaded %d CWD question row(s)", len(rows))

    # 3. Staging DB, namespace and Lake Formation grants — the same prerequisites
    #    deploy_datasets sets up for BIRD. Without them a fresh account fails the
    #    first CTAS ("Database does not exist: bench_stage", then "Schema ...
    #    cwd_acme not found", then "Required Create Table on cwd_acme").
    ctx = dd.resolve_grant_context(
        args=argparse.Namespace(
            catalog=args.catalog,
            grant_roles="",
            no_auto_grant_agent_roles=False,
            dry_run=args.dry_run,
        )
    )
    ic.safe_ident(name=args.stage_db)
    dd.ensure_stage_db(
        glue=None if args.dry_run else ctx.session.client("glue"),
        stage_db=args.stage_db,
        dry_run=args.dry_run,
    )
    dd.create_namespace_if_absent(
        s3tables=None if args.dry_run else ctx.session.client("s3tables"),
        table_bucket_arn=ctx.table_bucket_arn,
        namespace=namespace,
        dry_run=args.dry_run,
    )
    dd.grant_lf(
        lakeformation=None if args.dry_run else ctx.session.client("lakeformation"),
        catalog_id=ctx.lf_catalog_id,
        namespace=namespace,
        executing_role_arn=ctx.executing_role_arn,
        grant_role_arns=ctx.grant_role_arns,
        dry_run=args.dry_run,
    )

    # 4. AWS clients — created only when actually touching AWS.
    s3 = None if args.dry_run else ctx.session.client("s3")
    athena = None if args.dry_run else ctx.session.client("athena")

    # 5. Ingest every CWD table into the namespace.
    manifest = ic.Manifest(benchmark="cwd")
    entry = ingest_cwd_tables(
        tables=tables,
        raw_dir=raw_dir,
        namespace=namespace,
        bucket=args.bucket,
        catalog=args.catalog,
        workgroup=args.workgroup,
        stage_db=args.stage_db,
        output_loc=args.output_loc,
        s3=s3,
        athena=athena,
        dry_run=args.dry_run,
    )
    manifest.add(entry=entry)

    # 6. Gold-SQL handling. CWD gold SQL dialect is uncertain until the live gate,
    #    so transpile is OPT-IN via --gold-dialect: empty => pass gold SQL through
    #    unchanged (no transpile); set => transpile that dialect -> trino and mark
    #    the manifest transpile_ok=False on any failure (never silent-drop).
    if args.gold_dialect:
        failed = 0
        for row in rows:
            ok, transpiled, err = ic.transpile_gold(
                gold_sql=row.gold_sql, read=args.gold_dialect, write="trino"
            )
            if ok:
                row.gold_sql = transpiled
            else:
                failed += 1
                logger.warning(
                    "Transpile FAILED for db_id=%s (kept original gold SQL): %s "
                    "| SQL=%s",
                    row.db_id,
                    err,
                    row.gold_sql,
                )
        if failed:
            logger.warning(
                "%d/%d gold SQL rows failed to transpile (%s->trino)",
                failed,
                len(rows),
                args.gold_dialect,
            )
            entry.transpile_ok = False
    else:
        logger.info(
            "--gold-dialect empty: passing CWD gold SQL through unchanged (no "
            "transpile). Set --gold-dialect after inspecting the real gold SQL."
        )

    # 7. Filter rows to only ingested db_ids so cwd.jsonl and manifest.json never
    #    drift (mirrors ingest_bird). Log any excluded rows.
    ingested_ids = {e.db_id for e in manifest.entries}
    rows_to_write = [r for r in rows if r.db_id in ingested_ids]
    dropped = len(rows) - len(rows_to_write)
    if dropped:
        logger.info("Excluding %d rows for non-ingested db_ids from cwd.jsonl", dropped)

    # 8. Emit the normalized dataset + manifest (always local; no AWS). Under
    #    --dry-run do NOT clobber the canonical outputs with sentinel (-1 count)
    #    data — only log what would be written.
    jsonl_path = BENCH_DIR / "cwd.jsonl"
    manifest_path = BENCH_DIR / "manifest.json"
    if args.dry_run:
        logger.info(
            "[DRY RUN] would write %d rows to %s and a manifest with %d entries to %s",
            len(rows_to_write),
            jsonl_path,
            len(manifest.entries),
            manifest_path,
        )
    else:
        write_jsonl(rows=rows_to_write, path=jsonl_path)
        manifest.save(path=manifest_path)
        logger.info(
            "Wrote %d rows to %s and manifest to %s",
            len(rows_to_write),
            jsonl_path,
            manifest_path,
        )


if __name__ == "__main__":
    main()
