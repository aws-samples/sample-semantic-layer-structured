"""Ingest BIRD mini-dev into S3 Tables/Athena and emit a normalized dataset.

Idempotent CLI (mirrors ``scripts/initial_load_to_iceberg.py``):
  1. Download BIRD mini-dev (SQLite DBs + text-to-SQL JSON) into
     ``data/eval/benchmarks/bird/raw/`` (skipped if present).
  2. For each SQLite DB: export tables -> Parquet -> S3 stage -> external table
     -> DROP+CTAS into the ``bird_<db_id>`` Iceberg namespace; record a
     ``ManifestEntry``.
  3. Transpile each gold SQL SQLite->Trino; capture failures in the manifest.
  4. Write ``bird.jsonl`` (BenchmarkRow shape, no result sets) + ``manifest.json``.

Usage:
    python scripts/benchmarks/ingest_bird.py --bucket <stage-bkt> \\
        --catalog s3tablescatalog/<bench-bkt> --workgroup <wg> [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List

import boto3

from agents.shared.athena_bench import run_sql
from agents.shared.benchmark_adapter import BenchmarkRow, write_jsonl
from scripts.benchmarks import ingest_common as ic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BENCH_DIR = Path("data/eval/benchmarks/bird")
# The BIRD mini-dev DATABASES + question JSON are NOT in the GitHub repo — they
# ship as a separate ~764MB bundle. Verified layout: minidev/MINIDEV/{dev_databases/
# <db>/<db>.sqlite, mini_dev_sqlite.json}. The GitHub archive only contains code +
# an empty mini_dev_data/ pointing here. Alibaba OSS mirror (also on Google Drive):
BIRD_DATA_URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"
# The bundle is not content-addressed; record its sha256 in the runbook after first
# download and verify on re-download to pin the dataset version.
BIRD_DATA_SHA256 = ""  # <TODO: record after first download; empty = skip verify + warn>

# The text-to-SQL question file inside the extracted mini-dev tree, relative to
# ``raw/``. mini-dev ships several SQL-dialect variants; the SQLite variant is the
# one whose gold SQL we transpile SQLite->Trino.
BIRD_QUESTION_FILE = "mini_dev_sqlite.json"


def bird_records_to_rows(*, records: List[Dict], db_prefix: str) -> List[BenchmarkRow]:
    """Convert BIRD text-to-SQL records into normalized rows.

    Args:
        records: Parsed ``mini_dev_sqlite.json`` records (``db_id``/``question``/
            ``evidence``/``SQL``/``difficulty``).
        db_prefix: Namespace prefix applied to each ``db_id`` so BIRD tables
            never collide with the insurance schema (e.g. ``"bird_"``).

    Returns:
        One ``BenchmarkRow`` per record.
    """
    rows: List[BenchmarkRow] = []
    for rec in records:
        rows.append(
            BenchmarkRow(
                question=rec["question"],
                gold_sql=rec.get("SQL", ""),
                db_id=f"{db_prefix}{rec['db_id']}",
                difficulty=rec.get("difficulty", ""),
                evidence=rec.get("evidence", ""),
            )
        )
    return rows


def download_bird(*, dest: Path, dry_run: bool) -> None:
    """Download and extract the BIRD mini-dev data bundle into ``dest``.

    The mini-dev SQLite databases + question JSON ship as a ~764MB bundle from the
    Alibaba OSS mirror (``BIRD_DATA_URL``), NOT from the GitHub repo. When
    ``BIRD_DATA_SHA256`` is set the zip's sha256 is verified (fail loud on
    mismatch); when empty a WARNING is logged that the dataset version is unpinned.

    Args:
        dest: The ``raw/`` directory the bundle is extracted into.
        dry_run: When True, only log the planned download and return without
            touching the network or filesystem.

    Returns:
        None.

    Raises:
        RuntimeError: ``BIRD_DATA_SHA256`` is set but the downloaded zip's sha256
            does not match — refuse to ingest an unexpected dataset version.
    """
    if dry_run:
        logger.info("[DRY RUN] would download %s -> %s", BIRD_DATA_URL, dest)
        return

    # Imported lazily: requests is only needed for the live download path, so the
    # module (and its unit-tested pure function) imports cleanly without it.
    import requests

    logger.info("Downloading BIRD mini-dev data bundle from %s", BIRD_DATA_URL)
    resp = requests.get(BIRD_DATA_URL, timeout=600)
    resp.raise_for_status()
    content = resp.content

    # Pin the dataset version: verify the bundle's sha256 when known, else warn.
    if BIRD_DATA_SHA256:
        import hashlib

        actual = hashlib.sha256(content).hexdigest()
        if actual != BIRD_DATA_SHA256:
            raise RuntimeError(
                "BIRD data bundle sha256 mismatch: expected "
                f"{BIRD_DATA_SHA256}, got {actual}. Refusing to ingest an "
                "unexpected dataset version."
            )
        logger.info("Verified BIRD data bundle sha256 %s", actual)
    else:
        logger.warning(
            "BIRD_DATA_SHA256 is empty — dataset version is UNPINNED. Record the "
            "downloaded bundle's sha256 in scripts/benchmarks/ingest_bird.py (and "
            "the runbook) to pin it."
        )

    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        zf.extractall(dest)
    logger.info("Extracted BIRD mini-dev data bundle into %s", dest)


def find_sqlite_files(*, raw_dir: Path) -> List[Path]:
    """Locate every SQLite database file under the extracted ``raw/`` tree.

    Args:
        raw_dir: The ``raw/`` directory holding the extracted mini-dev tree.

    Returns:
        Sorted list of ``.sqlite`` paths. The data bundle nests each DB as
        ``.../MINIDEV/dev_databases/<db_id>/<db_id>.sqlite`` — glob that exact
        layout so stray/companion ``.sqlite`` files elsewhere are not ingested.
    """
    return sorted(raw_dir.rglob("*/MINIDEV/dev_databases/*/*.sqlite"))


def load_question_records(*, raw_dir: Path) -> List[Dict]:
    """Load the mini-dev SQLite text-to-SQL question records.

    Args:
        raw_dir: The ``raw/`` directory holding the extracted mini-dev tree.

    Returns:
        Parsed JSON records from the first ``mini_dev_sqlite.json`` found.

    Raises:
        FileNotFoundError: The question file is absent — fail loudly rather than
            emit an empty dataset.
    """
    # Confirmed bundle layout: ``.../MINIDEV/mini_dev_sqlite.json``.
    matches = sorted(raw_dir.rglob(f"*/MINIDEV/{BIRD_QUESTION_FILE}"))
    if not matches:
        raise FileNotFoundError(
            f"{BIRD_QUESTION_FILE} not found under {raw_dir} "
            "(expected .../MINIDEV/); is raw/ populated?"
        )
    return json.loads(matches[0].read_text(encoding="utf-8"))


def ingest_database(
    *,
    db_path: Path,
    db_prefix: str,
    bucket: str,
    catalog: str,
    workgroup: str,
    stage_db: str,
    output_loc: str,
    s3: Any,
    athena: Any,
    dry_run: bool,
    stage_prefix: str = "bird",
) -> ic.ManifestEntry:
    """Ingest one SQLite database into the ``<db_prefix><db_id>`` Iceberg namespace.

    For each table: export to Parquet, upload to the S3 stage, create a Glue
    external table over the stage, then DROP+CTAS into Iceberg (drop-then-create
    is the idempotency contract). Under ``dry_run`` no AWS/filesystem work happens
    — only the planned actions are logged.

    Args:
        db_path: Path to the ``<db_id>.sqlite`` file.
        db_prefix: Namespace prefix (e.g. ``"bird_"``).
        bucket: S3 staging bucket for Parquet exports.
        catalog: Athena catalog id (federated S3 Tables catalog).
        workgroup: Athena workgroup name.
        stage_db: Glue database holding the transient external tables.
        output_loc: Athena result ``s3://`` location (empty ⇒ workgroup default).
        s3: boto3 ``s3`` client (unused under ``dry_run``).
        athena: boto3 ``athena`` client (unused under ``dry_run``).
        dry_run: When True, log planned actions and skip all AWS calls.
        stage_prefix: Benchmark slug for the S3 staging key prefix
            (``benchmarks/<stage_prefix>/<namespace>/<table>``). Defaults to
            ``"bird"``; callers reusing this helper for another benchmark (e.g.
            Spider 2.0) MUST pass their own slug so staging Parquet never lands
            under another benchmark's prefix — an operator cleaning one
            benchmark's staging would otherwise wipe the other's.

    Returns:
        A ``ManifestEntry`` recording the resolved namespace, tables and row
        counts (``transpile_ok`` defaults True here; the caller flips it False
        if any of this db's gold SQL failed to transpile).
    """
    db_id = db_path.stem
    namespace = f"{db_prefix}{db_id}"
    tables = ic.sqlite_table_names(db_path=db_path)
    logger.info("[%s] %d tables: %s", namespace, len(tables), ", ".join(tables))

    row_counts: Dict[str, int] = {}
    for table in tables:
        # Per-db staging table name: two BIRD DBs can each have a same-named table
        # (e.g. "students"). A bare name in the shared stage_db + IF NOT EXISTS
        # means the 2nd CREATE is a no-op and the 2nd db materializes the 1st
        # db's data. Namespacing the staging table keeps them distinct.
        # (namespace = db_prefix + db_id; both are safe identifiers.)
        stage_table = f"{namespace}__{table}"
        # S3 key groups every db's Parquet under a stable, namespaced prefix so a
        # re-run overwrites the same objects rather than accumulating stale ones.
        s3_prefix_dir = f"benchmarks/{stage_prefix}/{namespace}/{table}"
        s3_key = f"{s3_prefix_dir}/{table}.parquet"
        s3_prefix = f"s3://{bucket}/{s3_prefix_dir}/"

        if dry_run:
            logger.info(
                "[DRY RUN] [%s.%s] would export Parquet, upload to %s, "
                "create external table, DROP+CTAS into Iceberg",
                namespace,
                table,
                s3_prefix,
            )
            row_counts[table] = -1  # unknown under dry-run (no SQLite read)
            continue

        # 1. SQLite table -> local Parquet (explicit Arrow schema).
        with tempfile.TemporaryDirectory() as tmp:
            local_parquet = Path(tmp) / f"{table}.parquet"
            columns = ic.sqlite_to_parquet(
                db_path=db_path, table=table, out_path=local_parquet
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
        # downstream CTAS materializes an empty Iceberg table (observed 2026-07-29 on
        # CWD; BIRD shares the same bench_stage db and code path). Dropping the Hive
        # external table only removes the catalog entry, never the underlying S3 data,
        # so this is safe and makes re-ingest truly idempotent.
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
                columns=columns,
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
                # Staging external table lives in the Glue default catalog, while
                # the CTAS target lives in the federated S3 Tables catalog. The
                # 3-part source name resolves across that catalog boundary. Task 0
                # spike must confirm this cross-catalog CTAS form against the real
                # S3 Tables catalog.
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
        tables=tables,
        row_counts=row_counts,
        transpile_ok=True,
    )


def _parse_args() -> argparse.Namespace:
    """Parse the BIRD ingestion CLI arguments.

    Returns:
        The parsed ``argparse.Namespace``.
    """
    parser = argparse.ArgumentParser(
        description="Ingest BIRD mini-dev into S3 Tables/Athena and emit a "
        "normalized JSONL dataset + manifest.",
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
        help="Ingest only the first N databases (0 = all).",
    )
    parser.add_argument(
        "--db-prefix",
        default="bird_",
        help="Namespace prefix for db_id (default: bird_).",
    )
    return parser.parse_args()


def main() -> None:
    """Parse args and run the BIRD ingestion pipeline. Returns None."""
    args = _parse_args()
    raw_dir = BENCH_DIR / "raw"

    # 1. Ensure the raw mini-dev tree exists (idempotent: skipped if present).
    if raw_dir.exists() and any(raw_dir.iterdir()):
        logger.info("BIRD raw data present at %s (skipping download)", raw_dir)
    else:
        # download_bird fails loudly on the placeholder SHA; under --dry-run it
        # only logs the planned download.
        download_bird(dest=raw_dir, dry_run=args.dry_run)
        if args.dry_run and not (raw_dir.exists() and any(raw_dir.iterdir())):
            logger.info(
                "[DRY RUN] raw/ absent — would ingest databases and questions "
                "after download; nothing to enumerate. Populate %s to preview "
                "per-db actions.",
                raw_dir,
            )
            return

    # 2. Load the question records and build normalized rows up front so gold-SQL
    #    transpilation can be reported even if a later AWS step fails.
    records = load_question_records(raw_dir=raw_dir)
    rows = bird_records_to_rows(records=records, db_prefix=args.db_prefix)
    logger.info("Loaded %d BIRD question records", len(rows))

    # 3. AWS clients — created only when actually touching AWS.
    s3 = None if args.dry_run else boto3.client("s3")
    athena = None if args.dry_run else boto3.client("athena")

    # 4. Enumerate SQLite DBs and ingest each (respecting --limit).
    sqlite_files = find_sqlite_files(raw_dir=raw_dir)
    if args.limit > 0:
        sqlite_files = sqlite_files[: args.limit]
    logger.info("Ingesting %d database(s)", len(sqlite_files))

    manifest = ic.Manifest(benchmark="bird")
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
            stage_prefix="bird",
        )
        manifest.add(entry=entry)

    # 5. Transpile gold SQL and emit bird.jsonl + manifest.json.
    emit_dataset(rows=rows, manifest=manifest, dry_run=args.dry_run)


def emit_dataset(*, rows: List[BenchmarkRow], manifest: ic.Manifest, dry_run: bool) -> None:
    """Transpile gold SQL, then write ``bird.jsonl`` + ``manifest.json``.

    Shared by ``main`` and ``deploy_datasets`` so a driver-only ingest still emits
    the dataset the BIRD eval (notebook 12) loads.

    Args:
        rows: Normalized question rows (``gold_sql`` mutated to Trino in place).
        manifest: Entries for the ingested databases; ``transpile_ok`` is set
            False for any db with a failed gold-SQL transpile.
        dry_run: When True, only log what would be written.
    """
    # Transpile every gold SQL SQLite->Trino; mutate each row's gold_sql on
    # success and mark the owning db's manifest entry transpile_ok=False on any
    # failure. Never silently drop — log the excluded rows.
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
            # Flag the owning db's manifest entry (only if that db was ingested).
            if row.db_id in ingested_ids:
                manifest.entry(db_id=row.db_id).transpile_ok = False
    if failed:
        logger.warning("%d/%d gold SQL rows failed to transpile", failed, len(rows))

    # Filter rows to only ingested DBs so bird.jsonl and manifest.json never
    # drift under --limit (Task 8 scoring calls manifest.entry(db_id) and would
    # hit KeyError for a row whose DB was skipped). Log the excluded rows.
    rows_to_write = [r for r in rows if r.db_id in ingested_ids]
    dropped = len(rows) - len(rows_to_write)
    if dropped:
        logger.info(
            "Excluding %d rows for non-ingested DBs (--limit) from bird.jsonl",
            dropped,
        )

    # Emit the normalized dataset + manifest (always local; no AWS). Under
    # --dry-run do NOT clobber the canonical outputs with sentinel (-1 count)
    # data — only log what would be written.
    jsonl_path = BENCH_DIR / "bird.jsonl"
    manifest_path = BENCH_DIR / "manifest.json"
    if dry_run:
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
