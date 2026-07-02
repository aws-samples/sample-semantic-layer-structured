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
| `results-analysis/`                    | Dated markdown deep-dives that interpret each run                                                                                                                                                          |

## Scoring

Query-agent runs are scored by three custom SESSION LLM-as-judge evaluators
(`agents/shared/eval_judges.py`): `GoalSuccess`, `FinalAnswerFaithfulness`, and `SqlGrounded`.

- **Read `GoalSuccess`** as the quality signal (k=3 mean over the 16 GT rows + 4 multi-turn scenarios).
- **`SqlGrounded` reads 1.00 across arms** because it scores _vacuously_ on SQL-free rows (degraded /
  advisory / clarify turns emit no SQL); do not read it as a quality delta.

## Headline findings

| Comparison                         | Result (GoalSuccess, k=3)                           | Analysis file                                                                |
| ---------------------------------- | --------------------------------------------------- | ---------------------------------------------------------------------------- |
| Normalized S3 vs. raw DynamoDB     | **0.75** vs. **~0.10** (normalization wins big)     | `results-analysis/2026-06-28-raw-vs-normalized-goalsuccess-zero-analysis.md` |
| Semantic RAG vs. VKG               | **0.75** vs. **0.71** (dead heat; RAG ~1.6× faster) | `results-analysis/2026-06-28-rag-vs-vkg.md`                                  |
| VKG with vs. without ontology-RAG  | **0.71** vs. **0.75** (RAG net-neutral)             | `results-analysis/2026-06-28-vkg-without-vs-vkg with.md`                     |
| Opus 4.8 vs. Sonnet 4.6 query swap | +0.08 RAG / +0.02 VKG (within noise; reverted)      | `results-analysis/2026-06-29-opus48-vs-sonnet46-model-swap-ab.md`            |

See `2026-06-28-rag.md` and `2026-06-28-vkg.md` for the per-failure deep-dives behind the RAG and VKG
aggregate scores.
