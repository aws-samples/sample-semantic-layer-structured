# Opus 4.8 vs Sonnet 4.6 — query-agent model swap A/B (reverted)

**Test:** a one-time experiment — swapped both query agents (ontology/VKG + metadata/RAG) from
`global.anthropic.claude-sonnet-4-6` to `global.anthropic.claude-opus-4-8`, deployed, ran the nb5 +
nb2 eval k=3 against the same pinned layers + questions (clean A/B — only the model changed), then
**reverted to Sonnet 4.6**. Production runs on Sonnet 4.6; the Opus figures below are from that
reverted experiment (not reproducible from a current notebook run, so they are recorded here).

## Quality result (GoalSuccess, k=3)

| Agent                    | Sonnet 4.6 (production) | Opus 4.8 (experiment) | Verdict              |
| ------------------------ | ----------------------- | --------------------- | -------------------- |
| **RAG** (metadata query) | **0.69**                | **0.77**              | ~flat (within noise) |
| **VKG** (ontology query) | **0.54**                | **0.56**              | ~flat (within noise) |

SqlGrounded = 1.0 on both arms; FAF tracked GoalSuccess. The Sonnet figures are the current production
baseline; both agents swing run-to-run (RAG ~0.69–0.81, VKG ~0.50–0.58) on the multi-turn clarify
scenarios, so neither the +0.08 (RAG) nor the +0.02 (VKG) Opus delta is outside the noise band.

## Cost & latency comparison (k=3 means, per full 16-scenario batch)

> **Token-accounting note** (`project_token_accounting_cache`): `total_tokens` folds Bedrock
> cache-read + cache-write tokens, which is why it is far larger than `input + output`. `input_tokens`
> is the NON-cached input only. For a like-for-like cost estimate I price `input_tokens` (fresh input),
> `output_tokens`, and treat the remainder of `total_tokens` as cache-read (billed ~0.1× input). All
> agents run `cache_config=auto`, so the cache-read share is large and real.

Per-batch token + latency measured during the experiment (k=3 means over the 16-scenario batch):

| Agent | Model      | avg wall-clock | input_tok | output_tok | total_tok (incl. cache) |
| ----- | ---------- | -------------- | --------- | ---------- | ----------------------- |
| VKG   | Sonnet 4.6 | 30.7 s         | 115,646   | 26,695     | 1,439,538               |
| VKG   | Opus 4.8   | 24.3 s         | 164,620   | 12,845     | 1,587,879               |
| RAG   | Sonnet 4.6 | 21.5 s         | 218,948   | 8,880      | 766,050                 |
| RAG   | Opus 4.8   | 19.5 s         | 309,137   | 8,422      | 1,021,248               |

**Cost basis (Bedrock on-demand, us-east-1, per-1M-token list at time of writing):**
Opus 4.8 ≈ **$15 / $75** (input / output) per 1M; Sonnet 4.6 ≈ **$3 / $15** per 1M — i.e. **Opus is
~5× the price per token on both input and output.** Cache reads bill at ~0.1× the input rate.
($/batch below = `input×rate + output×rate + (total−input−output)×0.1×input-rate`.)

**Estimated $/batch (k=3 mean of the 16-scenario batch):**

| Agent     | Sonnet $/batch | Opus $/batch | Opus/Sonnet $ | wall-clock (S→O)                 |
| --------- | -------------- | ------------ | ------------- | -------------------------------- |
| VKG       | ~$1.14         | ~$5.55       | **4.9×**      | 30.7s → 24.3s (Opus ~20% faster) |
| RAG       | ~$0.95         | ~$6.32       | **6.6×**      | 21.5s → 19.5s (Opus ~9% faster)  |
| **Total** | **~$2.09**     | **~$11.87**  | **5.7×**      | both faster on Opus              |

**Verdict from the quantified data:** Opus 4.8 costs **5.7× more per batch** (~$2.09 → ~$11.87) and is
**modestly faster** (lower wall-clock on both agents — Opus generates fewer output tokens, 12.8k/8.4k
vs 26.7k/8.9k, so streaming finishes sooner) — but buys **no durable quality**: both arms land within
the run-to-run noise band of the Sonnet baseline (no deterministic data row flips). The latency edge is
real but minor and does not justify ~6× spend for flat quality. Opus burns **more input tokens** too
(VKG 164k vs 116k, RAG 309k vs 219k) — its longer reasoning inflates the priced input, compounding the
5× rate.

## Critical operational note — Opus 4.8 rejects `temperature`

Opus 4.8 returns `ValidationException: 'temperature' is deprecated for this model` on
Converse/ConverseStream (confirmed live via `aws bedrock-runtime converse`). The Strands `BedrockModel`
uses ConverseStream, so the swap REQUIRED dropping `temperature=0.0` from all 4 query/judge call sites
or the agents would have silently returned 0 tables. (Memory: `project_opus48_temperature_deprecated`.)
The Haiku router keeps temperature. This is the load-bearing gotcha for any future Opus 4.8 swap.

## Per-row findings (the actual lesson)

**The model swap did NOT crack a single generation-ceiling row on either agent.**

- **VKG**: Opus shifted only the noisy **multi-turn clarification** rows; **every deterministic
  data-query row is unchanged** — gt-03 still omits the `holding_status` filter, gt-06 still misreads
  "top 10 BY NAME" as a COUNT, gt-04/07 still fail. The tiny aggregate delta is the multi-turn noise
  band, not a real data-query gain.
- **RAG**: Opus landed within noise of the Sonnet baseline. The rows RAG fails (advisory/edge
  questions) are the SAME ones on both models — a **dataset ceiling, not a model limit**; a stronger
  model didn't exceed it.

## Conclusion — strongest evidence yet that the ceilings are STRUCTURAL

Swapping Sonnet 4.6 → Opus 4.8 (a markedly stronger model) did not break the VKG data-query plateau or
the RAG ceiling. Both are properties of the **dataset + the question→query mapping**, not model
capability — a bigger model reasons the same wrong way on these specific question shapes (omitting a
documented filter, misreading "by name" as "by count"). This corroborates the earlier three-ceiling
finding (Ontop mapping, generation prompt, mechanical regenerate) from a fourth angle: model capacity.

## Decision: REVERTED to Sonnet 4.6

Opus is **~5.7× more expensive** (~$2.09 → ~$11.87 per k=3 batch) and lands within the noise band of the
Sonnet baseline on both agents — no deterministic-row gain. Opus's one genuine edge — modestly lower
latency (it emits fewer output tokens, so streaming finishes ~10–20% sooner) — does not justify ~6×
spend for flat quality. Reverted both agents to Sonnet 4.6 (`temperature=0.0` restored); production
runs on Sonnet. The A/B was worth running — it proved the plateau is not a model-capacity problem, and
quantifies exactly what a model upgrade would (not) buy.
