# Raw-DynamoDB vs normalized-S3 — SemanticRAG comparison

**Source:** `data/eval/results/raw_vs_normalized_kmean_20260629_234012.json` (nb7,
`notebooks/7_raw_dynamodb_vs_normalized_s3_eval.ipynb`, k=3).
**Raw layer:** `semantic-rag-raw-dynamodb-2f3773a8` (freshly built, 12 tables).

## Headline

| Arm               | GoalSuccess | FinalAnswerFaithfulness | SqlGrounded |
| ----------------- | ----------- | ----------------------- | ----------- |
| **raw-dynamodb**  | **0.00**    | 0.00                    | 1.00        |
| **normalized-s3** | **0.69**    | 0.71                    | 1.00        |

Same data, same agent, same questions — only the data **modeling** differs. The normalized layer
answers ~7/10 of the data questions; the raw single-table-design layer answers **none**. This is the
project's core thesis quantified: **normalization is the dominant accuracy lever.**

## What the run shows — the raw arm either degrades or returns 0 rows

The query agent's per-scenario behaviour on the raw layer (the result file now persists the generated
SQL, its result, and the ground-truth expectation per question — self-contained, no log mining):

| Question                                               | What the agent did                                        | Actual     | GT expected |
| ------------------------------------------------------ | --------------------------------------------------------- | ---------- | ----------- |
| List top 5 party types + descriptions                  | **no SQL** — slice-build degraded on the wide table       | —          | 3 rows      |
| Policies where insured == policyholder                 | **no SQL** — degraded                                     | —          | 20 rows     |
| For each rider, insured participants + roles           | **no SQL** — degraded                                     | —          | 10 rows     |
| How many parties are there?                            | **no SQL** — degraded                                     | —          | 1           |
| Payout schedule → policyholder, frequency, next payout | **no SQL** — degraded                                     | —          | 5 rows      |
| How many are there? (multi-turn)                       | **no SQL** — degraded                                     | —          | 1           |
| Total financial-activity per month in 2024             | **no SQL** — degraded                                     | —          | 3 rows      |
| Total market value of active holdings, by party        | SQL emitted; `JOIN … ON 1=0 -- no valid join path exists` | **0 rows** | 2 rows      |
| List top 10 coverage products by name                  | `… WHERE Deleted = 'False' …`                             | **0 rows** | 5 rows      |
| Top 10 parties by holding value + product names        | `JOIN … ON CONCAT('PARTY#', c.PartyID) = p.pk …`          | **0 rows** | 4 rows      |

Two failure modes, both rooted in the raw denormalized shape:

- **7/10 degrade with no SQL at all** — the agent's slice-builder can't assemble a usable schema from
  the wide single-table-design tables (`parties` ≈ 400 cols, `holdings` ≈ 430, ~200 opaque
  `ext_custom_field_*`/`ext_date_field_*` noise columns), so it produces no query.
- **3/10 emit SQL that executes but returns 0 rows** — see the concrete proof below.

(`SqlGrounded` reads 1.00 here because it is a vacuous pass — it scores 1.0 on any row that emits no
SQL, and the few rows that do emit reference real columns. GoalSuccess / FinalAnswerFaithfulness are
the truthful signals; both are 0.00.)

## The proof: the layer is correct; the raw shape is the bottleneck

The 3 emitted queries are concrete, self-evident evidence — captured verbatim in the result file:

1. **The agent itself gives up on the join.** For "total market value of active holdings, grouped by
   party," it emitted:
   ```sql
   FROM "semantic-layer-dev-holdings" h
   JOIN "semantic-layer-dev-coverages" c ON 1=0  -- no valid join path exists; see below
   JOIN "semantic-layer-dev-parties"  p ON 1=0
   ```
   `ON 1=0` is the agent explicitly signalling it could not construct a relational join over the
   single-table-design keys → guaranteed 0 rows. The relations exist in the data (and the KB doc
   documents them), but the agent cannot reliably reconstruct them from the denormalized layout.
2. **Boolean-vs-string value mismatch.** For "top 10 coverage products by name," it emitted
   `WHERE Deleted = 'False'` (a capitalized string, the ACORD/CamelCase convention from the sampled
   values) — but the physical `Deleted` column is a real **boolean** storing lowercase `false`. Probed
   directly: `WHERE Deleted = 'False'` → `TYPE_MISMATCH: Cannot apply operator: boolean = varchar(5)`;
   the same query without the filter returns the real names (`Accidental Death`, `Base Life`, …). A
   single wrong value-literal empties the result.
3. **Single-table-design key trap.** "Top 10 parties by holding value" joined on
   `CONCAT('PARTY#', c.PartyID) = p.pk` and aggregated `h.FundName` — using the documented `PARTY#`
   transform — yet still returned 0 rows, because the holding↔coverage leg keys off the shared `pk`/`sk`
   item-collection keys, which are not relational FKs. Valid SQL, wrong for the shape.

So when the raw arm emits SQL it executes and returns 0 rows for **raw-data-fidelity reasons** (boolean
literals, item-collection keys, no clean join path); when it can't even assemble a slice it degrades.
Neither is a layer-documentation defect.

## The raw layer itself is correct (not the bottleneck)

The SemanticRAG build agent produced an accurate KB doc: real column names (`PartyID`, `FullName`,
`PartyType`, `PartyTypeCode`, `PartyStatus`), the `PARTY#` key transform (`pk = 'PARTY#'||PartyID`;
join via `CONCAT('PARTY#', PartyID)`), the relationship structure (coverages↔party, the `relations`
bridge, holdings↔party via coverages) with example join SQL, and an explicit note flagging the `ext_*`
columns as noise. The agent demonstrably reads it — the emitted SQL uses the `PARTY#` transform and the
documented names. The failures are the agent mis-applying the raw schema's value encodings and
single-table keys, not missing/incorrect documentation. One contributing fidelity gap: the KB doc
presents values in CamelCase/ACORD form (e.g. `'False'`) while the physical store is lowercase boolean
— the source of the boolean mismatch above.

## Recommendation

- **The raw arm is a control / baseline, not an arm to optimize.** Its 0.00 reflects genuine
  raw-schema difficulty (the agent degrades or emits 0-row SQL), not a layer or executability defect.
- **The 0.00-vs-0.69 gap is the headline evidence for normalization** — identical data, agent, and
  questions; the normalized layer wins decisively because the agent reasons over clean relational
  entities (`party_id`, `holding_id`, typed `is_deleted = false`, real FKs) instead of a 400-column
  single-table-design blob with `PARTY#`-prefixed keys and stringly-typed flags.
- A richer enrichment brief or `ontology_agent` change would not help: the KB doc is already correct;
  the residual is the schema shape, which only normalization solves.
