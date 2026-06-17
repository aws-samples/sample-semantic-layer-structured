# Metadata Agent — Creation-Time Documentation Pipeline

This document describes the the Metadata Agent (Semantic-RAG mode). It is the **build-time** counterpart of the
[`metadata_query_agent`](../metadata_query_agent/README.md): this agent runs **when a semantic layer is created or revised**, the query agent runs **when a user asks a question**.

> **What this agent is for.** Given a semantic-layer configuration (a set of
> Glue / S3-Tables tables plus business context and reference documents), it
> uses an LLM with live-schema tools to write **business descriptions** for every
> table and column, persists them back to the **Glue Data Catalog**, and emits a
> **markdown doc per table** to S3 that is ingested into the Bedrock **Knowledge
> Base**. That KB is exactly what the query agent's Phase 1 topic router later
> retrieves over. In other words: **this agent populates the corpus the
> Semantic-RAG query path reads.**

## Diagram

```mermaid
flowchart TD
    INV([invoke · payload = id]) --> CFG["Read config from DynamoDB<br/>(semantic-layer-metadata · highest version)"]
    CFG -->|no tables| ERR([return error · no dataSources])
    CFG -->|tables exist| STATUS["Mark status = processing<br/>(buildStartedAt)"]
    STATUS --> RET([return {status: processing, jobId, tableCount}<br/>immediately])
    STATUS --> BG["Spawn daemon thread<br/>(OTel baggage copied via contextvars)"]

    subgraph BG_THREAD ["BACKGROUND THREAD"]
        direction TB
        INVENT["Build layer table inventory<br/>(_layer_tables_var — reference-table validation)"]
        LOOP{"For each table entry<br/>{database, table, catalogId, dataSource}"}
        AGENT["Fresh Strands agent (Claude Opus 4.8)<br/>system prompt = SYSTEM_PROMPT<br/>(or ANNOTATION_SYSTEM_PROMPT in revision mode)"]
        T1["① get_single_table_schema → Glue/Athena"]
        T2["② sample_table_data → 10 live rows"]
        T3["③ retrieve_ontology_patterns → Bedrock KB"]
        T4["④ download/search reference docs (S3)"]
        T5["⑤ compose table + per-column descriptions"]
        VALID["doc_validator: drop hallucinated columns/joins<br/>+ out-of-layer cross-references"]
        T6["⑥ update_glue_table_metadata → Glue catalog"]
        T7["⑦ save_metadata_document_to_s3 → .md + .md.metadata.json"]
        T8["⑧ update_progress → DynamoDB"]

        INVENT --> LOOP
        LOOP --> AGENT --> T1 --> T2 --> T3 --> T4 --> T5 --> VALID --> T6 --> T7 --> T8
        T8 --> LOOP
    end

    LOOP -->|all done| DONE["Mark status = completed (completedAt)"]
    DONE --> KB["_trigger_kb_ingestion → StartIngestionJob (fire-and-forget)"]
    DONE --> EVAL["_trigger_eval → EventBridge evaluation.requested"]
```

## Where the flow lives in code

| Step                        | Entry point                                                       | Module                                             |
| --------------------------- | ----------------------------------------------------------------- | -------------------------------------------------- |
| Entrypoint / orchestration  | `@app.entrypoint invoke(payload, context)`                        | [`main.py`](main.py)                               |
| Config + status (DynamoDB)  | config read, `processing` / `completed` / `failed`                | [`main.py`](main.py)                               |
| System prompts + MODEL_ID   | `SYSTEM_PROMPT`, `ANNOTATION_SYSTEM_PROMPT`, `build_table_prompt` | [`prompt_builder.py`](prompt_builder.py)           |
| Hallucination defense       | column/join schema validation; out-of-layer ref drop              | [`doc_validator.py`](doc_validator.py)             |
| Token budgeting             | tiktoken counts; per-request + per-batch limits                   | [`token_manager.py`](token_manager.py)             |
| KB ingestion + eval trigger | `_trigger_kb_ingestion`, `_trigger_eval`                          | [`main.py`](main.py)                               |
| Model manifest (IAM grants) | `foundation_models`                                               | [`models.json`](models.json)                       |
| Container                   | `opentelemetry-instrument python -m metadata_agent.main`          | [`../Dockerfile.metadata`](../Dockerfile.metadata) |

## Invocation

- **Runtime:** AgentCore (`BedrockAgentCoreApp`), Python 3.12 container, port 8080.
- **Entrypoint:** `invoke(payload, context)` in [`main.py`](main.py).
- **Payload:** `{"id": "<semantic-layer-id>"}` — the partition key for the
  DynamoDB metadata configuration. The **active (highest) version** record is
  resolved automatically.
- **Async contract:** the entrypoint validates the config, flips status to
  `processing`, spawns a **daemon thread**, and **returns immediately** with
  `{"status": "processing", "jobId": id, "tableCount": N}`. All table work runs
  in the background; progress is polled via DynamoDB. OpenTelemetry baggage
  (`session.id`) is copied into the thread with `contextvars.copy_context()` so
  the background spans are tagged and evaluations can find them.

## The per-table workflow (model-facing)

For **each** table in the config, a **fresh** Strands agent is constructed
(Claude **Opus 4.8**, `max_tokens≈16k`, 900 s read timeout; `temperature` is
omitted — Opus 4.8 deprecated it and rejects any value)
and driven by `SYSTEM_PROMPT`. Per-table isolation keeps context from
accumulating across a large layer. The agent calls these tools in order:

1. **`get_single_table_schema(db, table, catalogId)`** — fetch the **real**
   column names, types, and constraints from Glue/Athena. Descriptions are
   composed against ground truth, not guessed.
2. **`sample_table_data(db, table, catalogId)`** — inspect ~10 live rows
   (max 50) via Athena to detect value patterns, enums, and key shapes.
3. **`retrieve_ontology_patterns(schema_description)`** — RAG over the Bedrock KB
   for domain terminology, source paths, and FK patterns.
4. **`download_document_from_s3` / `search_document` / `read_document_lines`** —
   extract context from any user-uploaded reference docs (data dictionaries,
   glossaries) listed in the config.
5. **Compose** a table overview + per-column descriptions, business concepts,
   reference (join) tables, and example questions — as markdown.
6. **`update_glue_table_metadata(db, table, description, column_descriptions_json, catalogId)`**
   — write descriptions back to the Glue catalog (each capped at Glue's 255-char
   column-comment limit).
7. **`save_metadata_document_to_s3(...)`** — write the enriched markdown to
   `s3://{ARTIFACTS_BUCKET}/metadata/{layer_id}/{version}/{catalogId}/{db}/{table}.md`
   plus a `.md.metadata.json` **sidecar** carrying `{layer_id, version,
catalogId, database, table}` so the KB can filter at retrieval time.
8. **`update_progress(...)`** — bump `tablesProcessed` / `currentTable` /
   `progressPercent` in DynamoDB.

## Completion

Once the per-table loop finishes, status flips to `completed`
(`completedAt`), then **two** fire-and-forget triggers run — both non-fatal:

- **KB ingestion** — `_trigger_kb_ingestion` calls Bedrock
  **`StartIngestionJob`** directly (not via EventBridge) over the S3 metadata
  docs, refreshing the Knowledge Base the query agent's Phase 1 topic router
  retrieves over. This is the Semantic-RAG counterpart of the ontology agent's
  `ontology.published` KNN-index rebuild — but there is no KNN index here, so a
  KB ingestion job is started instead of an event being emitted.
- **`evaluation.requested`** — `_trigger_eval` emits a single **EventBridge**
  event (`agents/shared/eval_trigger.emit_evaluation_requested`,
  `layer_type="SemanticRAG"`) to kick a ground-truth eval of the freshly-built
  layer version.

Both check their env vars (`SEMANTIC_RAG_KB_ID`,
`SEMANTIC_RAG_DATA_SOURCE_ID` for ingestion) and skip silently if unset; a
failure in either is logged but never fails the build.

## Hallucination defense — `doc_validator.py`

The LLM composes the `## Columns` and `## Reference Tables` sections freely, so
the agent **re-fetches the real schema** and drops anything that doesn't exist:

- **Phantom columns/joins** named in the markdown but absent from the live schema
  are removed (case-insensitive — Glue returns lower-case).
- **Out-of-layer cross-references** are dropped: a join to a table **not in the
  layer inventory** is removed so the downstream query agent never goes hunting
  for an unbuilt table (e.g. `participant` / `payout`). This is enforced both in
  the prompt (reference-table redirect rules) and as a hard post-filter.

## Inputs & outputs

**Consumes:**

- **DynamoDB config** (`{id, version}`): `dataSources[]`
  (`{databaseName, tableName, catalogId, dataSource}`), `useCasesDescription`,
  `dataSourcesDescription`, `uploadedDocuments[]` (S3 paths), and the
  revision-mode fields.
- **Live data:** Glue catalog schema + Athena sample rows.
- **Bedrock KB:** ontology / domain patterns.
- **S3:** user-uploaded reference documents.

**Produces:**

- **Glue Data Catalog:** table description + JSON map of column descriptions.
- **S3 docs:** `.md` table doc + `.md.metadata.json` sidecar per table, scoped by
  layer id / version.
- **DynamoDB:** status + progress (`buildStartedAt`, `completedAt`/`failedAt`).
- **Bedrock KB ingestion:** `StartIngestionJob` (fire-and-forget) over the S3
  metadata docs.
- **EventBridge:** `evaluation.requested` to kick a ground-truth eval of the
  freshly-built layer.

## Revision mode

When `config.revisionMode` is set, the agent runs the **annotation** path:
`ANNOTATION_SYSTEM_PROMPT` plus the config's `revisionInstructions` drive
targeted refinement of existing docs (same per-table loop), and a **versioned
history** record is written on completion. This mirrors the
[`ontology_agent`](../ontology_agent/README.md) revision pattern.

## Notable constraints & gotchas

- **`catalogId` is passed verbatim** to every Glue/Athena tool call —
  `s3tablescatalog/<bucket>` for S3 Tables (Iceberg) vs `AWSDataCatalog` for
  standard Glue. Dropping it breaks query routing in Athena.
- **Reference tables must exist in the layer.** Table descriptions may only name
  tables present in the `TABLES IN THIS SEMANTIC LAYER` prompt section; an
  empty/bridge/audit-only table redirects the reader to the real data table by
  name rather than inventing ACORD concepts.
- **Token limits** (`token_manager.py`): `MAX_TOKENS_PER_REQUEST = 150_000`,
  `MAX_TABLES_PER_BATCH = 3`. Prompt + schema + samples must fit.
- **Background context propagation** is mandatory: without
  `contextvars.copy_context()`, background spans are untagged and evals find no
  spans.
- **Fire-and-forget triggers** (KB ingestion, eval) are **non-fatal** — they
  check their env vars (`SEMANTIC_RAG_KB_ID`, `SEMANTIC_RAG_DATA_SOURCE_ID`) and
  skip silently if unset; a failure is logged but never fails the build.
- **`python -m metadata_agent.main`** (not a direct file run) avoids a double
  module-execution warning from `BedrockAgentCoreApp` startup.

## Model

- **Claude Opus 4.8** (`global.anthropic.claude-opus-4-8`) — see
  [`models.json`](models.json) and `prompt_builder.MODEL_ID`. Every model-id
  literal under this directory must appear in `models.json` (CDK derives
  `bedrock:InvokeModel` IAM grants from it; enforced by
  `tests/unit/test_model_manifests.py`).
