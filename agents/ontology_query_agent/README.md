# Ontology Query Agent — Query Resolution Flow (VKG mode)

This document describes the Ontology Query Agent (VKG / Virtual Knowledge Graph
mode). It is the SPARQL/Ontop sibling of the
[`metadata_query_agent`](../metadata_query_agent/README.md) (Semantic-RAG mode):
same two-tier shape and shared graph primitives, but Tier 2 assembles an
**ontology slice**, generates **SPARQL**, and Phase 5 translates that SPARQL to
**SQL** (via Ontop) and runs it on **Athena**.

> **Why this doc exists.** It is easy to assume this agent is a free-form ReAct
> tool loop, or that it runs SPARQL against Neptune. **Neither is true.** There
> is no agent-wide `SYSTEM_PROMPT`. **Neptune holds schema only** (classes,
> properties, mappings) — running `COUNT(*)` against it returns 0 — so the
> generated SPARQL is **lineage only**: Phase 5 hands it to the Ontop translate
> Lambda to get **Athena SQL**, which is what actually executes against the row
> data. The live agent resolves every question with a **two-tier cascade**:
> Tier 1 governed-metric lookup, then a **deterministic Tier 2 Strands graph** of
> plain function calls. The only model-facing prompts on this path are the
> **Phase 3 slice judge** (`tier2/slice_judge.py`), the **Phase 4 SPARQL
> generator** (inline prompt in `tier2/vkg_query_generator.py` / `main.py`), and
> a conditional **Phase 5 SQL-repair** round (`_REPAIR_PROMPT` in `main.py`). The
> only model-id constants live in [`query_prompts.py`](query_prompts.py).

## Diagram

```mermaid
flowchart TD
    Q([User question]) --> CR{"Answers a prior<br/>clarification?<br/>(resolve_clarification_reply)"}
    CR -->|"unique match"| RES["Re-run original question<br/>+ ClarificationResolution<br/>(prune rivals · confirm pick)"]
    CR -->|"no / ambiguous match"| FU["Follow-up contextualization<br/>(contextualize_question)"]
    RES --> T1
    FU --> T1{"Tier 1<br/>governed-metric lookup<br/>(KNN ≥ 0.85)"}

    T1 -->|hit| T1X["execute pre-compiled SQL → Athena"]
    T1X --> R([Return result · metadata.tier=1])

    T1 -->|miss / error| T2START["Tier 2 — deterministic Strands graph<br/>(tier2_resolve · open MCP session · fetch ontology once)"]

    subgraph TIER2 ["TIER 2  (deterministic Strands graph — Phase 1→5, VKG)"]
        direction TB
        P1["Phase 1 · Topic router<br/>KNN/lexical over ontology class+property IRIs<br/>(_GatewayTopicRouter over fetched ontologyJson)"]
        P2{"Phase 2 · Term disambiguation<br/>term → candidate IRI (exact/inflected/substring)"}
        P3["Phase 3 · Ontology-slice builder + judge loop<br/>CONSTRUCT (n_hops) → Turtle → centrality-fit<br/>JUDGE_PROMPT sufficiency"]
        P3B{"Phase 3b · Slice disambiguation<br/>(graph-based)"}
        P4["Phase 4 · SPARQL generate + validate<br/>VkgQueryGenerator (rdflib parse + 1 repair)"]
        P5G{"Phase 5a · Grounding gate<br/>BGP triples vs slice (domain check)"}
        P5T["Phase 5b · SPARQL→SQL translate<br/>Ontop gateway translate_sparql_to_sql (deterministic)"]
        EXEC["Phase 5c · Athena execute (+1 LLM SQL-repair)<br/>row cap · result shaping"]

        P1 --> P2
        P2 -->|clear| P3
        P3 -->|sufficient| P3B
        P3B -->|clear| P4
        P4 -->|ok| P5G
        P5G -->|"miss: real but out-of-slice"| P3
        P5G -->|"miss: hallucinated/misused & rounds<2"| P4
        P5G -->|grounded| P5T
        P5T -->|sql| EXEC
    end

    P1 -.->|no candidates · phase1_empty| DEG([degraded → error answer])
    P2 -.->|ambiguous / low-confidence| CLAR([clarify → needs_clarification JSON])
    P3 -.->|"insufficient · phase3_max_rounds / no-op expand"| DEG
    P3B -.->|ambiguous| CLAR
    P3B -.->|unsupported relationship| DEG
    P4 -.->|sparql_repair_failed| DEG
    P5G -.->|grounding_unresolved · rounds = 2| DEG
    P5T -.->|sparql_translation_failed| DEG
    EXEC -.->|sql_execution_failed · 2 attempts| DEG

    T2START --> P1
    EXEC --> R
    CLAR --> R
    DEG --> R
```

## Where the flow lives in code

| Step                                 | Entry point                                                  | Module                                                                                                                    |
| ------------------------------------ | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| Orchestration                        | `_run_query` → `_run_query_core`                             | [`main.py`](main.py)                                                                                                      |
| Clarification resolution             | `load_pending_clarification` + `resolve_clarification_reply` | [`../shared/clarification.py`](../shared/clarification.py)                                                                |
| Follow-up contextualization          | `contextualize_question`                                     | [`../shared/followup.py`](../shared/followup.py)                                                                          |
| Tier 1 lookup / execute              | KNN governed-metric lookup + Athena execute                  | [`../shared/metric_lookup.py`](../shared/metric_lookup.py) · [`main.py`](main.py)                                         |
| Tier 2 entry + phase deps            | `tier2_resolve` → `_build_phase_deps`                        | [`main.py`](main.py) · [`tier2/workflow.py`](tier2/workflow.py)                                                           |
| Graph + shared primitives            | `PhaseDeps` · `WorkflowContext` · `run_tier2_graph`          | [`tier2/workflow.py`](tier2/workflow.py) · [`../shared/tier2_graph.py`](../shared/tier2_graph.py)                         |
| Phase 1 topic router                 | `_GatewayTopicRouter` · `VkgTopicRouter`                     | [`tier2/vkg_topic_router.py`](tier2/vkg_topic_router.py)                                                                  |
| Phase 2 disambiguation               | `analyze_terms` (shared)                                     | [`../shared/disambiguation_common.py`](../shared/disambiguation_common.py)                                                |
| Phase 3 slice builder + judge        | `VkgSliceBuilder` · `build_slice_judge`                      | [`tier2/vkg_slice_builder.py`](tier2/vkg_slice_builder.py) · [`tier2/slice_judge.py`](tier2/slice_judge.py)               |
| Phase 3 CONSTRUCT / gateway          | `NeptuneConstruct` · typed MCP wrappers                      | [`tier2/neptune_construct.py`](tier2/neptune_construct.py) · [`tier2/gateway_client.py`](tier2/gateway_client.py)         |
| Phase 3b slice disambiguation        | `find_slice_ambiguities`                                     | [`tier2/slice_disambiguation.py`](tier2/slice_disambiguation.py)                                                          |
| Phase 4 SPARQL generate/validate     | `VkgQueryGenerator.generate` · `validate_sparql`             | [`tier2/vkg_query_generator.py`](tier2/vkg_query_generator.py) · [`tier2/sparql_validator.py`](tier2/sparql_validator.py) |
| Phase 5 grounding / translate / exec | `check_grounding` · `_run_execution` · `_repair_sql`         | [`tier2/grounding.py`](tier2/grounding.py) · [`main.py`](main.py)                                                         |

The deterministic phases (1, 2, 3b, the grounding + Ontop-translate + Athena
halves of 5) are **not** LLM agents — they are plain Python functions wrapped by
the `_FnNode` adapter, reading and mutating a single shared `WorkflowContext`
that node functions and conditional-edge predicates both close over. Only three
spots invoke a model: the **Phase 3 slice judge**, the **Phase 4 SPARQL
generator**, and a conditional **Phase 5 SQL-repair**.

## Invocation

- **Runtime:** AgentCore (`BedrockAgentCoreApp`), Python 3.12 container.
- **Entrypoint:** `invoke(payload, context=None)` in [`main.py`](main.py).
- **Payload:** `{id, question | message, sessionId, userId, turnId?}` where `id`
  is the `ontologyId`. The dispatcher routes on `turnId`: present →
  `_chat_stream()` (SSE AG-UI events); absent → `_run_query()` (direct JSON).
- **Returns:** on success `{answer, sql_query (the SPARQL lineage), results
(rows), n_quads, reasoning, metadata}`; on clarification `{needs_clarification:
true, options, clarification}`; on degrade a plain error answer (never a 5xx).

## Follow-up contextualization & clarification resolution (before Tier 1/2)

Identical in spirit to the RAG agent (the code is shared):

- **Follow-up contextualization** (`contextualize_question`,
  [`../shared/followup.py`](../shared/followup.py)) rewrites _"again, how many?"_
  into a standalone question using chat history before Tier 1 runs. Fail-soft.
- **Clarification resolution** — when the previous assistant turn was a
  clarification, this turn's message is the user's **selection**.
  `load_pending_clarification` + `resolve_clarification_reply`
  ([`../shared/clarification.py`](../shared/clarification.py)) match it to one
  offered option; on a unique match the agent re-runs the **original** question
  with a `ClarificationResolution` threaded into the graph (Phase 1 prunes the
  rivals, Phase 2 treats the chosen IRI as a confident binding). Fail-soft
  throughout.

## Tier 1 — Governed-metric lookup

The **first thing attempted for every question**, identical to the RAG agent:
hydrate the per-namespace KNN index from the `semantic-layer-metrics` DDB table,
embed the question (Titan v2), KNN search, and require the top hit to clear a
**0.85** cosine threshold. **On a hit**, execute the metric's pre-compiled SQL on
Athena and return with `metadata.tier = 1` — Tier 2 never runs. Every failure
mode (KNN unavailable, below threshold, DDB drift, execution error) is logged and
**falls through to Tier 2** — a governed-metric problem must never block the more
general path.

## Tier 2 — Deterministic Strands graph (VKG)

Reached **only when Tier 1 misses**. `tier2_resolve` opens an MCP session to the
Neptune gateway, **fetches the ontology once**, builds a `PhaseDeps`, and runs
the graph. Routing flags on the shared `WorkflowContext` (`degraded` /
`needs_clarification` / `grounding_missing`) steer the conditional edges.

### Phase 1 — Topic router

([`tier2/vkg_topic_router.py`](tier2/vkg_topic_router.py))

Ranks the ontology's **class and property IRIs** against the question. The
deployed `_GatewayTopicRouter` runs a KNN/lexical score over the **already-fetched
`ontologyJson`** (name + label + comment overlap), with a lexical fallback on
cold start when the KNN index isn't hydrated yet. Output is a ranked candidate
IRI list on `ctx.candidates`. **No candidates → `degraded = "phase1_empty"`.**

### Phase 2 — Term disambiguation

([`../shared/disambiguation_common.py`](../shared/disambiguation_common.py) —
shared with the RAG agent)

Maps each question term to a candidate IRI by **exact local-name**, then
**inflected name** (`inflection_variants`: `parties`↔`party`), then a
token/substring fallback, recording bindings on `ctx.disambiguation`. It escalates
to a `needs_clarification` payload (`clarification_source = "phase2"`) when a term
maps to **>1 distinct candidate IRI**, or on **low confidence** with nothing
lexical to trust. Two signals **suppress** the low-confidence clarification: an
**exact/inflected class-name match**, and a **user-confirmed pick** (`resolved_names`
from a prior clarification, recorded as a CLEAR mapping at confidence 1.0 — without
this a low-confidence clarification is unresolvable by picking and would re-ask
forever).

### Phase 3 — Ontology-slice builder + judge loop

([`tier2/vkg_slice_builder.py`](tier2/vkg_slice_builder.py) ·
[`tier2/slice_judge.py`](tier2/slice_judge.py) ·
[`tier2/neptune_construct.py`](tier2/neptune_construct.py))

1. **Build** — issue a SPARQL **`CONSTRUCT`** around the Phase 1 candidates
   (`n_hops` from the schema graph) via the gateway; `NeptuneConstruct` parses the
   returned **Turtle** into an `rdflib.Graph`. **Bridge classes** (transitive
   connectors between otherwise-disconnected candidates) are folded in so the
   slice carries the join path the SPARQL needs. The graph is serialized to TTL
   and fit to a **12,000-token budget** by **centrality truncation**: Phase 1
   candidates, their direct properties, and immediate domain/range neighbours are
   **force-kept**; lowest-centrality nodes (weighted degree) are evicted first.
   This force-keep is essential — without it a 40-class ontology can drop the
   question's own anchor classes.
2. **`is_sufficient`** — the **slice judge** (`JUDGE_PROMPT`, Sonnet 4.6) returns
   `{sufficient, missing}` reading the TTL **literally** (no schema reasoning
   outside the slice; biased toward `sufficient` for simple single-class
   questions).
3. **`expand` loop** — while insufficient and rounds `< 3`, fold the missing IRIs
   in and re-judge. Three early exits / corrections:
   - **Self-contradiction override** (deterministic): if every `missing` IRI is
     in fact already in the slice (by local name), the slice is authoritative →
     proceed.
   - **No-op `expand` early-exit**: if `expand` can add no new fetchable IRI
     (judge named something this ontology lacks), the rebuilt slice is identical →
     **degrade immediately** rather than spend the remaining rounds.
   - **Round ceiling**: still unsatisfied at round 3 → `degraded =
"phase3_max_rounds"`.

   On `phase3_max_rounds` the graph **short-circuits to `degraded` — it does NOT
   generate SPARQL** against a judge-rejected slice. The degrade message names the
   unmet `missing` list, and the slice + per-round judge verdicts are emitted on
   the phase trace for the UI's reasoning panel.

### Phase 3b — Slice disambiguation guard

([`tier2/slice_disambiguation.py`](tier2/slice_disambiguation.py))

A slice-level guard on the 3→4 edge: heuristic resolution via the slice's own
`subClassOf` / domain / range edges (merged into `ctx.disambiguation`),
**clarification** on a genuine property collision (a needed predicate reachable
from >1 unconnected class → `clarification_source = "phase3b"`), and an
**unsupported-relationship fast-fail** (`degraded = "relationship_unsupported"`)
when the question needs a multi-role relationship the ontology models only
generically — fast-failing before Phase 4 invents a predicate the grounding gate
would reject.

### Phase 4 — SPARQL generator + syntax check

([`tier2/vkg_query_generator.py`](tier2/vkg_query_generator.py) ·
[`tier2/sparql_validator.py`](tier2/sparql_validator.py))

A tightly-scoped Strands agent (Sonnet 4.6, inline system prompt) generates a
SPARQL `SELECT` from the slice + question (plus any `grounding_feedback` fed back
from Phase 5). The prompt is hardened for **Ontop strictness**: full
angle-bracketed IRIs (no PREFIX/CURIE shortcuts — property IRIs nest under their
class, e.g. `…/Address/city`), every non-aggregated `SELECT` var must appear in
`GROUP BY` with the **full expression** (not an alias), and each `(expr AS ?alias)`
must introduce a **new** alias (the projection-alias-reuse fix). The result is
parse-validated with `rdflib.parseQuery` (syntax only); on `SparqlSyntaxError` a
**single repair round** feeds the parse error back, and a second failure sets
`degraded = "sparql_repair_failed"`.

### Phase 5 — Grounding gate → Ontop translate → Athena execute

([`tier2/grounding.py`](tier2/grounding.py) · `_run_execution` / `_repair_sql` in
[`main.py`](main.py))

Phase 5 runs in three halves:

1. **Grounding gate** (`check_grounding`, deterministic) — parse the SPARQL,
   walk the BGP triples, and check every IRI against the slice (a predicate must
   exist on its subject's class via `rdfs:domain`). Misses are **classified** —
   this is the key VKG difference from the RAG agent's single back-edge:
   - **real but out-of-slice** (exists in Neptune, just wasn't fetched by Phase
     3's `n_hops`) → loop **back to Phase 3** to widen the slice;
   - **hallucinated / misused** (fabricated, or a predicate on the wrong class) →
     loop **back to Phase 4** to regenerate with the bad IRIs as a negative
     constraint.

   At grounding round 2 still ungrounded → `degraded = "grounding_unresolved"`.

2. **SPARQL→SQL translation** (`gateway.translate_sparql_to_sql`, deterministic,
   **no model**) — the Ontop Lambda (Java, provisioned-concurrency=1 to stay warm)
   applies the ontology's OBDA mappings (class→table, property→column) and returns
   `{sql, database, catalog}` (Trino dialect). An Ontop error returns immediately
   with `degraded = "sparql_translation_failed"` — Athena is **not** attempted.
3. **Athena execute + repair** (`_run_execution`) — run the translated SQL on
   Athena. Ontop does no retry, so the **agent owns resilience**: on a query
   failure (`state_change_reason` set, not a raise) one **LLM repair round**
   (`_REPAIR_PROMPT`, Sonnet 4.6) fixes **only** the reported error and
   re-executes — **max 2 attempts total**, then `degraded = "sql_execution_failed"`.
   Results are row-capped, shaped into `{columns, rows}`, mapped best-effort into
   `n_quads` for the citation panel, and summarized into prose deterministically
   (`_summarize_select`).

## Result shaping & terminals

After the graph completes, `_run_query` reads the populated `WorkflowContext`:

- **Clarification** (`needs_clarification`, Phase 2 / 3b) → clarification JSON +
  a **pending-clarification record** (original question + offered options) so the
  next turn can resolve the selection.
- **Degraded** (`phase1_empty`, `phase3_max_rounds`, `relationship_unsupported`,
  `sparql_repair_failed`, `grounding_unresolved`, `sparql_translation_failed`,
  `sql_execution_failed`, or any caught exception) → a plain error answer with the
  phase's `degraded_detail` where set, never a 5xx.
- **Success** → the executed Athena SQL, columns/rows, the **SPARQL lineage**, the
  ontology slice (Turtle), n-quad citations, and usage totals, shaped to match the
  Tier 1 payload (`metadata.tier = 2`) so the frontend stays tier-agnostic.

## Tier 2 module inventory

**VKG-specific** ([`tier2/`](tier2/)): `vkg_topic_router.py`,
`vkg_slice_builder.py`, `vkg_query_generator.py`, `slice_judge.py`,
`slice_disambiguation.py`, `grounding.py`, `neptune_construct.py`,
`gateway_client.py`, `sparql_validator.py`, `lexical_match.py`, `workflow.py`.

**Shared** ([`../shared/`](../shared/)): `tier2_graph.py` (graph boilerplate,
`PhaseDeps` / `WorkflowContext` / `_FnNode`), `disambiguation_common.py`
(`analyze_terms`, `inflection_variants`, clarification builders), `clarification.py`,
`followup.py`.

## Notable VKG gotchas

- **Neptune is schema-only.** Never run a count/aggregate against Neptune
  directly — it returns 0 rows. SPARQL is **lineage**; the executed query is the
  Ontop-translated **Athena SQL**.
- **Ontop translation is deterministic and lives in a Lambda** on the Neptune
  gateway; the agent owns _all_ retry/repair (one bounded `_repair_sql` round).
- **Hybrid Phase 5 back-edge** (expand vs regenerate) is unique to VKG — the RAG
  agent only ever loops Phase 5→4, because a hallucinated _column_ can't be
  conjured by widening a slice, whereas a VKG slice is an n-hop CONSTRUCT that
  genuinely may not yet contain a real IRI.
- **Centrality truncation force-keeps Phase 1 candidates** + their direct
  properties/neighbours so the slice never drops the question's anchor classes.
- **Nested-IRI / Ontop-strictness traps** in SPARQL generation (full
  angle-bracketed IRIs, full-expression `GROUP BY`, new projection aliases) — the
  Phase 4 prompt encodes these; `_strip_fences` removes stray ```sparql fences.

## Model(s)

- **Claude Sonnet 4.6** (`global.anthropic.claude-sonnet-4-6`) — Phase 3 judge,
  Phase 4 SPARQL generator, Phase 5 SQL-repair (see `QUERY_MODEL_ID` /
  `JUDGE_MODEL_ID` in [`query_prompts.py`](query_prompts.py)).
- **Titan Text Embeddings v2** (`amazon.titan-embed-text-v2:0`) — Tier 1
  governed-metric KNN and topic-router KNN.

Both appear in [`models.json`](models.json); every model-id literal under this
directory must be listed there (CDK derives `bedrock:InvokeModel` IAM grants from
it; enforced by `tests/unit/test_model_manifests.py`).

## Design docs

- [`docs/plans/shipped/2026-06-04-vkg-phase5-ontop-athena-design.md`](../../docs/plans/shipped/2026-06-04-vkg-phase5-ontop-athena-design.md)
  — the Ontop Lambda + Phase 5 SPARQL→SQL→Athena rewrite.
- [`docs/plans/shipped/2026-05-16-ontop-vkg-design.md`](../../docs/plans/shipped/2026-05-16-ontop-vkg-design.md)
  — the original OBDA / Ontop VKG decision.