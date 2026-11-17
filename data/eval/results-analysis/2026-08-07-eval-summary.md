# Eval Summary — full-dataset run on a from-scratch deployment (2026-08-07)

**Date:** 2026-08-07
**Scope:** All evaluation notebooks re-run in order, at full scope
(`MAX_TABLES=0 MAX_DBS=0 MAX_ROWS=0` — no truncation), against a **completely
rebuilt** dev environment. Every number below is measured and unforced. Where a
notebook did not produce scores, that is stated plainly rather than back-filled
from an earlier run.

**Environment:** dev, account `********4087`, `us-east-1`. All 20
`semantic-layer-dev-*` CloudFormation stacks were destroyed and redeployed from
scratch (`CreationTime` 2026-07-28/29) before this run, with the agentcore stack
deployed `--context enableHybrid=true` so the two hybrid runtimes
(`semantic_layer_query` + `semantic_layer_creation`, bringing the account to 7) and
`lambda-api`'s cross-stack imports exist.

**Three agent paths under test:**

| Path             | Runtime                                   | Retrieval strategy                               |
| ---------------- | ----------------------------------------- | ------------------------------------------------ |
| **Semantic RAG** | `semantic_layer_dev_metadata_query`       | KB-retrieved table/column metadata → Athena SQL  |
| **VKG**          | `semantic_layer_dev_ontology_query`       | OWL ontology → SPARQL → Ontop → Athena SQL       |
| **Hybrid**       | `semantic_layer_dev_semantic_layer_query` | thin VKG for joins + RAG for values → Athena SQL |

---

## 1. Source result files

| nb  | What it measures                                 | Result file                                                |
| --- | ------------------------------------------------ | ---------------------------------------------------------- |
| 1   | metadata **generation** agent, 40 curated tables | `metadata_gen_batch_eval_20260804_020942.json`             |
| 2   | Semantic-RAG **query** agent, groundtruth (k=3)  | `metadata_query_kmean_eval_20260804_031427.json`           |
| 4   | ontology **generation** agent, 40 curated tables | `ontology_gen_batch_eval_20260804_104414.json`             |
| 5   | VKG **query** agent, groundtruth (k=3)           | `ontology_query_kmean_eval_20260804_121011.json`           |
| 6   | RAG vs VKG comparator                            | `semantic_rag_vs_vkg_kmean_20260804_121027.json`           |
| 11  | BIRD layer **builds** (11 databases)             | `bird_vkg_built_layers.json`                               |
| 12  | BIRD **benchmark** eval (500 rows)               | `bird_vkg_benchmark_pooled_20260810_043453.json` (§7.5)    |
| 13  | CWD ACME layer **build** + gen eval              | `cwd_vkg_gen_batch_eval_20260807_134659.json`              |
| 14  | CWD **benchmark** eval (45 rows)                 | `cwd_vkg_benchmark_eval_20260807_172155.json` (§5)         |
| 15  | **Hybrid** query agent, groundtruth (k=3)        | `hybrid_query_kmean_eval_20260807_142020.json`             |
| 16  | 3-way Hybrid vs VKG vs RAG comparator            | `semantic_rag_vs_vkg_vs_hybrid_kmean_20260807_142023.json` |

---

## 2. Generation agents (nb1, nb4) — both perfect

> nb13 also scores a generation run (the CWD ontology build) and likewise returns
> 1.000 on both evaluators; it is reported with the benchmark builds in §4.

| Signal                          | nb1 metadata gen                    | nb4 ontology gen     |
| ------------------------------- | ----------------------------------- | -------------------- |
| `Builtin.GoalSuccessRate`       | **1.000**                           | **1.000**            |
| `Builtin.ToolParameterAccuracy` | **1.000** (203 evaluated, 2 failed) | —                    |
| `Builtin.ToolSelectionAccuracy` | 0.980 (203 evaluated, 2 failed)     | —                    |
| `OntologyQuality` (custom)      | —                                   | **1.000**            |
| Tables processed                | 40 / 40                             | 40 / 40              |
| Build wall-clock                | 2893.3 s                            | 7506.2 s             |
| Total tokens                    | 7,833,405                           | _(not instrumented)_ |

**The anti-fabrication guard is doing real work.** nb1 recorded
`guard_drop_identifiers: 14` and `guard_drop_tables: 12` across 40 documents
checked, with **`leaked_identifiers: 0`**. That is 14 hallucinated identifiers
caught _before_ they could enter the semantic layer — the guard is not
decorative, and the end-state is clean.

---

## 3. Query agents on the curated layer — the 3-way comparison (nb2, nb5, nb15, nb16)

All three arms answer the **same** 16-row groundtruth set, k=3 repetitions, scored
by LLM judges plus a `SqlGrounded` check. This is the core result of the run.

| Metric (k=3)            | Semantic RAG (nb2) | VKG (nb5)       | **Hybrid (nb15)** |
| ----------------------- | ------------------ | --------------- | ----------------- |
| GoalSuccess             | **0.82 ± 0.04**    | **0.82 ± 0.16** | 0.41 ± 0.07       |
| FinalAnswerFaithfulness | **0.82 ± 0.04**    | **0.82 ± 0.16** | 0.53 ± 0.05       |
| SqlGrounded             | 0.98 ± 0.03        | **1.00 ± 0.00** | **1.00 ± 0.00**   |
| Avg latency / query     | 86.0 s             | 149.7 s         | **24.4 s**        |
| Avg tokens / query      | 68,703             | 317,714         | **43,069**        |
| Queries emitting SQL    | 11 / 15            | 11 / 15         | **13 / 15**       |

### Finding 1 — RAG and VKG tie on accuracy; RAG is far cheaper

The two mature paths are **statistically indistinguishable on GoalSuccess (0.82
vs 0.82)**. What separates them is cost and consistency:

- **VKG is perfectly grounded** (1.00 vs 0.98) — every answer traces to SQL it
  actually executed. That is the architectural payoff of routing through an
  ontology.
- **VKG costs 4.6× the tokens and 1.7× the latency** for that tie.
- **VKG is much less stable**: ±0.16 vs ±0.04. A 4× wider spread across identical
  repetitions means the VKG path is sensitive to run-to-run variation the RAG path
  is not.

On a **curated, well-modelled layer**, the ontology indirection buys grounding
rigour and provenance, not accuracy — and charges a large premium. VKG's value
proposition rests on grounding guarantees and reusable semantics, not on beating
RAG at answer quality.

### Finding 2 — Hybrid is dramatically cheaper and now perfectly grounded, but less accurate

The hybrid path is the **fastest (24.4 s, 3.5× faster than RAG, 6× faster than
VKG), the cheapest (43k tokens/query, 7.4× less than VKG), and the only arm that
emits SQL on 13 of 15 queries** rather than 11. It also reaches **SqlGrounded
1.00** — matching VKG.

It nevertheless scores lowest on GoalSuccess (0.41). The verbalization fix shipped
in `dbdf4ae` moved it from a near-total failure to a working-but-imperfect agent:

| Signal                  | Pre-fix (`071822`)      | Post-fix (`142020`)   |
| ----------------------- | ----------------------- | --------------------- |
| GoalSuccess             | 0.07                    | **0.41** (5.9×)       |
| FinalAnswerFaithfulness | 0.07                    | **0.53** (7.6×)       |
| SqlGrounded             | 0.93                    | **1.00**              |
| Run status              | `COMPLETED_WITH_ERRORS` | **all 3 `COMPLETED`** |

### Why hybrid loses the remaining 0.59 — a diagnosed taxonomy, not a mystery

Per-scenario scores split cleanly: **5 of 15 rows score a perfect 1.0**, one
scores 0.67, one 0.33, and **8 score 0.0**. Reading the judges' explanations, the
8 zeros are three distinct, separately-fixable defects — none of them a grounding
or SQL-correctness failure:

1. **Over-eager degradation (4 rows: `gt-row-01`, `04`, `07`, and the two
   multi-turn rows).** The agent abandons answerable questions with "data not
   available" or "I couldn't build a query fully grounded in the available
   ontology," naming missing fields (`normalized.life_participant.role`,
   `normalized.party`). The schema slice it assembles is too narrow, so it
   declines rather than joining more broadly. This is the single biggest bucket.
2. **Degraded message emitted where a _clarifying question_ was required (2 rows:
   `mt-stable-options`, `mt-parties-clarify`).** The multi-turn scenarios assert
   the agent asks a clarifying question on turn 1 instead of guessing. It instead
   returns "I found relevant tables but couldn't assemble a complete enough schema
   slice" — a failure message, not a question. The clarify path exists but is
   being pre-empted by the degrade path.
3. **Over-answering metadata questions (2 rows: `gt-row-13`, `gt-row-15`).** These
   assert the answer describes what _could_ be computed **without returning
   specific values**. The agent executes a query anyway and reports concrete
   numbers ("~$173,291 across 6 activities"), violating the assertion. Notably the
   judge confirms the conceptual explanation itself was correct — the agent is
   penalised purely for volunteering data it was asked not to return.
4. **One genuine correctness miss (`gt-row-03`).** It returned 4 parties with
   different values where gold expects 2. This is the only row in the set that is
   a real wrong-answer.

So of 8 zeros: **6 are behavioural-policy defects** (when to degrade, when to
clarify, when to withhold values), **1 is a multi-turn routing defect**, and
**only 1 is an incorrect query result**. That is an encouraging diagnosis — the
retrieval and SQL generation are sound (SqlGrounded 1.00, 13/15 emitting SQL); the
gap is in answer _policy_. Hybrid is a promising but not-yet-competitive third
path, and it is the cheapest one to iterate on.

---

## 4. Benchmark layer builds (nb11, nb13) — all succeeded

**nb11 — BIRD: all 11 layers `status=completed`** (11,531 s ≈ 3.2 h), 75 tables
total:

| Layer                              | Tables |     | Layer                            | Tables |
| ---------------------------------- | ------ | --- | -------------------------------- | ------ |
| `vkg-bird_california_schools`      | 3      |     | `vkg-bird_formula_1`             | 13     |
| `vkg-bird_card_games`              | 6      |     | `vkg-bird_student_club`          | 8      |
| `vkg-bird_codebase_community`      | 8      |     | `vkg-bird_superhero`             | 10     |
| `vkg-bird_debit_card_specializing` | 5      |     | `vkg-bird_thrombosis_prediction` | 3      |
| `vkg-bird_european_football_2`     | 7      |     | `vkg-bird_toxicology`            | 4      |
| `vkg-bird_financial`               | 8      |     |                                  |        |

**nb13 — CWD ACME: `vkg-cwd_acme` built, 13/13 tables**, `status=completed`,
`phase=refinement_complete`, `progressPercent=100`, in **24.1 min** of agent
build time (13:17:56 → 13:41:59 UTC; 1752 s notebook wall-clock including the
eval). nb13 also _scores_ its build, and both evaluators came back perfect:

| Evaluator                  | Score     | Evaluated | Failed |
| -------------------------- | --------- | --------- | ------ |
| `Builtin.GoalSuccessRate`  | **1.000** | 1         | 0      |
| `OntologyQuality` (custom) | **1.000** | 1         | 0      |

Layer construction over auto-discovered benchmark schemas is reliable: 12 of 12
databases built clean on the rebuilt environment, with no manual intervention.

---

## 5. What is NOT in this run, and why

Both benchmark **scoring** notebooks initially produced no scores. Both have since
been run and are reported below (nb12's BIRD re-run completed on 2026-08-10 and is
reported in §7.5). Stating the original failure directly because it shaped the
re-run design:

### nb12 (BIRD, 500 rows) — ran to completion but wrote no scores

All 500 rows were invoked; **377 returned OK, 123 failed, 121 emitted warnings**.
The notebook then exceeded its own 14 h cell timeout without writing an eval
file. Root cause was diagnosed rather than guessed:

The failures cluster at **915–975 s**, immediately past the notebook's
`urlopen(req, timeout=900)` client timeout — i.e. the agent genuinely exceeded
900 s, it was not a server error. Reconstructing session `gt-row-497` from
CloudWatch (trace `6a7532019b3b8986acbe5c037577a1de`, 977 log records, total
**1100.3 s**):

| Elapsed     | Event                                                                              |
| ----------- | ---------------------------------------------------------------------------------- |
| 0–13 s      | init, `fetch_ontology(vkg-bird_financial)`: 6 classes / 42 properties              |
| 33.6 s      | Phase 5 Ontop translation fails — `no NativeNode in reformulated IQ … EMPTY`       |
| 73.2 s      | repair attempt fails — `EXISTS operator is not supported with no common variables` |
| 73.9 s      | `[tier3] supervisor round 1/3` begins                                              |
| 73.9–1100 s | **still round 1** — 157 `translate_sql` calls, 66 Athena queries                   |

The Tier 3 supervisor _loop_ is bounded (`DEFAULT_MAX_ROUNDS = 3`), but **one
round is a single call into a Strands `Agent`, whose ReAct loop has no iteration
cap**. The Ontop errors were _non-convergent_ — they reject the SPARQL shape, so
every retry failed identically. Individual operations were fast (Athena 2–4 s);
the _count_ was the problem. It never reached round 2.

**Fixed in `e3aeba9`** — a per-round `WorkerBudget` with two ceilings (40 tool
calls catches fast-spin; 240 s wall-clock catches slow-spin, where few calls blow
the budget without hitting the call cap), enforced at the top of every worker
tool. The exception type is load-bearing: raising a plain `Exception` inside a
Strands `@tool` does **not** stop the agent — the SDK catches it and hands the
text back to the model as a tool _result_, so the loop continues. Hence
`WorkerBudgetExceeded(BaseException)`. Sized so `75 + 3×240 = 795 s < 900 s`.
20 new unit tests; full suite 1461 passed / 2 skipped.

**This fix is committed but was NOT deployed during this run**, so the numbers
above reflect the _un-budgeted_ agent. `e3aeba9` has since been deployed.

**How the BIRD re-run is structured.** A straight re-run would repeat
the same loss: at concurrency 3, 500 rows is ~14.3 h, i.e. right at the cell
timeout, so any overrun again discards all 500 rows of work before the persist
step. The re-run therefore splits into **11 per-database runs at concurrency 8**
(`scripts/run_bird_per_db_eval.py`), which makes the benchmark _checkpointed_: each
database writes its own result file, so a stall costs one database rather than the
whole run. The largest database is 66 rows.

Two properties of that design are load-bearing:

- **Evaluators are resolved by name** under a stable suffix (`bird_vkg_v1`) instead
  of a fresh uuid per run. AgentCore never garbage-collects evaluators and
  `CreateEvaluator` does not enforce name uniqueness, so create-always would mint
  66 new evaluators (302 already exist in this account) and — worse — score each
  database with a _different_ evaluator object, making the per-DB scores
  non-poolable.
- **Pooling is row-weighted, from per-row events.** Averaging 11 per-DB means would
  weight a 30-row database the same as a 66-row one, so every breakdown carries a
  per-metric row count. And the pooled value cannot come from
  `exec_metric_aggregates.average_score`, which returns `null` whenever any single
  row fails (confirmed again in nb14 below: all three headline aggregates were
  `null` while the per-DB breakdown held real values).

### nb14 (CWD, 45 rows) — ran after the `e3aeba9` deploy; **EX 0.034**

Deferred initially so the hybrid notebooks (nb15/nb16) could be measured first,
then run against the redeployed runtime carrying the Tier 3 `WorkerBudget` fix.
It completed in **3931 s (65 min)** — no cell-timeout, which is itself the first
evidence that the budget fix works: the un-budgeted agent could not finish.

Result file `cwd_vkg_benchmark_eval_20260807_172155.json`, status
`COMPLETED_WITH_ERRORS`, all 45 rows invoked:

| Metric                  | Score      | Scored rows | Rows > 0 |
| ----------------------- | ---------- | ----------- | -------- |
| ExecutionAccuracy       | **0.0345** | 29          | 1        |
| RewardVES               | 0.0345     | 29          | 1        |
| SoftF1                  | 0.0805     | 29          | 3        |
| GoalSuccess             | 0.178      | 45          | 8        |
| FinalAnswerFaithfulness | 0.178      | 45          | 8        |
| SqlGrounded             | **1.000**  | 45          | 45       |

**Why the exec denominator is 29, not 45 — and why that is not an agent failure.**
16 rows returned `score: null` with an empty explanation. Those are
`GoldExecutionFailed` faults: the CWD gold SQL does arithmetic on `claim_amount`
(`ca_lp.claim_amount + ca_lr.claim_amount + …`), but the ingested S3 Tables copy
types that column as `string` — `DESCRIBE cwd_acme.claim_amount` shows
`claim_amount string` while the identifier columns are `bigint`. Athena rejects it
with `TYPE_MISMATCH: Cannot apply operator: varchar + varchar`. Confirmed from the
Athena query history's `StateChangeReason` (the Lambda logs show nothing — the
handler _returns_ `errorCode` rather than logging it, and recorded 48 invocations
with zero errors and zero timeouts). All three codeBased metrics failed the
identical 16 rows (`Evaluated: 29, Failed: 16`); the LLM judges scored all 45,
which is why the two metric families carry different denominators.

This is a **benchmark ingestion typing defect, not a regression** — the fix belongs
in `scripts/benchmarks/deploy_datasets.py`, which must cast the T-SQL numeric
columns instead of landing every column as string. Until then,
`COMPLETED_WITH_ERRORS` on nb14 is the expected outcome, CWD exec metrics must be
quoted as **n=29**, and nothing may be divided by 45.

**Consequence:** CWD now has a real EX measurement (**0.034**, up from the
2026-07-27 baseline of 0/2 but on a 14× larger denominator). BIRD EX has since landed at
**0.1387 pooled across all 11 databases** (up 3.10× from the 0.0448 first measured, after
the telemetry, primary-key and Ontop-quoting fixes) — see §7.5 for the number and what
still caps it. Do not read the §3 judge scores as evidence about execution
correctness — they measure different things on a much easier, curated layer.

---

## 6. Caveats — read before quoting any number

- **`gt-row-00` timed out at ~971–1001 s in every VKG run**, including this one.
  It is a systematically hard row for the VKG path, not a flake.
- **11 of 15 nb5 sessions reported `COMPLETED_WITH_ERRORS`** from `IncompleteRead`
  on the trace fetch. The scores are from the rows that did return; treat nb5's
  ±0.16 spread with that in mind.
- **nb15 is the cleanest of the three query runs**: all 3 repetitions returned
  `COMPLETED` (no `COMPLETED_WITH_ERRORS`), so hybrid's ±0.07 spread is more
  trustworthy than VKG's ±0.16. Its **pre-fix** comparison run, by contrast, was a
  single `COMPLETED_WITH_ERRORS` repetition — read the 0.07 → 0.41 delta as
  directionally strong but not a like-for-like k=3 comparison.
- **3 of 16 groundtruth rows carry a PLACEHOLDER `Expected_SQL_Query`.** They
  cannot be scored for SQL correctness.
- **1 of 16 scenarios is skipped** in nb2/nb5/nb15 (it needs the
  `bedrock-agentcore[simulation]` extra), so **15 of 16 rows are scored** — this
  is why "queries emitting SQL" reads `11/15`.
- **AgentCore's `exec_metric_aggregates.average_score` returns `null` whenever
  ANY row fails evaluation.** Any consumer must compute EX from the per-row
  events instead of the headline aggregate. nb14 is a live example: all three
  headline aggregates read `null` while the per-row events yielded EX 0.0345.
- **CWD exec metrics have a denominator of 29, not 45.** 16 gold queries fail on
  Athena with `TYPE_MISMATCH: varchar + varchar` because `claim_amount` is ingested
  as `string`. Never divide CWD exec scores by 45, and do not read nb14's
  `COMPLETED_WITH_ERRORS` as an agent regression.
- **Lake Formation grants are ARN-keyed** and are dropped if a redeploy REPLACES
  an agent role. This run's from-scratch deploy required re-granting; re-grant
  after any future role-replacing deploy (runbook in
  `scripts/benchmarks/README.md`).

---

## 7. Bottom line

1. **The from-scratch rebuild is sound.** 20/20 stacks deployed, all three query
   runtimes live, 12/12 benchmark layers built clean, and all three generation
   evals (nb1, nb4, nb13) score a perfect **1.000** with **zero identifier
   leakage** despite the guard dropping 14 hallucinated identifiers.
2. **On a curated layer, Semantic RAG and VKG tie at GoalSuccess 0.82.** VKG buys
   perfect grounding (1.00) and pays 4.6× tokens, 1.7× latency, and 4× the
   variance for it. That trade is the central architectural result of this run.
3. **Hybrid is now viable but not competitive: 0.41 GoalSuccess, and by far the
   cheapest path** (24.4 s, 43k tokens/query — 6× faster and 7.4× cheaper than
   VKG) at SqlGrounded 1.00 with 13/15 queries emitting SQL. The verbalization fix
   lifted it 5.9× from 0.07. Of its 8 zero-scoring rows, **7 are answer-_policy_
   defects** (degrading instead of answering, degrading instead of clarifying,
   returning values when asked not to) and **only 1 is a wrong query result** — so
   the remaining gap is cheap to close relative to its cost advantage.
4. **On a real benchmark layer the VKG path is near-zero on execution accuracy.**
   nb14 (CWD, `e3aeba9` deployed) scores **EX 0.0345 — 1 of 29 scoreable rows** at
   SqlGrounded 1.000 and GoalSuccess 0.178. Read alongside point 2, this is the
   sharpest result in the run: the same architecture that ties RAG at 0.82 on a
   curated 16-row layer answers essentially nothing correctly on a 45-row
   third-party schema. Perfect grounding is not correctness — SqlGrounded 1.000
   only certifies the agent never ran ungrounded SQL, and it is 1.000 partly
   _because_ the agent so often emits no SQL at all. The curated-layer numbers are
   an upper bound on a favourable case, not a capability estimate.
5. **BIRD EX, after the P0–P2 fixes: 0.1387 pooled over all 11 databases (n=447 scored,
   449 answerable of 500 invoked)**, R-VES 0.1203, Soft-F1 0.1426, GoalSuccess 0.6904,
   SqlGrounded 1.0000.

   **Two coverage denominators, both correct — do not read them as contradictory.**
   447/500 = **89.4% of _invoked_ rows**, but 447/449 = **99.55% of _answerable_ rows**:
   51 of the 500 gold queries are invalid on Athena and can never be scored, so they are
   excluded from the denominator rather than counted as EX=0 (the separation introduced in
   `8490737`). Against the answerable denominator no database falls below the 0.95 floor,
   which is what licenses reading this run as a measurement of the agent rather than of
   the harness.

   **This supersedes the 0.0448 first reported here** (2026-08-09, same 11 databases,
   pre-fix). The re-run used `BIRD_EVAL_SUFFIX=bird_vkg_v2` against the rebuilt layers;
   coverage moved only 89.2% → 89.4% of invoked rows (n 446 → 447), so the comparison is
   like-for-like and the gain is the fixes, not a changed denominator:

   | metric            | pre-fix | post-fix |    × |
   | ----------------- | ------: | -------: | ---: |
   | ExecutionAccuracy |  0.0448 |   0.1387 | 3.10 |
   | RewardVES         |  0.0415 |   0.1203 | 2.90 |
   | SoftF1            |  0.0489 |   0.1426 | 2.92 |

   Nine of eleven databases improved and **none regressed** (verified per-DB from the
   per-row events of both runs, not from the aggregates). All **four** former hard zeros
   recovered: `card_games` 0.0 → **0.2157** (GoalSuccess 0.0 → 0.77, the Ontop
   reserved-word/quoting fix), `debit_card_specializing` 0.0 → 0.1481,
   `codebase_community` 0.0 → 0.1304, `formula_1` 0.0 → 0.0714. Best mover:
   `student_club` 0.0476 → **0.2619**. Flat: `toxicology` (0.2000 — its name heuristic was
   already correct on an all-string schema) and `thrombosis_prediction` (0.0541, only 2 of
   3 classes have a verifiable key).

   Per database (EX, n = rows scored for EX):

   | db                      |   n |     EX | GoalSuccess |
   | ----------------------- | --: | -----: | ----------: |
   | student_club            |  42 | 0.2619 |        0.90 |
   | card_games              |  51 | 0.2157 |        0.77 |
   | toxicology              |  40 | 0.2000 |        0.97 |
   | financial               |  27 | 0.1852 |        0.75 |
   | debit_card_specializing |  27 | 0.1481 |        0.66 |
   | codebase_community      |  46 | 0.1304 |        0.78 |
   | california_schools      |  24 | 0.1250 |        0.45 |
   | european_football_2     |  47 | 0.0851 |        0.56 |
   | superhero               |  50 | 0.0800 |        0.58 |
   | formula_1               |  56 | 0.0714 |        0.70 |
   | thrombosis_prediction   |  37 | 0.0541 |        0.46 |

   Two things this number does **not** say. It **beats the 0.1368 conditional upper
   bound** this section previously projected for the clean-SQL/measured subset — that
   bound was the estimated ceiling _if_ row identity were fixed, so exceeding it means
   the key fix generalized past the subset it was diagnosed on. And **EX 0.1387 against
   GoalSuccess 0.6904 is still a 5× gap**: judges accept far more answers than execute
   to the gold result set. Part of that gap is real capability, part is EX's strict
   set-match (column count and order), so GoalSuccess remains the optimistic bound and
   EX the strict one.

6. **Of the three defects listed here, two are fixed and one remains.**
   - ✅ **(a) BIRD telemetry loss** — fixed. Coverage went 71.6% → **99.55%** pooled, and
     invalid gold SQL is now separated from genuine telemetry loss rather than both
     scoring 0 (`8490737`). This had to land first: every other fix was invisible on 28%
     of rows until it did.
   - ✅ **(b) OBDA subject column from a real primary key** — fixed. 72 of 75 mapped
     classes now carry a verified key (was 0/75), the keys are pushed to Neptune and not
     only S3, and rebuilds no longer leave duplicate named graphs behind. Together with
     the Ontop reserved-word/quoting fix this is what moved EX 3.10×.
   - ⬜ **(c) CWD ingestion types every column as `string`** — still open, and still the
     reason CWD execution metrics are n=29 rather than 45. Fix the casts in
     `scripts/benchmarks/deploy_datasets.py` before quoting any CWD number as a 45-row
     result.

   **Next levers, now that measurement is trustworthy.** The largest is the
   **EX 0.1387 vs GoalSuccess 0.6904 gap** — the remaining accuracy work is here, not in
   the harness. Two known contributors: SPARQL→SQL translation still yields no SQL on a
   meaningful share of rows (a defect class separate from the card_games outage — malformed
   SPARQL, SQL-serialization and INTERVAL-conversion failures), and over-projection, where
   an otherwise-correct answer adds an identifying column the question never asked for and
   EX's strict set-match scores it 0. On the curated layer, the hybrid
   degrade/clarify/withhold policy remains the highest-leverage quality fix (7 of its 8
   zero-scoring rows).
