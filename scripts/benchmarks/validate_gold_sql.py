#!/usr/bin/env python3
"""Probe every benchmark row's GOLD SQL on Athena and record which rows are answerable.

Why this exists
---------------
The codeBased exec-metric evaluators (EX / R-VES / Soft-F1) execute the GOLD query
before scoring, and treat a gold failure as an EVALUATOR FAULT
(``errorCode=GoldExecutionFailed``) rather than a score of 0 — correctly, because
the agent cannot be graded against a query that does not run. AgentCore then
collapses EVERY ``errorCode`` to ``score: null`` in the per-row events, so the
evaluation output cannot distinguish the two reasons a row went unscored:

1. ``TelemetryUnavailable`` — the trace had not landed; a REAL measurement gap that
   more ingestion delay or lower latency would fix.
2. ``GoldExecutionFailed`` — the benchmark's own gold SQL is invalid on Trino/Athena;
   a PERMANENT dataset defect that no amount of waiting will fix.

Conflating them makes the coverage gate lie. Measured on the 2026-08-10 BIRD 11-DB
run: 53/500 rows were unscored and reported as 89.4% coverage, implying a 10.6%
telemetry hole. Probing all 53 showed 51 were invalid gold (26 TYPE_MISMATCH from
BIRD's all-varchar column typing, 11 FUNCTION_NOT_FOUND, 10
EXPRESSION_NOT_AGGREGATE, 4 other) and only 2 were true telemetry loss — the real
coverage against answerable rows was 99.55%, and the eight databases "below the
floor" were an artifact of the dataset, not the pipeline.

This script writes that distinction down as a durable artifact so the eval's
coverage denominator can count only rows that are answerable in principle.

Output: ``data/eval/benchmarks/<benchmark>/gold_validity.json``::

    {"benchmark": "bird", "probed_at": "<iso>", "n_rows": 500, "n_valid": 449,
     "invalid": {"<db_id>": {"<scenario_id>": "<athena error>"}}}

Scenario ids match ``agents.shared.eval_multiturn.scenario_id_for`` (``gt-row-NN``,
indexed WITHIN a db_id, which is how notebook 12 scopes a run).

Run:
    python scripts/benchmarks/validate_gold_sql.py                  # all dbs
    python scripts/benchmarks/validate_gold_sql.py --dbs bird_formula_1
    python scripts/benchmarks/validate_gold_sql.py --concurrency 16
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import boto3

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
POLL_INTERVAL_SECS = 2
POLL_MAX_ATTEMPTS = 90  # 180 s — a gold query slower than this is unusable anyway


def load_rows(*, benchmark: str) -> List[Dict[str, str]]:
    """Load a benchmark's rows, tagging each with its per-database scenario id.

    Args:
        benchmark: Benchmark name (the directory under ``data/eval/benchmarks``).

    Returns:
        One dict per row: ``{db_id, scenario_id, question, gold_sql}``.

    Raises:
        FileNotFoundError: the benchmark's ``<benchmark>.jsonl`` is missing.
    """
    path = REPO_ROOT / "data" / "eval" / "benchmarks" / benchmark / f"{benchmark}.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run the benchmark's ingest script first."
        )
    per_db_index: Dict[str, int] = {}
    rows: List[Dict[str, str]] = []
    with path.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            db_id = rec["db_id"]
            # Index WITHIN the db, matching eval_multiturn.scenario_id_for as the
            # notebook applies it: a run is scoped to one db, so row 0 of that db
            # is gt-row-00 regardless of its offset in the full file.
            idx = per_db_index.get(db_id, 0)
            per_db_index[db_id] = idx + 1
            rows.append({
                "db_id": db_id,
                "scenario_id": f"gt-row-{idx:02d}",
                "question": rec["Natural_Language_Question"],
                "gold_sql": rec["Expected_SQL_Query"],
            })
    return rows


def load_catalogs(*, benchmark: str) -> Dict[str, str]:
    """Map each db_id to the Athena catalog it was ingested into.

    Args:
        benchmark: Benchmark name.

    Returns:
        ``{db_id: catalog}``.

    Raises:
        FileNotFoundError: the benchmark manifest is missing.
    """
    path = REPO_ROOT / "data" / "eval" / "benchmarks" / benchmark / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — the benchmark is not deployed.")
    manifest = json.loads(path.read_text())
    return {e["db_id"]: e["catalog"] for e in manifest["entries"]}


def probe_one(
    *, row: Dict[str, str], catalog: str, workgroup: str
) -> Tuple[Dict[str, str], bool, str]:
    """Execute one gold query on Athena and report whether it succeeded.

    A fresh boto3 Session per call: Sessions are not documented thread-safe and this
    runs under a ThreadPoolExecutor.

    Args:
        row: A row from :func:`load_rows`.
        catalog: The Athena catalog for the row's database.
        workgroup: The Athena workgroup to run in.

    Returns:
        ``(row, ok, reason)`` — ``reason`` is the Athena failure text when
        ``ok`` is False, else the empty string.
    """
    athena = boto3.Session().client("athena")
    try:
        query_id = athena.start_query_execution(
            QueryString=row["gold_sql"],
            QueryExecutionContext={"Database": row["db_id"], "Catalog": catalog},
            WorkGroup=workgroup,
        )["QueryExecutionId"]
    except Exception as exc:  # noqa: BLE001 — a rejected submission is an invalid query
        return row, False, f"StartQueryExecution rejected: {exc}"

    for _ in range(POLL_MAX_ATTEMPTS):
        status = athena.get_query_execution(QueryExecutionId=query_id)[
            "QueryExecution"
        ]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            return row, True, ""
        if state in ("FAILED", "CANCELLED"):
            return row, False, status.get("StateChangeReason", state)
        time.sleep(POLL_INTERVAL_SECS)
    return row, False, (
        f"gold query did not settle within "
        f"{POLL_INTERVAL_SECS * POLL_MAX_ATTEMPTS}s"
    )


def main() -> int:
    """Probe every gold query and write the benchmark's gold-validity sidecar.

    Returns:
        Process exit code (0 on success).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", default="bird", help="Benchmark name.")
    parser.add_argument(
        "--dbs", default="", help="Comma-separated db_ids to probe (default: all)."
    )
    parser.add_argument(
        "--concurrency", type=int, default=12, help="Parallel Athena queries."
    )
    parser.add_argument(
        "--workgroup", default="primary", help="Athena workgroup."
    )
    args = parser.parse_args()

    rows = load_rows(benchmark=args.benchmark)
    catalogs = load_catalogs(benchmark=args.benchmark)
    if args.dbs:
        wanted = {d.strip() for d in args.dbs.split(",") if d.strip()}
        rows = [r for r in rows if r["db_id"] in wanted]
        if not rows:
            raise ValueError(f"no rows matched --dbs {args.dbs!r}")

    missing = sorted({r["db_id"] for r in rows} - set(catalogs))
    if missing:
        raise ValueError(
            f"these db_ids have no manifest entry (not deployed?): {missing}"
        )

    print(f"Probing {len(rows)} gold queries at concurrency {args.concurrency}...")
    invalid: Dict[str, Dict[str, str]] = {}
    n_valid = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(
                probe_one,
                row=r,
                catalog=catalogs[r["db_id"]],
                workgroup=args.workgroup,
            )
            for r in rows
        ]
        for done, future in enumerate(futures, start=1):
            row, ok, reason = future.result()
            if ok:
                n_valid += 1
            else:
                invalid.setdefault(row["db_id"], {})[row["scenario_id"]] = reason
            if done % 50 == 0 or done == len(rows):
                print(f"  {done}/{len(rows)} probed, {n_valid} valid")

    out_path = (
        REPO_ROOT / "data" / "eval" / "benchmarks" / args.benchmark
        / "gold_validity.json"
    )
    payload = {
        "benchmark": args.benchmark,
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "workgroup": args.workgroup,
        "scope": sorted({r["db_id"] for r in rows}),
        "n_rows": len(rows),
        "n_valid": n_valid,
        "n_invalid": len(rows) - n_valid,
        "invalid": invalid,
    }
    out_path.write_text(json.dumps(payload, indent=1) + "\n")

    print(f"\nvalid {n_valid}/{len(rows)}  invalid {len(rows) - n_valid}")
    for db_id in sorted(invalid, key=lambda k: -len(invalid[k])):
        print(f"  {db_id:<34} {len(invalid[db_id])} unanswerable")
    print(f"\n✓ Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
