"""Ingest the Spider 2.0-lite SQLite subset into S3 Tables/Athena (STRETCH tier).

Spider 2.0-lite spans three backing engines: SQLite (local ``.sqlite`` files),
BigQuery, and Snowflake. This pipeline handles ONLY the SQLite subset — it reuses
the proven BIRD SQLite ingest path (``ingest_common`` helpers +
``ingest_bird.ingest_database``): export tables -> Parquet -> S3 stage -> Glue
external table -> DROP+CTAS into a ``spider2_<db_id>`` Iceberg namespace. Every
warehouse-origin database (BigQuery / Snowflake) is DEFERRED to the Snowflake
scoping spike and LOGGED by ``partition_ingestable`` — never silently skipped.

Data source / layout is NOT yet confirmed live (this is the stretch tier). BIRD's
real data turned out to be a separate ~764MB OSS bundle, NOT the GitHub repo, and
Spider 2.0's SQLite subset is likely the same: a bundle whose URL + on-disk layout
must be verified at a future live gate before ingestion. ``SPIDER2_DATA_URL`` is
therefore intentionally EMPTY and ``download_spider2`` fails loud until an operator
confirms the source and sets it (see the ingestion runbook). We deliberately do
NOT hardcode an unverified URL.

Usage:
    python scripts/benchmarks/ingest_spider2.py --bucket <stage-bkt> \\
        --catalog s3tablescatalog/<bench-bkt> --workgroup <wg> [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple

import boto3

from agents.shared.benchmark_adapter import BenchmarkRow, write_jsonl
from scripts.benchmarks import ingest_common as ic
from scripts.benchmarks.ingest_bird import ingest_database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BENCH_DIR = Path("data/eval/benchmarks/spider2")
# INTENTIONALLY EMPTY. Spider 2.0's SQLite-subset data source + on-disk layout are
# not yet verified live (stretch tier). BIRD's real data was a separate OSS bundle
# (NOT the git repo); Spider 2.0 is likely similar. An operator MUST confirm the
# source at the live gate and set this URL (record it + a sha256 in the runbook)
# before ingesting. download_spider2 fails loud while this is empty — do NOT invent
# a URL here.
SPIDER2_DATA_URL = ""  # <TODO: confirm at live gate>

# The Spider 2.0 text-to-SQL question file inside the extracted tree. The exact
# name/shape is confirmed at the live gate; this is the expected spelling for the
# lite SQLite variant whose gold SQL we transpile SQLite->Trino.
SPIDER2_QUESTION_FILE = "spider2lite.jsonl"


def classify_origin(*, db_id: str) -> str:
    """Classify a Spider 2.0 database id by its backing engine.

    Args:
        db_id: The Spider 2.0 database identifier.

    Returns:
        One of ``"bigquery"``, ``"snowflake"``, or ``"sqlite"`` (default).
    """
    low = db_id.lower()
    if "bigquery" in low or ("." in db_id and "public-data" in low):
        return "bigquery"
    if "snowflake" in low:
        return "snowflake"
    return "sqlite"


def partition_ingestable(*, db_ids: List[str]) -> Tuple[List[str], List[str]]:
    """Split db_ids into Athena-ingestable (sqlite) vs. deferred (warehouse).

    Args:
        db_ids: All Spider 2.0 database ids in the split.

    Returns:
        ``(ingestable, deferred)`` — ``ingestable`` are sqlite-origin; ``deferred``
        are bigquery/snowflake, logged for the Snowflake spike.
    """
    ingestable = [d for d in db_ids if classify_origin(db_id=d) == "sqlite"]
    deferred = [d for d in db_ids if classify_origin(db_id=d) != "sqlite"]
    if deferred:
        logger.warning(
            "Deferring %d warehouse-origin dbs to Snowflake spike: %s",
            len(deferred),
            deferred,
        )
    return ingestable, deferred


def spider2_records_to_rows(
    *, records: List[Dict], db_prefix: str
) -> List[BenchmarkRow]:
    """Convert Spider 2.0 text-to-SQL records into normalized rows.

    Accepts the common Spider 2.0 field spellings (``instance_id``/``question``/
    ``query``/``db``). The exact field names are confirmed at the live gate; a row
    without a question is skipped with a warning rather than emitted empty.

    Args:
        records: Parsed Spider 2.0 question records.
        db_prefix: Namespace prefix applied to each ``db_id`` so Spider 2.0 tables
            never collide with the insurance schema (e.g. ``"spider2_"``).

    Returns:
        One ``BenchmarkRow`` per record that carries a question.
    """
    rows: List[BenchmarkRow] = []
    for rec in records:
        # Spider 2.0 stores the target database under "db" or "db_id"; the NL query
        # under "question" or "instruction"; gold SQL under "query"/"SQL"/"sql".
        raw_db = rec.get("db") or rec.get("db_id", "")
        question = rec.get("question") or rec.get("instruction", "")
        gold_sql = rec.get("query") or rec.get("SQL") or rec.get("sql", "")
        if not question:
            logger.warning("Skipping Spider 2.0 record without a question: %r", rec)
            continue
        rows.append(
            BenchmarkRow(
                question=question,
                gold_sql=gold_sql,
                db_id=f"{db_prefix}{raw_db}",
                difficulty=rec.get("difficulty", ""),
                evidence=rec.get("evidence", ""),
            )
        )
    return rows


def download_spider2(*, dest: Path, dry_run: bool) -> None:
    """Download and extract the Spider 2.0 SQLite-subset bundle into ``dest``.

    Fails loud while ``SPIDER2_DATA_URL`` is empty — the data source + on-disk
    layout are not yet verified live (stretch tier). An operator must confirm the
    source at the live gate and set ``SPIDER2_DATA_URL`` (see the ingestion
    runbook) before ingesting. This mirrors the BIRD path (whose real data was a
    separate OSS bundle, not the git repo).

    Args:
        dest: The ``raw/`` directory the bundle is extracted into.
        dry_run: When True, only log the planned download and return without
            touching the network or filesystem.

    Returns:
        None.

    Raises:
        RuntimeError: ``SPIDER2_DATA_URL`` is empty — the source/layout must be
            confirmed at the live gate before download.
    """
    if not SPIDER2_DATA_URL:
        raise RuntimeError(
            "Spider 2.0 data source/layout not yet confirmed — see the ingestion "
            "runbook; set SPIDER2_DATA_URL in scripts/benchmarks/ingest_spider2.py "
            "after verifying the SQLite-subset bundle URL + on-disk layout at the "
            "live gate (BIRD's real data was a separate OSS bundle, not the git "
            f"repo). Alternatively pre-populate {dest} manually."
        )
    if dry_run:
        logger.info("[DRY RUN] would download %s -> %s", SPIDER2_DATA_URL, dest)
        return

    # Imported lazily: requests is only needed for the live download path, so the
    # module (and its unit-tested pure functions) imports cleanly without it.
    import requests

    logger.info("Downloading Spider 2.0 SQLite subset from %s", SPIDER2_DATA_URL)
    resp = requests.get(SPIDER2_DATA_URL, timeout=600)
    resp.raise_for_status()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        zf.extractall(dest)
    logger.info("Extracted Spider 2.0 SQLite subset into %s", dest)


def find_sqlite_files(*, raw_dir: Path) -> List[Path]:
    """Locate every SQLite database file under the extracted ``raw/`` tree.

    Args:
        raw_dir: The ``raw/`` directory holding the extracted Spider 2.0 tree.

    Returns:
        Sorted list of ``.sqlite`` paths. The exact nesting is confirmed at the
        live gate; a broad recursive glob is used until the layout is pinned.
    """
    return sorted(raw_dir.rglob("*.sqlite"))


def load_question_records(*, raw_dir: Path) -> List[Dict]:
    """Load the Spider 2.0 SQLite text-to-SQL question records.

    Args:
        raw_dir: The ``raw/`` directory holding the extracted Spider 2.0 tree.

    Returns:
        Parsed records from the first ``spider2lite.jsonl`` (or ``.json``) found.

    Raises:
        FileNotFoundError: The question file is absent — fail loudly rather than
            emit an empty dataset.
    """
    stem = Path(SPIDER2_QUESTION_FILE).stem
    matches = sorted(raw_dir.rglob(f"*{stem}*.jsonl")) + sorted(
        raw_dir.rglob(f"*{stem}*.json")
    )
    if not matches:
        raise FileNotFoundError(
            f"No Spider 2.0 question file (*{stem}*.json[l]) found under {raw_dir}; "
            "confirm the question-file name/layout at the live gate. Is raw/ "
            "populated?"
        )
    qfile = matches[0]
    text = qfile.read_text(encoding="utf-8")
    if qfile.suffix == ".jsonl":
        return [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    return json.loads(text)


def _parse_args() -> argparse.Namespace:
    """Parse the Spider 2.0 ingestion CLI arguments.

    Returns:
        The parsed ``argparse.Namespace``.
    """
    parser = argparse.ArgumentParser(
        description="Ingest the Spider 2.0-lite SQLite subset into S3 Tables/Athena "
        "and emit a normalized JSONL dataset + manifest. Warehouse-origin "
        "(BigQuery/Snowflake) dbs are deferred to the Snowflake spike.",
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
        help="Log planned per-db actions without touching AWS.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Ingest only the first N sqlite databases (0 = all).",
    )
    parser.add_argument(
        "--db-prefix",
        default="spider2_",
        help="Namespace prefix for db_id (default: spider2_).",
    )
    return parser.parse_args()


def main() -> None:
    """Parse args and run the Spider 2.0 SQLite-subset ingestion. Returns None."""
    args = _parse_args()
    raw_dir = BENCH_DIR / "raw"

    # 1. Ensure the raw Spider 2.0 tree exists (idempotent: skipped if present).
    #    download_spider2 fails loud while SPIDER2_DATA_URL is empty (unconfirmed
    #    source); under --dry-run it fails the same way (no silent no-op).
    if raw_dir.exists() and any(raw_dir.iterdir()):
        logger.info("Spider 2.0 raw data present at %s (skipping download)", raw_dir)
    else:
        download_spider2(dest=raw_dir, dry_run=args.dry_run)

    # 2. Load the question records and build normalized rows up front so gold-SQL
    #    transpilation can be reported even if a later AWS step fails.
    records = load_question_records(raw_dir=raw_dir)
    rows = spider2_records_to_rows(records=records, db_prefix=args.db_prefix)
    logger.info("Loaded %d Spider 2.0 question records", len(rows))

    # 3. Enumerate on-disk sqlite DBs, then partition every question db_id by
    #    backing engine. Only sqlite-origin dbs are Athena-ingestable here;
    #    BigQuery/Snowflake dbs are DEFERRED to the Snowflake spike and LOGGED
    #    (never silently dropped) by partition_ingestable.
    sqlite_files = find_sqlite_files(raw_dir=raw_dir)
    # Question db_ids are namespaced (db_prefix + raw db). Strip the prefix to
    # classify by the RAW id so the "bigquery"/"snowflake" markers are visible.
    raw_db_ids = sorted(
        {_strip_prefix(db_id=r.db_id, prefix=args.db_prefix) for r in rows}
    )
    _ingestable_ids, _deferred_ids = partition_ingestable(db_ids=raw_db_ids)

    if args.limit > 0:
        sqlite_files = sqlite_files[: args.limit]
    logger.info("Ingesting %d sqlite database(s)", len(sqlite_files))

    # 4. AWS clients — created only when actually touching AWS.
    s3 = None if args.dry_run else boto3.client("s3")
    athena = None if args.dry_run else boto3.client("athena")

    # 5. Ingest each sqlite DB via the SAME helper BIRD uses (identical Parquet
    #    export -> S3 stage -> external table -> DROP+CTAS contract).
    manifest = ic.Manifest(benchmark="spider2")
    for db_path in sqlite_files:
        entry = ingest_database(
            db_path=db_path,
            db_prefix=args.db_prefix,
            bucket=args.bucket,
            catalog=args.catalog,
            workgroup=args.workgroup,
            stage_db=args.stage_db,
            output_loc=args.output_loc,
            s3=s3,
            athena=athena,
            dry_run=args.dry_run,
            stage_prefix="spider2",
        )
        manifest.add(entry=entry)

    # 6. Transpile every gold SQL SQLite->Trino; mutate each row's gold_sql on
    #    success and mark the owning db's manifest entry transpile_ok=False on any
    #    failure. Never silently drop — log the excluded rows.
    ingested_ids = {e.db_id for e in manifest.entries}
    failed = 0
    for row in rows:
        ok, transpiled, err = ic.transpile_gold(
            gold_sql=row.gold_sql, read="sqlite", write="trino"
        )
        if ok:
            row.gold_sql = transpiled
        else:
            failed += 1
            logger.warning(
                "Transpile FAILED for db_id=%s (kept SQLite gold SQL): %s | SQL=%s",
                row.db_id,
                err,
                row.gold_sql,
            )
            if row.db_id in ingested_ids:
                manifest.entry(db_id=row.db_id).transpile_ok = False
    if failed:
        logger.warning("%d/%d gold SQL rows failed to transpile", failed, len(rows))

    # 7. Filter rows to only ingested (sqlite) DBs so spider2.jsonl and
    #    manifest.json never drift (warehouse/--limit rows are excluded). Log the
    #    excluded rows so nothing is silently dropped.
    rows_to_write = [r for r in rows if r.db_id in ingested_ids]
    dropped = len(rows) - len(rows_to_write)
    if dropped:
        logger.info(
            "Excluding %d rows for non-ingested DBs (warehouse-deferred/--limit) "
            "from spider2.jsonl",
            dropped,
        )

    # 8. Emit the normalized dataset + manifest (always local; no AWS). Under
    #    --dry-run do NOT clobber the canonical outputs with sentinel (-1 count)
    #    data — only log what would be written.
    jsonl_path = BENCH_DIR / "spider2.jsonl"
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


def _strip_prefix(*, db_id: str, prefix: str) -> str:
    """Strip the namespace prefix from a row db_id to recover the raw Spider 2.0 id.

    Args:
        db_id: A namespaced db_id (``<prefix><raw>``).
        prefix: The namespace prefix applied by ``spider2_records_to_rows``.

    Returns:
        The raw db_id (prefix removed if present, else the id unchanged).
    """
    return db_id[len(prefix) :] if db_id.startswith(prefix) else db_id


if __name__ == "__main__":
    main()
