#!/usr/bin/env python3
"""Execute the eval notebooks in dependency order, headless, one at a time.

Each notebook is run in-place semantics (cwd = notebooks/) via nbclient and the
fully-executed copy is written to ``<stem>_run.ipynb`` beside the original — the
"_run" postfix convention the goal asks to strip afterward. Fails loudly: any
cell error aborts the whole run (no silent continue) so a broken upstream
notebook never lets a downstream comparator read stale/empty results.

Order (dependency-driven):
  2  metadata query agent (SemanticRAG)      insurance ground-truth
  5  ontology query agent (VKG)              insurance ground-truth
  6  SemanticRAG vs VKG                       comparator (needs 2,5)
  11 BIRD VKG layer build                     builds vkg-bird_* layers
  12 BIRD benchmark eval                       needs 11 (script: per-DB driver)
  13 CWD VKG layer build                       builds vkg-cwd_acme
  14 CWD benchmark eval                         needs 13
  15a Hybrid layer build                        script; copies 4's VKG tables
  15 Hybrid query agent eval                    insurance ground-truth (needs 15a)
  16 SemanticRAG vs VKG vs Hybrid              comparator (needs 2,5,15)
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError

# Full dependency-ordered chain. Builder notebooks (1 -> SemanticRAG layer,
# 4 -> VKG ontology layer) run BEFORE their eval notebooks (2, 5) because a
# from-scratch deploy starts with an EMPTY metadata table — the eval notebooks
# resolve "the most-recently-completed <type> layer" and fail loudly if none
# exists. 11/13 build their own benchmark layers; 12/14 eval them.
NOTEBOOK_ORDER = [
    "1_metadata_agent_ac_eval",                            # builds SemanticRAG layer
    "2_metadata_query_agent_ondemand_groundtruth_eval",    # evals SemanticRAG (needs 1)
    "4_ontology_agent_ac_eval",                            # builds VKG ontology layer
    "5_ontology_queryagent_ac_eval",                       # evals VKG (needs 4)
    "6_semantic_rag_vs_vkg",                               # comparator (needs 2,5)
    "11_bird_semantic_layer",                              # builds vkg-bird_* layers
    "12_bird_benchmark_eval",                              # evals BIRD (needs 11)
    "13_cwd_semantic_layer",                               # builds vkg-cwd_acme layer
    "14_cwd_benchmark_eval",                               # evals CWD (needs 13)
    "15a_build_hybrid_layer",                              # builds hybrid layer (needs 4)
    "15_hybrid_queryagent_ac_eval",                        # evals Hybrid (needs 15a)
    "16_hybrid_vs_vkg_vs_rag",                             # comparator (needs 2,5,15)
]

# Steps that run a script instead of a notebook, keyed by their NOTEBOOK_ORDER stem.
#   12  — the full 500-row BIRD batch in ONE notebook cell outran CELL_TIMEOUT and
#         persisted no scores (2026-08-07); the per-DB driver checkpoints each
#         database as its own notebook-12 execution and pools the results.
#   15a — no notebook builds the Hybrid layer; without it notebook 15 fails with
#         "No completed Hybrid (hybrid-*) layer found".
SCRIPT_STEPS = {
    "12_bird_benchmark_eval": "run_bird_per_db_eval.py",
    "15a_build_hybrid_layer": "build_hybrid_layer.py",
}

# Per-notebook cell timeout (seconds). Batch evals + layer builds are slow;
# give each cell a generous ceiling but not unbounded so a hung agent call
# surfaces instead of blocking forever.
#
# Must stay comfortably ABOVE the builder notebooks' own DynamoDB poll budget
# (BUILD_POLL_FLOOR_SECS + 150s/table, so 9600s for the 40-table curated layer).
# Otherwise nbclient kills the cell first and we lose the notebook's own
# fail-loudly message about which build stalled and in which phase.
#
# 14h rather than 4h because nb11 builds ALL 11 BIRD databases inside a SINGLE
# cell, sequentially: 11 builds x a ~4050s per-db budget is far past a 4h ceiling,
# and the previous 14400s value would have had nbclient kill that cell mid-way
# through the db list — turning a diagnosable per-db stall into an opaque timeout.
CELL_TIMEOUT = 50400

# Environment forced onto every notebook so a run of this script always means
# "the entire dataset".
#
# Three builder notebooks ship with deliberately SMALL committed defaults for
# interactive smoke runs — nb4 `MAX_TABLES=1`, nb11/nb13 `MAX_DBS=1`. Those
# defaults silently scoped a full-chain run down to a 1-table VKG ontology and a
# 1-of-11 BIRD build (observed 2026-08-04), which made the downstream
# SemanticRAG-vs-VKG comparison meaningless: SemanticRAG covered all 40 tables
# while VKG covered one, so the gap measured the slice, not the architecture.
# 0 means "no limit" in every one of these notebooks. The eval notebooks already
# default to MAX_ROWS=0, but pin it too so the intent is explicit rather than
# inherited.
FULL_SCOPE_ENV = {
    "MAX_TABLES": "0",   # nb1, nb4: build over every discovered table
    "MAX_DBS": "0",      # nb11, nb13: build a layer for every ingested benchmark db
    "MAX_ROWS": "0",     # nb2, nb5, nb12, nb14, nb15: score every dataset row
}


def run_one(*, stem: str, notebooks_dir: Path) -> None:
    """Execute a single notebook and write ``<stem>_run.ipynb``.

    Args:
        stem: Notebook filename without extension (e.g. ``12_bird_benchmark_eval``).
        notebooks_dir: The ``notebooks/`` directory (used as execution cwd so the
            notebooks' ``../data`` relative paths resolve).

    Raises:
        CellExecutionError: A cell raised — abort the whole run (fail loudly).
        FileNotFoundError: The source notebook is missing.
    """
    src = notebooks_dir / f"{stem}.ipynb"
    if not src.exists():
        raise FileNotFoundError(f"Notebook not found: {src}")
    out = notebooks_dir / f"{stem}_run.ipynb"
    print(f"\n{'=' * 70}\n▶ RUN {stem}\n{'=' * 70}", flush=True)
    t0 = time.time()
    nb = nbformat.read(src, as_version=4)
    # Strip the committed notebook's SAVED outputs before executing. Without this,
    # a crashed run (dead kernel / OOM) persists a `_run.ipynb` in which the
    # not-yet-executed cells still carry their PREVIOUS outputs and
    # execution_counts — indistinguishable from cells that ran successfully this
    # time. That silently misreports a failed run as a complete one (observed
    # 2026-07-29: an OOM-killed nb1 looked "16/16 cells clean" but had written no
    # results file). Cleared outputs mean execution_count is None for every cell
    # that did not actually run, so the artifact is an honest execution record.
    for cell in nb.cells:
        if cell.cell_type == "code":
            cell.outputs = []
            cell.execution_count = None
    client = NotebookClient(
        nb,
        timeout=CELL_TIMEOUT,
        kernel_name="python3",
        resources={"metadata": {"path": str(notebooks_dir)}},
        allow_errors=False,
    )
    try:
        client.execute()
    finally:
        # Always persist whatever executed — even on failure — so partial output
        # is inspectable rather than lost.
        nbformat.write(nb, out)
    print(f"✅ {stem} completed in {time.time() - t0:.0f}s -> {out.name}", flush=True)


def run_script(*, stem: str, script: str, repo_root: Path) -> None:
    """Run a ``SCRIPT_STEPS`` script with this interpreter from the repo root.

    Args:
        stem: The step's ``NOTEBOOK_ORDER`` stem (for logging).
        script: Script filename under ``scripts/``.
        repo_root: Repository root (scripts import ``agents.*`` / ``scripts.*``).

    Raises:
        subprocess.CalledProcessError: The script exited non-zero — abort the run.
    """
    print(f"\n{'=' * 70}\n▶ RUN {stem} (scripts/{script})\n{'=' * 70}", flush=True)
    t0 = time.time()
    subprocess.run([sys.executable, str(repo_root / "scripts" / script)], cwd=repo_root, check=True)
    print(f"✅ {stem} completed in {time.time() - t0:.0f}s", flush=True)


def main() -> None:
    """Run all eval notebooks in order; optional CLI arg restricts to a subset."""
    notebooks_dir = Path(__file__).resolve().parent.parent / "notebooks"
    # Optional: pass a comma-separated list of stems (or numeric prefixes) to run.
    only = None
    if len(sys.argv) > 1 and sys.argv[1].strip():
        only = {a.strip() for a in sys.argv[1].split(",")}
    order = [
        s
        for s in NOTEBOOK_ORDER
        # "15" selects both 15a (Hybrid build) and 15 (its eval).
        if only is None or s in only or s.split("_")[0] in only
        or re.match(r"\d+", s).group() in only
    ]
    print(f"Notebooks to run ({len(order)}): {[s.split('_')[0] for s in order]}", flush=True)
    # Force full scope for every notebook. Set here (not per-notebook) because the
    # kernels inherit this process's environment, and os.environ.get(...) inside a
    # notebook then sees "0" instead of its small committed smoke-run default.
    os.environ.update(FULL_SCOPE_ENV)
    print(
        "Full-scope env: "
        + ", ".join(f"{k}={v}" for k, v in sorted(FULL_SCOPE_ENV.items())),
        flush=True,
    )
    for stem in order:
        if stem in SCRIPT_STEPS:
            run_script(stem=stem, script=SCRIPT_STEPS[stem], repo_root=notebooks_dir.parent)
        else:
            run_one(stem=stem, notebooks_dir=notebooks_dir)
    print(f"\n🏁 ALL {len(order)} steps executed cleanly.", flush=True)


if __name__ == "__main__":
    main()
