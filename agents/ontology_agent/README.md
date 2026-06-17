# Ontology Agent — Creation-Time VKG / Ontology Builder

This document describes the Ontology Agent (VKG mode). It is the **build-time** counterpart of the
[`ontology_query_agent`](../ontology_query_agent/README.md): this agent runs
**when a VKG semantic layer is created or revised**, the query agent runs **when
a user asks a question**.

> **What this agent is for.** Given an ontology configuration (a set of
> Glue / S3-Tables tables plus business context and reference documents), it uses
> an LLM with live-schema tools to generate an **OWL ontology in N-Quads**:
> classes and datatype properties per table, then **foreign-key ObjectProperty**
> triples linking them. It **persists the graph to Neptune** (via the Neptune MCP
> gateway), writes a consolidated `ontology.nq` to S3, and enriches the **Glue
> Data Catalog** with `rdfs:comment` descriptions. That Neptune graph + ontology
> JSON is exactly what the VKG query agent's slice builder and Ontop translation
> later read. In other words: **this agent builds the schema graph the VKG query
> path reasons over.**

## Diagram

```mermaid
flowchart TD
    INV([invoke · payload = id]) --> CFG["Read config from DynamoDB<br/>(semantic-layer-metadata · highest version)"]
    CFG --> RET([return {status: processing, id, task_id} immediately])
    CFG --> BG["Spawn daemon thread<br/>(OTel baggage copied via contextvars · async task tracked)"]

    BG --> REVQ{"config.revisionMode?"}
    REVQ -->|yes| REV["Revision workflow<br/>(download base N-Quads · apply_targeted_edits ·<br/>delete old Neptune graph · persist_revision_from_s3)"]
    REVQ -->|no| P1

    subgraph BUILD ["BACKGROUND THREAD — full build"]
        direction TB
        P1["Phase 1 · Per-table ontology generation<br/>fresh agent/table · SlidingWindow(30)<br/>schema → OWL classes + DatatypeProperties (N-Quads)<br/>append_nquads (batched) · save_intermediate_ontology (FS + S3)"]
        P2["Phase 2 · FK ObjectProperty injection<br/>FK plan built in Python from Phase-1 fk_hints<br/>append_fk_triples · persist_file_to_neptune (MCP gateway)<br/>update_glue_metadata_from_ontology"]
        ASM["Assembly (Python, no LLM)<br/>concat all per-table N-Quads → save_ontology_to_s3<br/>(ontologies/&lt;id&gt;/ontology.nq)"]
        ICE["Iceberg metadata update (non-fatal)<br/>column docs + table descriptions → S3 Iceberg JSON"]
        P1 --> P2 --> ASM --> ICE
    end

    ICE --> DONE["status = completed (metadataPath, completedAt)"]
    REV --> DONE
    DONE --> PUB["EventBridge ontology.published → rebuild KNN topic index"]
    DONE --> EVAL["EventBridge evaluation.requested → ground-truth eval"]
```

## Where the flow lives in code

| Step                               | Entry point                                                           | Module                                                          |
| ---------------------------------- | --------------------------------------------------------------------- | --------------------------------------------------------------- |
| Entrypoint / orchestration         | `@app.entrypoint invoke(payload, context)`                            | [`main.py`](main.py)                                            |
| Active-version resolution          | `_resolve_active_version` (highest version in DDB)                    | [`main.py`](main.py)                                            |
| Phase 1 (classes + datatypes)      | per-table agent loop · `append_nquads` · `save_intermediate_ontology` | [`main.py`](main.py) · [`prompt_builder.py`](prompt_builder.py) |
| Phase 2 (FK ObjectProperties)      | Python FK plan · `append_fk_triples` · `persist_file_to_neptune`      | [`main.py`](main.py)                                            |
| Assembly + S3 persist              | concat fragments · `save_ontology_to_s3`                              | [`main.py`](main.py)                                            |
| Glue enrichment                    | `update_glue_metadata_from_ontology`                                  | [`main.py`](main.py)                                            |
| Prompts + namespace + N-Quads spec | Phase-1/2/revision system prompts · XSD maps · namespace URIs         | [`prompt_builder.py`](prompt_builder.py)                        |
| Token budgeting                    | tiktoken counts; 150k per-request limit                               | [`token_manager.py`](token_manager.py)                          |
| Model manifest (IAM grants)        | `foundation_models`                                                   | [`models.json`](models.json)                                    |
| Container                          | `opentelemetry-instrument python -m ontology_agent.main`              | [`../Dockerfile.ontology`](../Dockerfile.ontology)              |

## Invocation

- **Runtime:** AgentCore (`BedrockAgentCoreApp`), Python 3.12 container.
- **Entrypoint:** `invoke(payload, context)` in [`main.py`](main.py).
- **Payload:** `{"id": "<ontology-id>"}` — the partition key for the DynamoDB
  config; the **active (highest) version** is resolved automatically.
- **Async contract:** the entrypoint reads the config, spawns a **daemon
  thread**, and **returns immediately** with `{"status": "processing", "id":
ontology_id, "task_id": ...}`. All phases run in the background. OTel baggage
  (`session.id`) is propagated into the thread via `contextvars.copy_context()`,
  and the work is registered with `app.add_async_task()` /
  `app.complete_async_task()`.

## Phases

### Phase 1 — Per-table ontology generation

A **fresh** Strands agent (Claude **Opus 4.8**) is created **per table** to keep
context from accumulating across a large layer (thousands of tables would
otherwise overflow the model's input window). For each table the agent:

- fetches the real schema (`get_single_table_schema`) and samples rows
  (`sample_table_data`) — the latter surfaces **FK hints** (`col → target_table`);
- retrieves ontology patterns from the KB (`retrieve_ontology_patterns`) and
  searches any uploaded reference docs;
- generates OWL **class** URIs, **DatatypeProperty** triples, and namespace
  triples in **N-Quads**;
- calls **`append_nquads`** incrementally (batched) so a wide table doesn't force
  the model to emit 100–270 KB of N-Quads in one shot;
- calls **`save_intermediate_ontology`** to persist the per-table fragment to the
  local filesystem **and** S3, and `update_progress` in DynamoDB.

A `SlidingWindowConversationManager(window_size=30)` evicts old messages to keep
input tokens stable. `MaxTokensReachedException` (or any per-table error) is
caught, logged, and **skipped** — the build continues with the next table.

### Phase 2 — FK ObjectProperty injection + Neptune persist

The **FK plan is built in Python** (not by the LLM): Phase 1 fragments are read
off the filesystem, their `fk_hints` parsed, and a FK dependency map keyed by
table is assembled. Then, **per table** (fresh agent, `window_size=10`):

- the pre-rendered `owl:ObjectProperty` FK triples are appended via
  **`append_fk_triples`** (the agent copies pre-rendered lines — it does not
  invent relationships);
- **`persist_file_to_neptune`** uploads the complete per-table N-Quads to the
  Neptune graph through the Neptune **MCP gateway** (`NEPTUNE_GATEWAY_URL`, IAM
  SigV4) — reading the file in Python rather than as LLM output bypasses the
  token limit;
- **`update_glue_metadata_from_ontology`** writes the ontology's `rdfs:comment`
  values to Glue column descriptions.

### Assembly (Python, no LLM)

After Phase 2, all per-table fragments are concatenated into one consolidated
N-Quads file and written to `s3://{ARTIFACTS_BUCKET}/ontologies/{id}/ontology.nq`
via `save_ontology_to_s3`; `metadataPath` is recorded in DynamoDB. This runs in
plain Python to avoid loading every fragment into a single model context.

### Iceberg metadata update (non-fatal)

Column doc-strings and table descriptions are written to the S3 Iceberg metadata
JSON files. A failure here is logged but does **not** block completion.

### Completion

Status flips to `completed`, then two EventBridge events fire:
`ontology.published` (triggers the topic-router **KNN index rebuild** the query
agent's Phase 1 depends on) and `evaluation.requested` (ground-truth eval).

## Revision mode

When `config.revisionMode` is set, Phases 1/2/Assembly are **skipped** entirely.
The revision workflow downloads the existing N-Quads from S3 as base context,
uploads the `revisionInstructions` as markdown, **deletes the old Neptune
graph**, and runs a revision agent (`apply_targeted_edits`,
`persist_revision_from_s3`) before updating DynamoDB.

## Inputs & outputs

**Consumes:** DynamoDB config (`{id, version}`: `name`, `namespace`,
`dataSources[]`, `uploadedDocuments[]`, `useCasesDescription`,
`dataSourcesDescription`, revision fields), Glue schema, Athena sample rows,
Bedrock KB patterns, S3 reference docs.

**Produces:**

- **Neptune graph** — the full ontology (Phase 2 `persist_file_to_neptune`).
- **S3** — consolidated `ontologies/{id}/ontology.nq`, per-table Phase-1
  fragments, and revision-mode context files.
- **DynamoDB** — status, `metadataPath`, progress, timestamps.
- **Glue Data Catalog** — `rdfs:comment` column descriptions.
- **S3 Iceberg metadata** — column docs + table descriptions.
- **EventBridge** — `ontology.published`, `evaluation.requested`.

## Notable constraints & gotchas

- **Per-table agent isolation** is deliberate: a fresh agent per table prevents
  context bleed; per-table state lives on the filesystem, so a clean context is
  safe.
- **Token-overflow mitigation** is layered: sliding-window conversation managers
  (30 in Phase 1, 10 in Phase 2), **batched** `append_nquads`, and
  `persist_file_to_neptune` reading N-Quads **in Python** so the large graph
  never has to pass through the model.
- **Neptune is reached over the MCP gateway** (`NEPTUNE_GATEWAY_URL`, IAM
  SigV4), not a direct connection.
- **Namespace URI** is `config.namespace` when set, else auto-derived as
  `http://<slug>/ontology/<id>` from the sanitized ontology name.
- **`ontology.published` must fire** for the query agent's topic-router KNN
  index to include the new layer; without it, Phase 1 retrieval can't find the
  new classes.
- **R2RML / OBDA mapping note.** The early VKG design
  (`2026-05-16-ontop-vkg-design.md`) envisioned emitting explicit R2RML
  alongside N-Quads. The deployed agent instead encodes table/column traceability
  as predicates (`mapsToTable` / `mapsToColumn`) in the N-Quads, which the Ontop
  translate Lambda consumes as its OBDA mapping at query time. If you go looking
  for generated `.obda` / `.r2rml` files, there aren't any.

## Model

- **Claude Opus 4.8** (`global.anthropic.claude-opus-4-8`) — see
  [`models.json`](models.json) and `prompt_builder.MODEL_ID`. Every model-id
  literal under this directory must appear in `models.json` (CDK derives
  `bedrock:InvokeModel` IAM grants from it; enforced by
  `tests/unit/test_model_manifests.py`).
