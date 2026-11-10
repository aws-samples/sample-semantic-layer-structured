# Ontology Query Agent — Query Resolution Flow (VKG mode)

This document describes the Ontology Query Agent (VKG / Virtual Knowledge Graph
mode). It is the SPARQL/Ontop sibling of the
[`metadata_query_agent`](../metadata_query_agent/README.md) (Semantic-RAG mode):
same three-tier shape and shared graph primitives, but Tier 2 assembles an
**ontology slice**, generates **SPARQL**, and Phase 5 translates that SPARQL to
**SQL** (via Ontop) and runs it on **Athena**.

> Model-facing prompts exist in exactly these spots — everything else is
> deterministic Python:
>
> - **Intent router** (`_router_classify_fn`) and **advisory synthesis**
>   (`_advisory_synthesize`) — off the graph, before Tier 1.
> - In the Tier 2 graph: the **Phase 3 slice judge** (`tier2/slice_judge.py`),
>   the **Phase 4 SPARQL generator** (inline prompt in
>   `tier2/vkg_query_generator.py`), a conditional **Phase 5 SQL-repair** round
>   (`_REPAIR_PROMPT` in `main.py`), and the **Phase 5 answer renderer**
>   (`_render_answer`, `_ANSWER_PROMPT` in `main.py`) that turns the Athena result
>   into the user-facing sentence on the success path.

## Diagram

Nodes marked **📡** emit an OpenTelemetry span the AgentCore evaluation judges
harvest. A 🤖 marks a model (LLM) call; the SDK auto-instruments those as `chat`
spans, while 📡-only nodes emit a hand-rolled span via `emit_answer_span` /
`emit_grounding_span` (see [OTEL spans](#otel-spans--eval-telemetry)).

```mermaid
flowchart TD
    Q([User question]) --> CR{"Answers a prior<br/>clarification?<br/>(resolve_clarification_reply)"}
    CR -->|"unique match"| RES["Re-run original question<br/>+ ClarificationResolution<br/>(prune rivals · confirm pick)"]
    CR -->|"no / ambiguous match"| FU["Follow-up contextualization<br/>(contextualize_question)"]
    RES --> IR
    FU --> IR{"🤖 Intent router<br/>(classify_intent)"}

    IR -->|advisory| ADV["🤖📡 Advisory answer<br/>(build_advisory_answer · grounds in ontology<br/>comments + governed metrics)"]
    ADV --> R([Return result])
    IR -->|data_query / error| T1{"Tier 1<br/>governed-metric lookup<br/>(KNN ≥ 0.85)"}

    T1 -->|hit| T1X["execute pre-compiled SQL → Athena"]
    T1X --> R

    T1 -->|miss / error| RT{"Governed-rule tier<br/>(_rule_tier_answer · GOVERNED_RULES_ENABLED)<br/>KNN ≥ 0.85 over PUBLISHED rules"}

    RT -->|"hit · fully mapped + thresholds approved"| RTX["execute compiled SPARQL → Ontop → Athena<br/>(provenance.tier = governed_rule)"]
    RTX --> R
    RT -->|"hit · missing input / unapproved threshold"| RADV["🤖 Advisory + knowledge_gap<br/>(names what is missing · never guesses a bound)"]
    RADV --> R

    RT -->|"flag off / miss / error"| T2START["Tier 2 — deterministic Strands graph<br/>(tier2_resolve · open MCP session · fetch ontology once)"]

    subgraph TIER2 ["TIER 2  (deterministic Strands graph — Phase 1→5, VKG)"]
        direction TB
        P1["Phase 1 · Topic router<br/>KNN/lexical over ontology class+property IRIs<br/>(_GatewayTopicRouter over fetched ontologyJson)"]
        P2{"Phase 2 · Term disambiguation<br/>term → candidate IRI (exact/inflected/substring)"}
        P3["🤖 Phase 3 · Ontology-slice builder + judge loop<br/>CONSTRUCT (n_hops) → Turtle → centrality-fit<br/>JUDGE_PROMPT sufficiency (slice judge = LLM)"]
        P3B{"Phase 3b · Slice disambiguation<br/>(graph-based)"}
        P4["🤖 Phase 4 · SPARQL generate + validate<br/>VkgQueryGenerator (rdflib parse + 1 repair)"]
        P5G{"Phase 5a · Grounding gate<br/>BGP triples vs slice (domain check)"}
        P5T["Phase 5b · SPARQL→SQL translate<br/>Ontop gateway translate_sparql_to_sql (deterministic)"]
        EXEC["🤖📡 Phase 5c · Athena execute (+1 LLM SQL-repair)<br/>then 🤖 answer renderer (_render_answer)<br/>+ 📡 emit_grounding_span (slice + executed SQL)"]

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

    P1 -.->|no candidates · phase1_empty| DEG
    P2 -.->|ambiguous / low-confidence| CLAR
    P3 -.->|"insufficient · phase3_max_rounds / no-op expand"| DEG
    P3B -.->|ambiguous| CLAR
    P3B -.->|unsupported relationship| DEG
    P4 -.->|sparql_repair_failed| DEG
    P5G -.->|grounding_unresolved · rounds = 2| DEG
    P5T -.->|sparql_translation_failed| DEG
    EXEC -.->|sql_execution_failed · 2 attempts| DEG

    CLAR["📡 clarify terminal<br/>(needs_clarification JSON ·<br/>answer_emitter → emit_answer_span)"]
    DEG["📡 degraded terminal<br/>(error answer ·<br/>answer_emitter → emit_answer_span)"]

    T2START --> P1
    EXEC --> R
    CLAR --> R
    DEG --> R
```

Note the two terminals (`CLAR`, `DEG`) emit their `emit_answer_span` **inside the
graph**, as the terminal node's body — that is the only OTEL context position the
SESSION harvester treats as the conversation's final answer (a post-graph emit
orphans into a separate trace).

## Where the flow lives in code

| Step                                 | Entry point                                                      | Module                                                                                                                                                                                   |
| ------------------------------------ | ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Orchestration                        | `_run_query` → `_run_query_core`                                 | [`main.py`](main.py)                                                                                                                                                                     |
| Clarification resolution             | `load_pending_clarification` + `resolve_clarification_reply`     | [`../shared/clarification.py`](../shared/clarification.py)                                                                                                                               |
| Follow-up contextualization          | `contextualize_question`                                         | [`../shared/followup.py`](../shared/followup.py)                                                                                                                                         |
| Intent router + advisory answer      | `classify_intent` · `build_advisory_answer`                      | [`../shared/advisory.py`](../shared/advisory.py) · [`main.py`](main.py)                                                                                                                  |
| Tier 1 lookup / execute              | KNN governed-metric lookup + Athena execute                      | [`../shared/metric_lookup.py`](../shared/metric_lookup.py) · [`main.py`](main.py)                                                                                                        |
| Governed-rule tier (flag-gated)      | `_rule_tier_answer` → `rule_lookup` · `execute_rule`             | [`main.py`](main.py) · [`../shared/rule_lookup.py`](../shared/rule_lookup.py) · [`../shared/rule_executor.py`](../shared/rule_executor.py)                                               |
| Tier 2 entry + phase deps            | `tier2_resolve` → `_build_phase_deps`                            | [`main.py`](main.py) · [`tier2/workflow.py`](tier2/workflow.py)                                                                                                                          |
| Graph + shared primitives            | `PhaseDeps` · `WorkflowContext` · `run_tier2_graph`              | [`tier2/workflow.py`](tier2/workflow.py) · [`../shared/tier2_graph.py`](../shared/tier2_graph.py)                                                                                        |
| Phase 1 topic router                 | `_GatewayTopicRouter` · `VkgTopicRouter`                         | [`tier2/vkg_topic_router.py`](tier2/vkg_topic_router.py)                                                                                                                                 |
| Phase 2 disambiguation               | `analyze_terms` (shared)                                         | [`../shared/disambiguation_common.py`](../shared/disambiguation_common.py)                                                                                                               |
| Phase 3 slice builder + judge        | `VkgSliceBuilder` · `build_slice_judge`                          | [`tier2/vkg_slice_builder.py`](tier2/vkg_slice_builder.py) · [`tier2/slice_judge.py`](tier2/slice_judge.py)                                                                              |
| Phase 3 CONSTRUCT / gateway          | `NeptuneConstruct` · typed MCP wrappers                          | [`tier2/neptune_construct.py`](tier2/neptune_construct.py) · [`tier2/gateway_client.py`](tier2/gateway_client.py)                                                                        |
| Phase 3b slice disambiguation        | `find_slice_ambiguities`                                         | [`tier2/slice_disambiguation.py`](tier2/slice_disambiguation.py)                                                                                                                         |
| Phase 4 SPARQL generate/validate     | `VkgQueryGenerator.generate` · `validate_sparql`                 | [`tier2/vkg_query_generator.py`](tier2/vkg_query_generator.py) · [`tier2/sparql_validator.py`](tier2/sparql_validator.py)                                                                |
| Phase 5 grounding / translate / exec | `check_grounding` · `_run_execution` · `_repair_sql`             | [`tier2/grounding.py`](tier2/grounding.py) · [`main.py`](main.py)                                                                                                                        |
| Tier 3 fallback (degrade only)       | `_run_tier3_supervisor` → `run_supervisor_loop` · `WorkerBudget` | [`main.py`](main.py) · [`tier3/supervisor.py`](tier3/supervisor.py) · [`tier3/budget.py`](tier3/budget.py)                                                                               |
| SQL firewall (all tiers)             | `bind_from_context` · `enforce_for_caller` · `resolve_profile`   | [`../shared/firewall_context.py`](../shared/firewall_context.py) · [`../shared/sql_firewall.py`](../shared/sql_firewall.py) · [`../shared/grant_profile.py`](../shared/grant_profile.py) |

The deterministic phases (1, 2, 3b, the grounding + Ontop-translate + Athena
halves of 5) are **not** LLM agents — they are plain Python functions wrapped by
the `_FnNode` adapter, reading and mutating a single shared `WorkflowContext`
that node functions and conditional-edge predicates both close over. Four spots
**inside the graph** invoke a model — the **Phase 3 slice judge**, the **Phase 4
SPARQL generator**, a conditional **Phase 5 SQL-repair**, and the **Phase 5
answer renderer** (`_render_answer`, on the success path; it exists so Phase 5
emits a real `chat` span for the eval judges) — plus two **before** the graph:
the **intent router** and **advisory synthesis** (`../shared/advisory.py`).

## Invocation

- **Runtime:** AgentCore (`BedrockAgentCoreApp`), Python 3.12 container.
- **Entrypoint:** `invoke(payload, context=None)` in [`main.py`](main.py).
- **Payload:** `{id, question | message, sessionId, userId, turnId?}` where `id`
  is the `ontologyId`. The dispatcher routes on `turnId`: present →
  `_chat_stream()` (SSE AG-UI events); absent → `_run_query()` (direct JSON).
- **Returns:** on success `{answer, sql_query (the SPARQL lineage), results
(rows), n_quads, reasoning, metadata}`; on clarification `{needs_clarification:
true, options, clarification}`; on degrade a plain error answer (never a 5xx).

## SQL firewall (before any tier runs)

([`../shared/sql_firewall.py`](../shared/sql_firewall.py) ·
[`../shared/firewall_context.py`](../shared/firewall_context.py) ·
[`../shared/grant_profile.py`](../shared/grant_profile.py))

There are exactly two serve-time routes to `start_query_execution`, and both call
`enforce_for_caller` immediately before it:

- `_run_athena_sql` ([`main.py`](main.py)) — the ungated Athena core behind Tier 2's
  Ontop-translated SQL, the LLM repair round, and the Tier 3 tools. The firewall runs
  **before any AWS client is built**, so a denied statement costs nothing.
- `execute_metric` ([`../shared/metric_executor.py`](../shared/metric_executor.py)) —
  the Tier 1 path. It firewalls the **final** statement, after the caller's filters are
  merged in, and parses in the metric's own dialect (parsing a non-Athena statement as
  `athena` would fail, which inside a restrictive profile means denied). Separately and
  first, `check_metric_allowed` applies `allowedMetrics` — a metric is authorized by
  identity, not by the shape of its SQL, so there is no point parsing SQL the caller
  may not run at all.

Build-side Athena helpers (`delta_fingerprint.py`, `athena_bench.py`) are not on a
caller's serve path and are not gated.

Two halves, and the distinction matters:

- **Always on, structural.** SELECT-only at the root, plus a **deep AST scan** for
  data-modifying nodes anywhere in the tree — so `WITH d AS (DELETE … RETURNING *)
SELECT * FROM d` is refused even though it begins with `WITH`. Comments are stripped
  before a pre-parse regex runs, and a dangerous-function denylist applies. Parsing is
  sqlglot's `athena` dialect. None of this depends on the caller having a grant.
- **Per-caller, opt-in.** `tableAllowlist`, `columnDenylist` and `allowedMetrics` come
  from the caller's merged grants. The merge is **deny-wins**: denylists union, and an
  allowlist is the union of only the grants that _declare_ one, so a permissive grant
  cannot erase a restrictive grant's PII denylist. `None` (no grant declares one)
  means unconstrained and is deliberately **not** the same as `[]`, which denies
  everything.

The caller profile is bound once per invocation via a `ContextVar`, in `invoke()`
before any tool or phase runs — `execute_sql_query` reads the binding rather than
taking it as a parameter, because its signature _is_ the model's tool schema and the
model must not be able to supply its own permissions. The namespace is resolved from
the ontology's DynamoDB config row, **never from the payload**. A failure to read the
grants fails the turn: restrictions we cannot read are not the same as no restrictions.

Where the identity comes from is worth being precise about, because the two halves differ.
Groups — which is what resolves a role — are read **only** from the forwarded Bearer JWT's
`cognito:groups` claim; no payload field can add one. The subject is
`_user_id_from_context`, which prefers the JWT `sub` and falls back to `payload['userId']`,
then to `anonymous`. `bind_from_context` deliberately takes that subject as a parameter
rather than decoding it itself, so the precedence is decided in one place per agent and
cannot be silently re-made with different rules further down.

A denial raises `UnsafeSQLError`, which propagates as a turn failure rather than being
retried or degraded around — Phase 5's SQL is deterministic Ontop output, so unlike the
RAG agent's model-written SQL there is nothing to usefully rewrite. (The RAG agent
returns its denial instead: a Strands `@tool` that raises is retried by the model
forever.)

The agent-side firewall does **not** read `ENABLE_AUTHZ`; it is independent of whether
the REST API's policy-decision point is enforcing. The structural half runs
unconditionally — there is no switch that turns SELECT-only off. Only the per-caller half
needs configuration: with `GRANTS_TABLE` unset the resolver logs
`grant_profile_no_table` and returns identity only, which means no restrictions to apply
(unconstrained), **not** deny-all. See the root README's
[Namespaces, Authorization & the SQL Firewall](../../README.md#namespaces-authorization--the-sql-firewall).

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

## Governed-rule tier — between Tier 1 and Tier 2

`_rule_tier_answer`, flag-gated on `GOVERNED_RULES_ENABLED` (unset = off, and the
block returns before making any Neptune, embedding, or KNN call). **Ordering:
metrics first, then rules** — a governed metric is both more specific and cheaper
than a rule, so a question a metric answers must never pay for rule lookup.

Matching mirrors Tier 1: embed the question, KNN over the namespace's PUBLISHED
rules (hydrated from the `<ontologyId>/rules` Neptune graph, not DynamoDB), accept
the top hit only at cosine ≥ **0.85**. Two outcomes on a hit:

- **Evaluable** — every binding resolves to a column and every applied threshold
  is approved: execute the rule's `compiled_sparql` through Ontop → Athena and
  return with `provenance.tier = "governed_rule"`. The answer states the
  epistemic status in words, names the threshold and its approver, and cites the
  source document.
- **Knowledge gap** — any `missing_inputs` or unapproved threshold: **do not
  execute**. Return an advisory answer carrying a `knowledge_gap` that names
  exactly what is missing (`rule_id`, `missing_inputs`, `unapproved_thresholds`).
  Provenance is `advisory` with `sources = ["rule:<id>"]`, so the Monitoring tab
  shows which rule stalled without counting it as an answered rule. The bound is
  never guessed.

The evaluable/gap predicate is `unapproved_threshold_ids` **imported from
`rule_executor`**, not re-derived here: a caller that disagreed with the executor
would either advise forever or be refused and fall to Tier 2 forever, both
silently. Fail-soft like Tier 1 — every error path returns `None` and Tier 2 runs.

> Known gap (shared with Tier 1): neither no-graph tier calls
> `emit_answer_span`, so a rule-answered turn exports no final-answer span and
> the SESSION eval judges score it as missing rather than wrong.

## Tier 2 — Deterministic Strands graph (VKG)

Reached **only when Tier 1 and the governed-rule tier both miss**. `tier2_resolve`
opens an MCP session to the Neptune gateway, **fetches the ontology once**, builds a `PhaseDeps`, and runs
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

## Tier 3 — Supervisor-loop agentic fallback

([`tier3/supervisor.py`](tier3/supervisor.py) · [`tier3/budget.py`](tier3/budget.py))

Reached **only when Tier 2 degrades** (`wf.degraded` set) — never on a
clarification or a success. A bounded **worker → judge → retry** loop reasons
freely over the ontology to recover an answer the structured SPARQL path missed.
It never fabricates: on exhaustion the honest Tier 2 degraded answer is kept.

Three bounds, because the round ceiling alone is not enough:

| Bound                    | Default | Catches                                                   |
| ------------------------ | ------- | --------------------------------------------------------- |
| `DEFAULT_MAX_ROUNDS`     | `3`     | worker rounds (`supervisor.py`)                           |
| `DEFAULT_MAX_TOOL_CALLS` | `40`    | **fast-spin** — hundreds of cheap calls burning the clock |
| `DEFAULT_ROUND_SECONDS`  | `240.0` | **slow-spin** — few calls, each very slow                 |

The two per-round bounds exist because a Strands ReAct loop has no iteration cap
of its own, and a plain `Exception` raised inside a `@tool` is handed back to the
model as a tool _result_ — so the model retries forever. `WorkerBudgetExceeded`
therefore derives from **`BaseException`** so it unwinds the agent call rather
than being swallowed as a tool result.

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

## OTEL spans — eval telemetry

The AgentCore evaluation judges (GoalSuccessRate, FinalAnswerFaithfulness,
SqlGrounded, ToolCallOrdering) are **SESSION-level** graders that read the
conversation from the agent's OpenTelemetry spans, harvested under the
`strands.telemetry.tracer` scope. They cannot see anything the agent did **not**
emit as a span — so every path that produces a user-visible answer must leave one
behind, and a deterministic (no-LLM) path emits **nothing** for free. This agent
therefore mixes two kinds of spans:

- **Auto-instrumented `chat` spans** — emitted by the Strands SDK whenever a model
  is invoked: the Phase 3 slice judge, the Phase 4 SPARQL generator, the
  conditional Phase 5 SQL-repair, the **Phase 5 answer renderer**
  (`_render_answer`), and the off-graph intent router / advisory synthesis.
- **Hand-rolled spans** — for the deterministic paths that would otherwise be
  invisible to the judges. These are **eval-only telemetry**: no tokens, no
  behaviour change, fail-soft (a tracing error never breaks a query).

| Path / terminal               | Span emitted                                       | Helper                                                       | Why it's needed                                                                                                                                                                                                                                      |
| ----------------------------- | -------------------------------------------------- | ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Advisory answer (pre-cascade) | `emit_answer_span` (label `advisory`)              | [`../shared/answer_span.py`](../shared/answer_span.py)       | The user sees the advisory answer, not the intermediate intent-classification span; this makes the judges grade the real answer.                                                                                                                     |
| Phase 5 success               | auto `chat` (renderer) **+** `emit_grounding_span` | [`../shared/grounding_span.py`](../shared/grounding_span.py) | Phase 5 is deterministic (Ontop→Athena, no LLM). The renderer gives FAF a real NL answer span; `emit_grounding_span` carries the **slice + executed SQL** so `SqlGrounded` has the schema to verify against (without it the judge fails closed → 0). |
| Clarify terminal              | `emit_answer_span` (label `clarification`)         | [`../shared/answer_span.py`](../shared/answer_span.py)       | A clarify turn makes no model/tool call, so it has no span at all; the SESSION judges would `ValidationException` ("no spans") on the turn.                                                                                                          |
| Degraded terminal             | `emit_answer_span` (label `final_answer`)          | [`../shared/answer_span.py`](../shared/answer_span.py)       | Same — a degraded turn's last model span is an intermediate (e.g. the Phase 4 SPARQL), which the judge would mistake for the answer.                                                                                                                 |

**Where the terminal spans fire matters.** The `clarify` / `degraded`
`emit_answer_span` calls run **inside the graph**, as the terminal node's body
(`_emit_terminal_answer` → `deps.answer_emitter`), while the graph's `multiagent`
span is still the active (recording) OTEL context. That is the only position the
SESSION harvester treats as the conversation's final answer — a post-graph emit
orphans into a separate trace and the judge never sees it. On the **success**
path there is no synthetic answer span: the Phase 5 renderer already emitted a
real `chat` span, so adding one would be a redundant zero-usage span that could
shadow the real one.

> ⚠️ **The `SqlGrounded` score is the least trustworthy cell** for VKG, in two
> directions. It passes **vacuously (1.0)** on every SQL-free row (advisory,
> bail-out, clarify) because there is no executed SQL to fault; and it can read
> **near-zero** when the `emit_grounding_span` slice fails to harvest, even though
> grounding is fine. Judge VKG by **GoalSuccessRate + direct answer inspection**,
> not the grounding sub-score.

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

- **Claude Sonnet 5** (`global.anthropic.claude-sonnet-5`) — Phase 3 judge,
  Phase 4 SPARQL generator, Phase 5 SQL-repair (see `QUERY_MODEL_ID` /
  `JUDGE_MODEL_ID` in [`query_prompts.py`](query_prompts.py)).
- **Titan Text Embeddings v2** (`amazon.titan-embed-text-v2:0`) — Tier 1
  governed-metric KNN, the governed-rule tier's KNN, and topic-router KNN.

Both appear in [`models.json`](models.json); every model-id literal under this
directory must be listed there (CDK derives `bedrock:InvokeModel` IAM grants from
it; enforced by `tests/unit/test_model_manifests.py`).
