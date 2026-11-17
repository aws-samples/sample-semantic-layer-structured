# Evaluation Dataset & Results

This directory holds the ground-truth dataset and the offline evaluation artifacts that back the
benchmark claims in the root [`README.md`](../../README.md#evaluation--benchmarks). The
[`notebooks/`](../../notebooks/) suite reads the dataset here, runs Amazon Bedrock **AgentCore Batch
Evaluations** against the deployed runtimes, and writes results back into `results/`.

## Contents

| Path                                   | What it is                                                                                                                                                                                                 |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `groundtruth_dataset.json`             | 16 ground-truth rows (`Natural_Language_Question` / `Expected_Answer` / `Expected_SQL_Query` / `Expected_SQL_Result`) + 4 multi-turn scenarios; shared by all query-agent notebooks                        |
| `normalized_layer_enrichment_brief.md` | Enrichment brief used to seed the normalized Semantic-RAG layer (join/label/derivation recipes)                                                                                                            |
| `results/`                             | Raw per-run JSON emitted by the notebooks — `*_batch_eval_*.json` (full per-query records) and `*_kmean_*.json` (k=3 mean/std aggregates, self-contained with generated SQL + GT expectation per scenario) |
| `results-analysis/`                    | Markdown deep-dives that interpret the runs — currently the single consolidated `2026-08-07-eval-summary.md`                                                                                               |

## Scoring

Query-agent runs are scored by three custom SESSION LLM-as-judge evaluators
(`agents/shared/eval_judges.py`): `GoalSuccess`, `FinalAnswerFaithfulness`, and `SqlGrounded`.

- **Read `GoalSuccess`** as the quality signal (k=3 mean over the 16 GT rows + 4 multi-turn scenarios).
- **`SqlGrounded` reads 1.00 across arms** because it scores _vacuously_ on SQL-free rows (degraded /
  advisory / clarify turns emit no SQL); do not read it as a quality delta.

## Headline findings

The current numbers all live in the single consolidated deep-dive,
[`results-analysis/2026-08-07-eval-summary.md`](results-analysis/2026-08-07-eval-summary.md) — the
three-way RAG / VKG / Hybrid comparison on the curated layer (§3), the benchmark layer builds (§4),
and the BIRD and CWD execution-accuracy results (§5, §7).

| Comparison                  | Result                                                                                                                      | Where                 |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| Semantic RAG vs. VKG        | GoalSuccess **0.82** each (tie); VKG pays 4.6× tokens, 1.7× latency for perfect grounding                                   | summary §3, Finding 1 |
| Hybrid vs. RAG / VKG        | GoalSuccess **0.41**, but 6× faster and 7.4× cheaper than VKG at SqlGrounded 1.00                                           | summary §3, Finding 2 |
| BIRD mini-dev (11 DBs, VKG) | **EX 0.1387** / GoalSuccess 0.6904 (n=447 scored, 449 answerable of 500 invoked)                                            | summary §7.5          |
| CWD ACME (VKG)              | **EX 0.034** on n=29 scoreable rows — 16 of 45 gold queries fail on Athena because ingestion types every column as `string` | summary §5            |

> The dated per-comparison analyses from 2026-06-28/29 (raw-vs-normalized, rag-vs-vkg,
> vkg-with-vs-without-RAG, the Opus/Sonnet swap A/B) were pruned in `99602e6` once the consolidated
> summary superseded them on the RAG-vs-VKG comparison. Note that the summary does **not** restate
> their other conclusions — normalization-vs-raw, ontology-pattern RAG on/off, and the Opus/Sonnet
> swap are recorded only as one-line findings in the root
> [`README.md`](../../README.md#headline-findings) and were measured on earlier revisions. Recover
> the files from git history (`git show 99602e6^:<path>`) for the per-failure detail.

## Benchmark datasets

Beyond the hand-built insurance ground truth above, the harness scores the agents on public
text-to-SQL benchmarks ingested into S3 Tables / Athena. Each benchmark lives under
`data/eval/benchmarks/<name>/` and is produced by an idempotent ingestion pipeline (see the runbook
[`scripts/benchmarks/README.md`](../../scripts/benchmarks/README.md)).

### Layout

| Path                              | What it is                                                                 |
| --------------------------------- | -------------------------------------------------------------------------- |
| `benchmarks/cwd/`                 | data.world CWD (ACME_Insurance) — `raw/` + `cwd.jsonl` + `manifest.json`   |
| `benchmarks/bird/`                | BIRD mini-dev — `raw/` + `bird.jsonl` + `manifest.json`                    |
| `benchmarks/spider2/` _(stretch)_ | Spider 2.0-lite SQLite subset — `raw/` + `spider2.jsonl` + `manifest.json` |

`raw/` holds the extracted upstream archive (git-ignored). `manifest.json` maps each `db_id` to its
Athena `database`/`catalog`, ingested `tables`, `row_counts`, and gold-SQL `transpile_ok` status.

### JSONL row schema

Each `<name>.jsonl` file is one normalized [`BenchmarkRow`](../../agents/shared/benchmark_adapter.py)
per line, keeping the ground-truth field names:

| Field                       | Meaning                                                                  |
| --------------------------- | ------------------------------------------------------------------------ |
| `Natural_Language_Question` | The question fed to the agent (required).                                |
| `Expected_SQL_Query`        | The reference (gold) SQL.                                                |
| `db_id`                     | Namespaced source database (e.g. `bird_california_schools`, `cwd_acme`). |
| `difficulty`                | BIRD difficulty band (`simple` / `moderate` / `challenging`).            |
| `evidence`                  | BIRD oracle-knowledge hint.                                              |
| `Expected_Tier`             | Optional tier tag (carried from the legacy insurance rows).              |
| `Expected_Answer`           | Optional NL answer for legacy / advisory rows; omitted when empty.       |

**`Expected_SQL_Result` is intentionally absent.** Result sets are _not_ stored — the gold SQL is
executed at eval time to recompute them (required for the execution metrics anyway, and it avoids
stale frozen results). The loader `agents/shared/benchmark_adapter.load_rows` reads **both** the
legacy `groundtruth_dataset.json` list-of-dict shape and the per-benchmark `.jsonl`, so notebooks and
scorers never branch on the source dataset.

### Scoring: execution metrics alongside the LLM judges

Benchmark runs add an **execution-based metrics track** that runs _alongside_ the existing LLM judges
(`GoalSuccess` / `FinalAnswerFaithfulness` / `SqlGrounded`) — it does not replace them. The metrics
are scored **server-side** by a codeBased AgentCore evaluator (one Lambda / three evaluators,
[`cdk/lib/stacks/backend/benchmark-eval-handler/`](../../cdk/lib/stacks/backend/benchmark-eval-handler/)):
per trace it extracts the agent's predicted SQL from the OTel spans and the gold SQL from
`evaluationReferenceInputs`, executes both on Athena, and computes — via the vendored
[`agents/shared/exec_metrics.py`](../../agents/shared/exec_metrics.py):

- **EX** (Execution Accuracy) — 1 iff the result sets match as order-insensitive multisets (numeric-tolerant; column-order sensitive within a row).
- **R-VES** (Reward-based Valid Efficiency Score) — BIRD's reward-banded efficiency on the gold/pred time ratio; 0 unless the result is correct.
- **Soft-F1** — order/duplicate-tolerant F1 over result rows.

### Pointers

- Driver notebooks — a VKG semantic-layer build + a benchmark eval per benchmark:
  `notebooks/11_bird_semantic_layer.ipynb` + `notebooks/12_bird_benchmark_eval.ipynb` (BIRD),
  `notebooks/13_cwd_semantic_layer.ipynb` + `notebooks/14_cwd_benchmark_eval.ipynb` (CWD).
  Execution metrics (EX / R-VES / Soft-F1) are scored **server-side** by the codeBased AgentCore
  evaluator Lambda (`cdk/lib/stacks/backend/benchmark-eval-handler/`), not client-side in the notebook.
- Ingestion runbook: [`scripts/benchmarks/README.md`](../../scripts/benchmarks/README.md).
