#!/usr/bin/env python3
"""Run the BIRD benchmark eval (notebook 12) once per database, then pool the results.

Why per-DB instead of one 500-row batch
---------------------------------------
The 2026-08-07 full-dataset attempt invoked all 500 rows in a SINGLE notebook cell
and wrote NO scores: 123 non-convergent rows each ran to the client's 900 s
ceiling, so the cell blew past ``run_eval_notebooks.CELL_TIMEOUT`` (14 h) before
reaching its persist step. Every row's work was lost.

Splitting by ``db_id`` makes the run **checkpointed**: each database is its own
notebook execution with its own results file, so a stall costs one database rather
than the whole benchmark. The largest BIRD database is 66 rows, which finishes
comfortably inside the cell timeout at ``BIRD_CONCURRENCY=8``.

Notebook 12 reads two env vars this driver sets:

- ``BIRD_DB``          — scope the row filter to one database.
- ``BIRD_CONCURRENCY`` — ``max_concurrent_scenarios`` for the batch run.

It also reads ``BIRD_EVAL_SUFFIX`` (default ``bird_vkg_v1``), the STABLE evaluator
name suffix that makes evaluator creation idempotent — without it each of the 11
runs would mint 6 new AgentCore evaluators (they are never garbage-collected) and
the per-DB scores would come from different evaluator objects.

Run:
    python scripts/run_bird_per_db_eval.py                    # all built DBs
    python scripts/run_bird_per_db_eval.py bird_financial,... # a subset
    python scripts/run_bird_per_db_eval.py --pool-only        # just re-pool results

Telemetry coverage gates the numbers
------------------------------------
EX / R-VES / Soft-F1 are SPAN-DERIVED: the evaluator Lambda reads the agent's SQL out
of the CloudWatch session spans. A row whose trace has not landed by the time the
evaluator runs is UNMEASURABLE, and the Lambda returns
``errorCode=TelemetryUnavailable`` so the row drops out of the metric's n instead of
being scored 0.0. That makes COVERAGE (measured rows / invoked rows) the first number
to read: the 2026-08-09 run reported pooled EX 0.0448 at 71.6% coverage where the
measured subset was 0.0625. This driver therefore records per-DB coverage, pools it,
and prints it beside every mean. Notebook 12 raises when a single database falls below
``BIRD_MIN_TELEMETRY_COVERAGE`` (default 0.95) — after writing its results file, so a
coverage failure is loud without discarding the run's evidence.

Each database writes:
    notebooks/12_bird_benchmark_eval_run_<db_id>.ipynb        (execution record)
    data/eval/results/bird_vkg_benchmark_eval_<db_id>_*.json  (scores)
and the driver finally writes the pooled roll-up:
    data/eval/results/bird_vkg_benchmark_pooled_<timestamp>.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import nbformat
from dotenv import load_dotenv
from nbclient import NotebookClient

NOTEBOOK_STEM = "12_bird_benchmark_eval"
REPO_ROOT = Path(__file__).resolve().parent.parent
NOTEBOOKS_DIR = REPO_ROOT / "notebooks"
RESULTS_DIR = REPO_ROOT / "data" / "eval" / "results"

# The notebooks load notebooks/.env themselves (their cwd IS notebooks/), but this
# driver runs from the repo root, so it must load the same file explicitly. Without
# it PROJECT_NAME falls back to "semantic-layer" and the DynamoDB layer discovery
# scans "semantic-layer-metadata" — a table that does not exist, because the
# deployed name carries the stage suffix ("semantic-layer-dev-metadata").
# override=False so an explicitly exported env var still wins.
_DOTENV_PATH = NOTEBOOKS_DIR / ".env"
if not _DOTENV_PATH.exists():
    raise FileNotFoundError(
        f"{_DOTENV_PATH} not found — it carries PROJECT_NAME / "
        f"ONTOLOGY_METADATA_TABLE, without which layer discovery scans the wrong table."
    )
load_dotenv(dotenv_path=_DOTENV_PATH, override=False)

# Per-database cell ceiling. The largest BIRD db is 66 rows; at concurrency 8 with
# a 795 s worst-case row that is ~1.8 h of invocation, plus a 420 s span-ingestion
# delay and up to 5400 s of batch polling. 4 h leaves generous headroom while
# still surfacing a genuinely hung run instead of blocking for 14 h.
CELL_TIMEOUT = 14400

# Concurrency for the batch runner inside the notebook (raised from the notebook's
# historical 3). Bounded by the agent runtime's scaling and Bedrock TPM.
DEFAULT_CONCURRENCY = "8"

# Stable evaluator-name suffix — the reuse key. Bump when a judge prompt or a
# codeBased Lambda ARN changes so a new evaluator generation is minted.
DEFAULT_EVAL_SUFFIX = "bird_vkg_v1"

EXEC_METRICS = ("ExecutionAccuracy", "RewardVES", "SoftF1")


def discover_built_db_ids() -> List[str]:
    """Return the BIRD db_ids that have a completed ``vkg-bird_*`` layer, sorted.

    Reads the same DynamoDB metadata table notebook 12 scans, so the driver's DB
    list can never disagree with what the notebook is willing to score.

    Returns:
        Sorted list of db_id strings (e.g. ``['bird_california_schools', ...]``).

    Raises:
        RuntimeError: if neither ONTOLOGY_METADATA_TABLE nor PROJECT_NAME is set,
            or if no completed ``vkg-bird_*`` layer exists.
    """
    import boto3

    # No default project name: the deployed table is "<deployName>-metadata" where
    # deployName carries the stage suffix ("semantic-layer-dev"), and there is no
    # "-dev" fallback anywhere. Guessing "semantic-layer" scans a table that does
    # not exist and fails with an opaque ResourceNotFoundException.
    table_name = os.environ.get("ONTOLOGY_METADATA_TABLE", "").strip()
    if not table_name:
        project_name = os.environ.get("PROJECT_NAME", "").strip()
        if not project_name:
            raise RuntimeError(
                "Neither ONTOLOGY_METADATA_TABLE nor PROJECT_NAME is set — cannot "
                f"resolve the layer metadata table. Both live in {_DOTENV_PATH}."
            )
        table_name = f"{project_name}-metadata"
    table = boto3.resource("dynamodb").Table(table_name)
    found: List[str] = []
    scan_kwargs: Dict[str, object] = {
        "FilterExpression": "contains(id, :p) AND #s = :c",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":p": "vkg-bird_", ":c": "completed"},
    }
    while True:
        page = table.scan(**scan_kwargs)
        for item in page.get("Items", []):
            layer_id = item["id"]
            if layer_id.startswith("vkg-"):
                found.append(layer_id[len("vkg-"):])
        if "LastEvaluatedKey" not in page:
            break
        scan_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if not found:
        raise RuntimeError(
            f"No completed vkg-bird_* layers in {table_name}. "
            "Run 11_bird_semantic_layer.ipynb first."
        )
    return sorted(set(found))


def run_one_db(*, db_id: str, concurrency: str, eval_suffix: str) -> None:
    """Execute notebook 12 scoped to one BIRD database.

    Parameters:
        db_id: The BIRD db_id to scope to (passed through as ``BIRD_DB``).
        concurrency: ``max_concurrent_scenarios`` value (``BIRD_CONCURRENCY``).
        eval_suffix: Stable evaluator-name suffix (``BIRD_EVAL_SUFFIX``).

    Raises:
        FileNotFoundError: the source notebook is missing.
        CellExecutionError: a cell raised — propagated so the caller decides
            whether to continue with the remaining databases.
    """
    src = NOTEBOOKS_DIR / f"{NOTEBOOK_STEM}.ipynb"
    if not src.exists():
        raise FileNotFoundError(f"Notebook not found: {src}")
    # One execution record PER DATABASE. A shared '<stem>_run.ipynb' would be
    # overwritten 11 times, leaving only the last database inspectable.
    out = NOTEBOOKS_DIR / f"{NOTEBOOK_STEM}_run_{db_id}.ipynb"

    print(f"\n{'=' * 70}\n▶ BIRD {db_id} (concurrency={concurrency})\n{'=' * 70}",
          flush=True)
    started = time.time()
    notebook = nbformat.read(src, as_version=4)
    # Clear committed outputs so a crashed run cannot masquerade as a complete one
    # (an un-executed cell keeps execution_count=None instead of a stale count).
    for cell in notebook.cells:
        if cell.cell_type == "code":
            cell.outputs = []
            cell.execution_count = None

    # The kernel inherits this process's environment, so scope is set here.
    os.environ["BIRD_DB"] = db_id
    os.environ["BIRD_CONCURRENCY"] = concurrency
    os.environ["BIRD_EVAL_SUFFIX"] = eval_suffix
    os.environ["MAX_ROWS"] = "0"          # no truncation within the database

    client = NotebookClient(
        notebook,
        timeout=CELL_TIMEOUT,
        kernel_name="python3",
        resources={"metadata": {"path": str(NOTEBOOKS_DIR)}},
        allow_errors=False,
    )
    try:
        client.execute()
    finally:
        # Persist whatever ran, even on failure, so a partial run is inspectable.
        nbformat.write(notebook, out)
    print(f"✅ {db_id} completed in {time.time() - started:.0f}s -> {out.name}",
          flush=True)


def latest_result_for_db(*, db_id: str) -> Optional[Path]:
    """Return the newest per-DB results file for one database, if any.

    Parameters:
        db_id: The BIRD db_id whose results file to locate.
    Returns:
        Path to the most recent matching JSON, or ``None`` if none exists.
    """
    matches = sorted(RESULTS_DIR.glob(f"bird_vkg_benchmark_eval_{db_id}_*.json"))
    return matches[-1] if matches else None


def pool_results(*, db_ids: List[str], failed: Iterable[str] = ()) -> Dict[str, object]:
    """Pool the per-DB results files into one row-weighted BIRD-wide result.

    Means are pooled by ROW COUNT, not by averaging the 11 per-DB averages — a
    30-row database must not carry the same weight as a 66-row one.

    Parameters:
        db_ids: The db_ids to pool (missing results files are reported, not
            silently skipped).
        failed: db_ids whose run failed THIS invocation. They are reported as
            missing rather than pooled from their newest results file, which
            would be an older run's score (2026-10-02: all 11 runs failed and
            the roll-up silently re-reported the 2026-08-10 numbers).
    Returns:
        The pooled roll-up dict (also written to disk by :func:`main`).

    Raises:
        RuntimeError: if not a single per-DB results file was found.
    """
    per_db: Dict[str, object] = {}
    missing: List[str] = []
    # metric -> [total weighted score, total n]
    totals: Dict[str, List[float]] = {m: [0.0, 0.0] for m in EXEC_METRICS}
    for db_id in db_ids:
        path = None if db_id in failed else latest_result_for_db(db_id=db_id)
        if path is None:
            missing.append(db_id)
            continue
        payload = json.loads(path.read_text())
        # Coverage BEFORE means. EX / R-VES / Soft-F1 are span-derived, so a row
        # whose trace had not landed in CloudWatch is unmeasurable and the evaluator
        # Lambda returns errorCode=TelemetryUnavailable — the row leaves the
        # metric's n instead of being scored 0.0. Pooling a partially-measured
        # database silently understates coverage in the BIRD-wide figure, so it is
        # recorded per DB and re-checked here rather than inferred.
        coverage = payload.get("telemetry_coverage") or {}
        by_db = payload.get("exec_metrics_by_db", {}) or {}
        # A per-DB run's exec_metrics_by_db has exactly one key, but read it by
        # db_id so a multi-DB run pools correctly too.
        slice_metrics = by_db.get(db_id, {})
        for metric in EXEC_METRICS:
            mean = slice_metrics.get(metric)
            count = slice_metrics.get(f"{metric}_n")
            if isinstance(mean, (int, float)) and isinstance(count, int) and count:
                totals[metric][0] += float(mean) * count
                totals[metric][1] += count
        per_db[db_id] = {
            "results_file": path.name,
            "status": payload.get("status"),
            "n_rows": payload.get("n_rows"),
            "batch_evaluation_id": payload.get("batch_evaluation_id"),
            "exec_metric_aggregates": payload.get("exec_metric_aggregates"),
            "exec_metrics_by_db": slice_metrics,
            "llm_judge_aggregates": payload.get("llm_judge_aggregates"),
            "telemetry_coverage": coverage,
        }
    if not per_db:
        raise RuntimeError(
            f"No per-DB results files found under {RESULTS_DIR} for {db_ids}"
        )
    pooled = {
        metric: (round(total / n, 4) if n else None)
        for metric, (total, n) in totals.items()
    }
    scored_rows = {metric: int(n) for metric, (_, n) in totals.items()}
    # Pooled coverage: measured rows / ANSWERABLE rows. Two things can leave a row
    # unscored and AgentCore reports both as score=null, because it collapses every
    # evaluator errorCode to a null score:
    #
    #   TelemetryUnavailable — the trace had not landed. A REAL measurement gap.
    #   GoldExecutionFailed  — the benchmark's own gold SQL does not run on Athena.
    #                          Permanently unanswerable; belongs in NO denominator.
    #
    # Dividing by INVOKED rows conflates them and makes the floor lie. On the
    # 2026-08-10 run, 53 rows were unscored → 89.4% coverage against invoked rows,
    # which read as a 10.6% telemetry hole; probing all 53 gold queries showed 51
    # were invalid SQL and only 2 were true telemetry loss, putting real coverage at
    # 99.55% and turning eight "below-floor" databases into a dataset artifact.
    # Each per-DB file records its own unanswerable count (notebook 12 reads the
    # gold_validity.json sidecar); sum them so the pooled figure agrees with the
    # per-DB ones. Falls back to the raw denominator when the sidecar was absent.
    invoked_rows = sum(
        int(d["n_rows"]) for d in per_db.values() if isinstance(d.get("n_rows"), int)
    )
    unanswerable_rows = sum(
        int((d.get("telemetry_coverage") or {}).get("n_unanswerable_gold") or 0)
        for d in per_db.values()
    )
    answerable_rows = invoked_rows - unanswerable_rows
    pooled_coverage = {
        metric: (round(n / answerable_rows, 4) if answerable_rows else None)
        for metric, n in scored_rows.items()
    }
    low_coverage_dbs = sorted(
        db_id
        for db_id, detail in per_db.items()
        if (detail.get("telemetry_coverage") or {}).get("coverage_ok") is False
    )
    return {
        "benchmark": "bird",
        "agent": "ontology_query_agent",
        "pooling": "row-weighted mean over per-DB exec_metrics_by_db",
        "db_ids_requested": db_ids,
        "db_ids_pooled": sorted(per_db),
        "db_ids_missing_results": missing,
        "pooled_exec_metrics": pooled,
        "scored_rows_per_metric": scored_rows,
        "invoked_rows": invoked_rows,
        "unanswerable_gold_rows": unanswerable_rows,
        "answerable_rows": answerable_rows,
        "pooled_telemetry_coverage": pooled_coverage,
        "low_coverage_dbs": low_coverage_dbs,
        "per_db": per_db,
    }


def main() -> int:
    """Run notebook 12 per database and write the pooled roll-up.

    Returns:
        0 if every requested database produced results, 1 if any run failed or
        any results file was missing.
    """
    args = [a for a in sys.argv[1:] if a.strip()]
    pool_only = "--pool-only" in args
    positional = [a for a in args if not a.startswith("--")]

    if positional:
        db_ids = [d.strip() for d in ",".join(positional).split(",") if d.strip()]
    else:
        db_ids = discover_built_db_ids()
    concurrency = os.environ.get("BIRD_CONCURRENCY", DEFAULT_CONCURRENCY)
    eval_suffix = os.environ.get("BIRD_EVAL_SUFFIX", DEFAULT_EVAL_SUFFIX)

    print(f"Databases ({len(db_ids)}): {db_ids}", flush=True)
    print(f"Concurrency: {concurrency}   Evaluator suffix: {eval_suffix}", flush=True)

    failures: Dict[str, str] = {}
    if not pool_only:
        for db_id in db_ids:
            try:
                run_one_db(db_id=db_id, concurrency=concurrency,
                           eval_suffix=eval_suffix)
            except Exception as exc:  # noqa: BLE001
                # Do NOT abort the remaining databases: the whole point of the
                # split is that one stalled database costs only itself. The
                # failure is recorded and reported in the exit status.
                # Keep only the exception's final line: a CellExecutionError's str()
                # carries the whole cell source and traceback (2026-10-02: the
                # pooled JSON embedded the account id and an SSO URL that way).
                last_line = next((ln for ln in reversed(str(exc).splitlines()) if ln.strip()), "")
                failures[db_id] = f"{type(exc).__name__}: {last_line.strip()[:300]}"
                print(f"❌ {db_id} FAILED — {failures[db_id]}", flush=True)

    rollup = pool_results(db_ids=db_ids, failed=failures)
    rollup["failed_runs"] = failures
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"bird_vkg_benchmark_pooled_{stamp}.json"
    out_path.write_text(json.dumps(rollup, indent=2, default=str))

    print(f"\n{'=' * 70}\nPOOLED BIRD EXEC METRICS "
          f"({len(rollup['db_ids_pooled'])}/{len(db_ids)} dbs)\n{'=' * 70}")
    _invoked = rollup["invoked_rows"]
    _answerable = rollup["answerable_rows"]
    _unanswerable = rollup["unanswerable_gold_rows"]
    if _unanswerable:
        print(f"  {_unanswerable} of {_invoked} rows have gold SQL that cannot run "
              f"on Athena (dataset defect) — excluded from the denominator, so "
              f"coverage is measured against {_answerable} answerable rows.")
    for metric in EXEC_METRICS:
        _cov = rollup["pooled_telemetry_coverage"][metric]
        print(f"  {metric:<18}: {rollup['pooled_exec_metrics'][metric]}  "
              f"(n={rollup['scored_rows_per_metric'][metric]}/{_answerable}, "
              f"coverage={_cov})")
    # Coverage is printed WITH the means and warned about immediately after, because
    # a pooled EX read off a partially-measured run is not a benchmark score. The
    # 2026-08-09 run reported EX 0.0448 at 71.6% coverage; the measured subset was
    # 0.0625.
    _min_cov = min([c for c in rollup["pooled_telemetry_coverage"].values()
                    if c is not None], default=0.0)
    if _min_cov < 1.0:
        print(f"\n⚠ Telemetry coverage {_min_cov:.1%}: "
              f"{_answerable - min(rollup['scored_rows_per_metric'].values())} of "
              f"{_answerable} ANSWERABLE rows were UNMEASURABLE (span ingestion), so "
              f"the means above describe the measured subset only.")
    if rollup["low_coverage_dbs"]:
        print(f"⚠ Below the per-DB coverage floor: {rollup['low_coverage_dbs']}")
    if rollup["db_ids_missing_results"]:
        print(f"\n⚠ No results file for: {rollup['db_ids_missing_results']}")
    if failures:
        print(f"⚠ Failed runs: {sorted(failures)}")
    print(f"\n✓ Wrote {out_path}")
    return 1 if (failures or rollup["db_ids_missing_results"]) else 0


if __name__ == "__main__":
    sys.exit(main())
