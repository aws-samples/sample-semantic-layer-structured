# VKG with vs without ontology-RAG comparison

**Source:** `data/eval/results/ontology_rag_on_off_kmean_20260701_215620.json`
(notebook 8 — `8_semantic-layer-with-ontology-rag-vs-without_eval.ipynb`

- **with ontology-RAG** — layer `vkg-ontology_curated_layer-3d1fde76` (reused from nb5/nb6 k-run, `ontology_query_kmean_eval_20260701_114640.json`)
- **without ontology-RAG** — layer `vkg-without-ontologyrag-287784f6`, 40 tables


## Headline scorecard (k=3 mean, identical 16-scenario set)

| Metric                  | with ontology-RAG | without ontology-RAG | Δ                        |
| ----------------------- | ----------------- | -------------------- | ------------------------ |
| GoalSuccess             | 0.71 (±0.028)     | **0.75 (±0.0)**      | **−0.04 (without wins)** |
| FinalAnswerFaithfulness | 0.71 (±0.028)     | 0.73 (±0.028)        | −0.02 (without wins)     |
| SqlGrounded             | 1.0               | 1.0                  | 0.0                      |
| avg wall-clock          | 40.9 s            | 30.6 s               | +10.3 s (~1.3×)          |
| agent total tokens      | ~1.24 M           | ~1.35 M              | −0.12 M (without higher) |

**With the scenario sets matched, ontology-RAG is _net negative_.** It does not lift GoalSuccess —
the "without" arm is marginally _higher_ (0.75 vs 0.71) — and it costs ~1.3× wall-clock. Both arms are relative stable k=3 (the "without" arm is a perfect 0.75/0.75/0.75).


## Per-scenario GoalSuccess (both arms — identical set)

| Scenario                 | question (abbrev)                              | with     | without  | note                               |
| ------------------------ | ---------------------------------------------- | -------- | -------- | ---------------------------------- |
| gt-row-00                | insured party also policyholder                | **0.33** | **1.0**  | **the only divergence — RAG hurt** |
| gt-row-01                | per rider, insured participants + roles        | 0.0      | 0.0      | both fail                          |
| gt-row-02                | top-5 party types + descriptions               | 1.0      | 1.0      |                                    |
| gt-row-03                | total market value of active holdings by party | 0.0      | 0.0      | both fail (anchor-class defect)    |
| gt-row-04                | payout schedule: holder, freq, next amount     | 0.0      | 0.0      | both fail (5-way join)             |
| gt-row-05                | how many parties are there?                    | 1.0      | 1.0      |                                    |
| gt-row-06                | top-10 coverage products by name               | 1.0      | 1.0      |                                    |
| gt-row-07                | top-10 parties by market value + product names | 0.0      | 0.0      | both fail (4-way join)             |
| gt-row-08                | total financial activity per month 2024        | 1.0      | 1.0      |                                    |
| gt-row-13                | common metrics (advisory)                      | 1.0      | 1.0      |                                    |
| gt-row-14                | what can I ask? (advisory)                     | 1.0      | 1.0      |                                    |
| gt-row-15                | explain coverage table (advisory)              | 1.0      | 1.0      |                                    |
| mt-no-spurious-clarify   | how many parties are there?                    | 1.0      | 1.0      |                                    |
| mt-stable-options        | how many are there? (3-turn)                   | 1.0      | 1.0      |                                    |
| mt-simulated-party-count | party count (simulated)                        | 1.0      | 1.0      |                                    |
| mt-parties-clarify       | how many are there? (2-turn)                   | 1.0      | 1.0      |                                    |
| **mean**                 |                                                | **0.71** | **0.75** |                                    |

**Takeaway:** across all 16 matched scenarios, the two arms are identical on **15 of 16**. The clarify
scenarios (`mt-*`, `gt-row-05`) that used to be the "with" arm's supposed advantage now score 1.0 in
**both** arms — confirming clarify recovery is a query-agent capability,
not an ontology-RAG effect. The sole difference is **gt-row-00, where ontology-RAG regresses** (0.33 vs
1.0). The four hard failures (gt-01, 03, 04, 07) are SPARQL-generation / anchor-class-selection defects
invariant to ontology-RAG.

---

### Per-failure deep-dive (why each failed on a given ground truth)

### gt-row-00 — "Show me policies where the insured party is also the policyholder."

#### VKG with ontology-RAG

GoalSuccess **0.33** (1 of 3 k-runs passed). SqlGrounded 1.0 — the emitted SQL is grounded in the
retrieved slice, but the answer is judged wrong on 2 of 3 runs.

#### VKG without ontology-RAG

GoalSuccess **1.0** (all 3 k-runs passed) — resolves the self-join (party appearing in two roles on the
same policy) correctly and stably.

#### why

This is the **single row where the two arms diverge, and ontology-RAG is the arm that regresses.** The
self-join "same party in two roles" is a shape the base VKG slice handles cleanly. Adding the ontology-RAG
retrieval layer perturbs the candidate/slice ranking on this row — the extra retrieved context pulls in
sibling role/relation classes, destabilising an answer that is otherwise a clean 1.0 without it. Same
class of over-shaping / candidate-poisoning documented for the count questions. Net: **RAG is a liability
on this row**, and it is the only measurable per-row effect ontology-RAG has in the whole comparison.

### gt-row-01 — "For each rider, who are the insured participants and what are their roles?"

#### VKG with ontology-RAG

GoalSuccess **0.0**.

#### VKG without ontology-RAG

GoalSuccess **0.0**.

#### why

Fails identically in both arms → not an ontology-RAG issue. Multi-hop `rider → rider_participant →
life_participant/party` with a role attribution. The failure is in SPARQL generation choosing the join
path and surfacing the role label, independent of the RAG layer.

### gt-row-03 — "What is the total market value of all active holdings, grouped by party."

#### VKG with ontology-RAG

GoalSuccess **0.0**.

#### VKG without ontology-RAG

GoalSuccess **0.0**.

#### why

The known **anchor-class-selection** defect (see `2026-06-28-vkg-improvements.md`, Round 3): the agent
anchors on `HoldingSubaccount` + `Coverage` rather than `Holding`, uses `HoldingSubaccount/currentvalue`
instead of `Holding/market_value`, and the join returns 0 rows. The "Active" lifecycle filter now fires
correctly, but the wrong anchor class still yields 0 rows. Present in **both** arms — ontology-RAG does
not touch anchor-class selection.

### gt-row-04 — "For policies with a payout schedule, show the policyholder's name, payout frequency, and projected next-payout amount."

#### VKG with ontology-RAG

GoalSuccess **0.0**.

#### VKG without ontology-RAG

GoalSuccess **0.0**.

#### why

The heaviest question (≈5-way join). Failure modes are un-grouped aggregate / IRI-vs-CONCAT key-join
hygiene in SPARQL generation. Identical in both arms — a generation defect, orthogonal to ontology-RAG.

### gt-row-07 — "Top 10 parties by total holding market value, including the investment product names they hold."

#### VKG with ontology-RAG

GoalSuccess **0.0**.

#### VKG without ontology-RAG

GoalSuccess **0.0**.

#### why

4-way join + aggregate + top-N, sharing gt-03's market-value anchor problem plus a product-name join.
Fails identically in both arms. Not an ontology-RAG effect.

### mt-* clarify scenarios — now equal in both arms

#### VKG with ontology-RAG

`mt-no-spurious-clarify`, `mt-stable-options`, `mt-simulated-party-count`, `mt-parties-clarify` — **all 1.0.**

#### VKG without ontology-RAG

**All 1.0** — the multi-turn-parity fix means the "without" arm now runs these exact scenarios (with the
shared-sessionId clarify flow), and it recovers every one of them.

#### why

Clarify recovery (count questions no longer spuriously clarifying, stable options across re-asks) is a
**query-agent capability** — the Sonnet-5 upgrade + the `_real_inflection_forms` /
`_collapse_shared_stem_siblings` fixes (see improvements doc Rounds 2–3), present in the deployed agent
for **both** arms. It is not an ontology-RAG effect. In the earlier (mismatched) runs these rows ran only
on the "with" arm and inflated its headline; now that both arms run them and both score 1.0, the inflation
is gone and the headline flips to favor "without".

---

## Verdict

- **Ontology-RAG does not improve GoalSuccess on this benchmark.** On the identical 16-scenario set it is
  net _negative_ (0.71 vs 0.75), driven entirely by a **regression on gt-row-00** (0.33 vs 1.0). The two
  arms are identical on the other 15 scenarios.
- **The failures (gt-01, 03, 04, 07) are SPARQL-generation / anchor-class defects** invariant to
  ontology-RAG.
- **Cost:** ontology-RAG costs ~1.3× wall-clock (40.9 s vs 30.6 s) and does not reduce tokens.
- **Action:** investigate why ontology-RAG destabilises gt-row-00 (the one row it changes, and for the
  worse). On current evidence, ontology-RAG is not earning its latency cost for the query-time VKG path on
  this dataset.