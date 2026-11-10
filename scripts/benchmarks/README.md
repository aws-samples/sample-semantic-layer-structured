# Benchmark Ingestion Runbook

Idempotent pipelines that download public text-to-SQL benchmarks and ingest their
databases into S3 Tables / Athena (Iceberg), so the eval harness can score agents
with **execution-based metrics** (EX / R-VES / Soft-F1 — see
[`agents/shared/exec_metrics.py`](../../agents/shared/exec_metrics.py)) _alongside_
the existing LLM-as-judge evaluators. Each pipeline emits a normalized JSONL
dataset (no embedded result sets) plus a `manifest.json` mapping every `db_id` to
its Athena catalog/database, tables, row counts, and gold-SQL transpile status.

| Pipeline            | Benchmark                       | Source repo                       | Tier                           |
| ------------------- | ------------------------------- | --------------------------------- | ------------------------------ |
| `ingest_cwd.py`     | data.world CWD (ACME_Insurance) | `datadotworld/cwd-benchmark-data` | A — primary (insurance domain) |
| `ingest_bird.py`    | BIRD mini-dev                   | `bird-bench/mini_dev`             | B — breadth                    |
| `ingest_spider2.py` | Spider 2.0-lite (SQLite subset) | `xlang-ai/Spider2`                | C — stretch                    |

See the design doc `docs/plans/2026-07-13-benchmark-expansion-design.md` and the
eval dataset docs [`data/eval/README.md`](../../data/eval/README.md).

### Spider 2.0-snow runs on Snowflake DIRECTLY, not through Athena

`ingest_spider2.partition_ingestable` deliberately **defers** every warehouse-origin
(BigQuery / Snowflake) database rather than ingesting it. For the Snowflake half there
is nothing to ingest — Spider 2.0-snow exists precisely because the data already lives
in Snowflake — so those rows execute over a direct connection via
[`agents/shared/snowflake_bench.py`](../../agents/shared/snowflake_bench.py), the
drop-in sibling of `athena_bench` (same `columns`/`rows`/`elapsed_ms` contract, so
`exec_metrics` scores both identically).

Snowflake is _also_ registered as an Athena federated catalog (`snowflake_<database>`),
which is the right transport for product queries — it is the only way to join Snowflake
to the lake and it needs no agent changes. It is the wrong transport for this benchmark:
Spider 2.0-snow's gold SQL **is** Snowflake SQL, so routing it through Athena means
transpiling Snowflake→Trino (`QUALIFY`, `LATERAL FLATTEN` over `VARIANT` and several
date functions have no clean equivalent) and every transpile failure becomes an unscored
item. That makes the transport a source of measurement error, and turns the metric into
a test of Trino generation rather than Snowflake generation. See the module docstring for
the full rationale, including why one federated catalog cannot span databases.

Credentials are the **same** Secrets Manager secret the Athena connector uses, written
by `npm run sync:snowflake-secret` from `cdk/` (see
[`cdk/README.md`](../../cdk/README.md) §2) — one credential to rotate, not two.

⚠️ **Timing:** `snowflake_bench.run_sql` reports client wall-clock by default, whereas
Athena reports server-side `EngineExecutionTimeInMillis`. Pass `server_stats=True` for
Snowflake's own `EXECUTION_TIME` before comparing R-VES across the two engines; the
result's `server_time` flag records which clock produced `elapsed_ms`.

---

## Prerequisites

- Python 3.12 and the ingestion deps (`boto3`, `sqlglot`, `requests`, `pyarrow`).
  These get pinned in `scripts/benchmarks/requirements.txt` once Task 12 lands;
  until then install them directly, or use the repo's `agents/requirements.txt`
  (pins `boto3`/`sqlglot`/`requests`) plus `pip install pyarrow` for the Parquet
  export.
- AWS credentials for the target account. This project uses:

  ```bash
  export AWS_PROFILE=huthmac+demo AWS_REGION=us-east-1
  ```

- A confirmed S3 Tables (Iceberg) catalog, an Athena workgroup with a writable
  result location, and CREATE privilege on the benchmark namespace. These are
  confirmed by the **Task 0 spike** — see below before the first live run.

---

## Common CLI arguments

Every pipeline takes the same core arguments (verified against each script's
`argparse`):

| Arg            | Required | Default       | Meaning                                                                                                                               |
| -------------- | -------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `--bucket`     | yes      | —             | S3 staging bucket for the Parquet exports.                                                                                            |
| `--catalog`    | yes      | —             | Athena catalog id — the federated S3 Tables catalog, e.g. `s3tablescatalog/<bench-bucket>` (the value confirmed by the Task 0 spike). |
| `--workgroup`  | yes      | —             | Athena workgroup name.                                                                                                                |
| `--stage-db`   | no       | `bench_stage` | Glue database holding the transient external (staging) tables.                                                                        |
| `--output-loc` | no       | `""`          | Athena result `s3://` location; empty means the workgroup-enforced default.                                                           |
| `--dry-run`    | no       | off           | Log the planned per-db/per-table actions without touching AWS or clobbering the canonical JSONL/manifest outputs.                     |

Pipeline-specific arguments:

| Arg              | Pipeline | Default             | Meaning                                                                                                                                                                                      |
| ---------------- | -------- | ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--limit N`      | BIRD     | `0` (all)           | Ingest only the first N databases.                                                                                                                                                           |
| `--db-prefix`    | BIRD     | `bird_`             | Namespace prefix applied to each `db_id` (keeps BIRD tables from colliding with the insurance schema).                                                                                       |
| `--db-prefix`    | CWD      | `cwd_`              | Namespace prefix for the CWD database.                                                                                                                                                       |
| `--db-name`      | CWD      | `acme`              | Database slug appended to `--db-prefix` (default namespace: `cwd_acme`).                                                                                                                     |
| `--gold-dialect` | CWD      | `""` (pass-through) | Source SQL dialect of the CWD gold SQL to transpile to Trino (e.g. `postgres`, `mysql`, `sqlite`). Empty means **no transpile**; set it after inspecting the real gold SQL at the live gate. |

---

## Running a pipeline

Always dry-run first to preview the planned actions, then run for real.

### BIRD mini-dev

```bash
# Dry run — logs the download + per-db plan, writes nothing.
python scripts/benchmarks/ingest_bird.py \
  --bucket <stage-bucket> \
  --catalog s3tablescatalog/<bench-bucket> \
  --workgroup <athena-workgroup> \
  --dry-run

# Real run (optionally --limit 1 to ingest a single DB first for gate G3).
python scripts/benchmarks/ingest_bird.py \
  --bucket <stage-bucket> \
  --catalog s3tablescatalog/<bench-bucket> \
  --workgroup <athena-workgroup> \
  --output-loc s3://<results-bucket>/athena/
```

### CWD (ACME_Insurance)

Like `deploy_datasets.py` does for BIRD, `ingest_cwd.py` creates the `bench_stage`
Glue database and the `cwd_acme` namespace if absent, and applies the Lake Formation
grants (the executing role gets `CREATE_TABLE`; auto-discovered agent and eval roles
get `SELECT`/`DESCRIBE`), so it runs on a fresh account with no manual steps.

```bash
# Dry run.
python scripts/benchmarks/ingest_cwd.py \
  --bucket <stage-bucket> \
  --catalog s3tablescatalog/<bench-bucket> \
  --workgroup <athena-workgroup> \
  --dry-run

# Real run. Set --gold-dialect only after inspecting the real CWD gold SQL.
python scripts/benchmarks/ingest_cwd.py \
  --bucket <stage-bucket> \
  --catalog s3tablescatalog/<bench-bucket> \
  --workgroup <athena-workgroup> \
  --output-loc s3://<results-bucket>/athena/ \
  --gold-dialect postgres
```

### All databases at once (`deploy_datasets.py`)

The driver wraps a pipeline in a **resumable** download-and-ingest loop: it creates
each namespace, ingests it, applies the Lake Formation grants, and records the
`db_id` in a state file so a re-run skips what already landed. It builds no semantic
layer — that is a separate step (§ _Benchmark semantic layer_ below).

```bash
python scripts/benchmarks/deploy_datasets.py \
  --benchmark bird \
  --bucket <stage-bucket> \
  --catalog s3tablescatalog/<bench-bucket> \
  --workgroup <athena-workgroup> \
  [--limit N] [--only db1,db2] [--grant-roles extraRoleA,extraRoleB] [--dry-run]
```

`--grants-only` re-applies the grants to **already-ingested** namespaces and does
nothing else — no download, no staging, no ingest, and `--bucket` is not required:

```bash
python scripts/benchmarks/deploy_datasets.py \
  --benchmark bird \
  --catalog s3tablescatalog/<bench-bucket> \
  --grants-only [--dry-run]
```

Reach for it whenever the grant set needs to reach namespaces the resumable state
file has already marked done — a redeployed agent role (new ARN) or a NEW agent role
in the deployment. Without it the only way to extend grants to a completed namespace
was to re-ingest it. See the REDEPLOY TRAP below for what the missing grant looks
like: a WARN, not a failure.

---

## Dataset versions (operator action required)

Dataset downloads are version-pinned so the row count / schema can't drift under
the pipeline (BIRD mini-dev, for example, shipped a v2 that grew the row count
~500→780).

**BIRD — separate data bundle, NOT the GitHub repo (verified live 2026-07-14).**
The `bird-bench/mini_dev` GitHub repo contains only code + an empty
`llm/mini_dev_data/` whose README points to a ~764 MB data bundle. `ingest_bird.py`
downloads `BIRD_DATA_URL` (`https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip`;
also mirrored on Google Drive). The bundle is **not** content-addressed, so the
pipeline pins it by **sha256**: set `BIRD_DATA_SHA256` after the first download to
lock the version (empty ⇒ the run logs a WARNING that the version is unpinned).
Confirmed bundle layout:
`minidev/MINIDEV/dev_databases/<db_id>/<db_id>.sqlite` + `minidev/MINIDEV/mini_dev_sqlite.json`.

**CWD / Spider 2.0** pin by immutable GitHub **commit SHA** (`CWD_COMMIT_SHA` /
the Spider2 equivalent); each ships a `<PINNED_SHA>` placeholder and **fails
loudly** rather than download `refs/heads/main`.

Record the versions actually used here:

| Benchmark     | Source                                      | Version pin               | Recorded by / date |
| ------------- | ------------------------------------------- | ------------------------- | ------------------ |
| BIRD mini-dev | OSS `minidev.zip` bundle (not the git repo) | `BIRD_DATA_SHA256=<TODO>` | _TODO_             |
| CWD           | `datadotworld/cwd-benchmark-data` (git)     | `<TODO: record SHA>`      | _TODO_             |
| Spider 2.0    | `xlang-ai/Spider2` (git)                    | `<TODO: record SHA>`      | _TODO_             |

> **First-run validation (2026-07-14, live):** the full BIRD ingest CLI was run
> end-to-end for one database (`superhero`, 10 tables) against the `dev` S3 Tables
> bucket — all tables materialized (750 rows in `superhero`, exact match to source
> SQLite), `bird.jsonl`/`manifest.json` emitted, and a gold query scored EX=1 /
> Soft-F1=1.0 through `benchmark_scorer.score_row`. The write-path + scoring chain
> is proven; remaining work is the Task 8b agent-binding gate.

---

## Idempotency contract

Each pipeline is safe to re-run:

- **Downloads are skipped** when `raw/` already exists and is non-empty — the
  pinned archive is fetched once.
- **Benchmark Iceberg tables are re-ingested via drop-then-create.** For each
  table the pipeline runs `build_drop_sql` (`DROP TABLE IF EXISTS`) _then_
  `build_ctas_sql` (CTAS). CTAS has **no** `IF NOT EXISTS` / `OR REPLACE`, so a
  second CTAS onto an existing table fails `AlreadyExists`; **drop-then-create is
  the idempotency contract**, not "create if not exists".
- **Staging external tables are namespaced per database** as
  `<namespace>__<table>` (e.g. `bird_california_schools__schools`). Two BIRD
  databases can each own a same-named table (`students`); a bare name in the
  shared `stage_db` would let the second CTAS silently read the first DB's data.
  Namespacing keeps them distinct.
- **S3 Parquet keys are namespaced and stable** (`benchmarks/<name>/<namespace>/<table>/`)
  so a re-run overwrites the same objects instead of accumulating stale ones.
- **`--dry-run` never writes the canonical outputs.** It does not create/overwrite
  `bird.jsonl` / `cwd.jsonl` / `manifest.json` (those would otherwise be clobbered
  with sentinel `-1` row-count data) — it only logs what would be written.

---

## Transpile-failure policy

Gold SQL is transpiled to Trino (Athena's dialect) so it executes against the
ingested tables. Failures are **logged and tracked, never silently dropped**:

- **BIRD:** every gold SQL is transpiled SQLite→Trino via `sqlglot`. On success
  the row's `gold_sql` is replaced with the transpiled form. On failure the
  original SQLite SQL is **kept**, the failure is logged, and the owning db's
  manifest entry has `transpile_ok` flipped to `False`.
- **CWD:** transpile is opt-in via `--gold-dialect`. Empty (default) passes the
  gold SQL through unchanged. When set, the same log-and-flag policy applies for
  any `<dialect>`→Trino failure.

Rows for databases that were **not** ingested (e.g. under BIRD `--limit`) are
excluded from the emitted JSONL and logged, so the dataset and manifest never
drift out of sync.

---

## Output artifacts

Each pipeline writes under `data/eval/benchmarks/<name>/`:

```
data/eval/benchmarks/<name>/
  raw/            # extracted upstream archive (downloaded once; git-ignored)
  <name>.jsonl    # normalized BenchmarkRow rows, NO embedded result sets
  manifest.json   # db_id -> {database, catalog, tables, row_counts, transpile_ok}
```

---

## Dataset licenses & provenance

| Benchmark            | Source                            | License                           |
| -------------------- | --------------------------------- | --------------------------------- |
| BIRD mini-dev        | `bird-bench/mini_dev`             | CC BY-SA 4.0                      |
| CWD                  | `datadotworld/cwd-benchmark-data` | Apache-2.0                        |
| Spider 2.0 (stretch) | `xlang-ai/Spider2`                | Per-repo (see the repo's LICENSE) |

**Ontology / OWL / R2RML / TTL artifacts are downloaded for REFERENCE ONLY and
are never loaded into Athena.** CWD ships an OWL ontology, R2RML mappings, and TTL
alongside its DDL + CSV; `ingest_cwd.py` logs these reference files (matched by
suffix) but the metadata + ontology agents build the semantic layer separately
(see the Task 8b section) — the pipeline loads only the CSV data.

---

## Write-path (CTAS-into-Iceberg) — spike result ✅ CONFIRMED (2026-07-14)

> Ran live against the `dev` S3 Tables bucket
> (`semantic-layer-dev-analytics-tables`) via `agents.shared.athena_bench.run_sql`.
> The full staging→CTAS→read→DROP round-trip was exercised with the real
> `build_ctas_sql` / `build_drop_sql` output. CTAS-into-S3-Tables **works** with the
> corrections below (commit that added them: `[Fix] CTAS is_external=false +
unquoted DROP for S3 Tables`).

| Question                                                                                                  | Result                                                                                                                                                                                                                                                                                                                                                                                                         |
| --------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Does CTAS into `s3tablescatalog/<bucket>` succeed?                                                        | ✅ Yes — round-trip CTAS→read(`('1','x')`)→DROP confirmed                                                                                                                                                                                                                                                                                                                                                      |
| Is `WITH (table_type='ICEBERG')` required, ignored, or rejected?                                          | ⚠️ **Insufficient alone** — `table_type='ICEBERG'` by itself fails `NOT_SUPPORTED: Only managed table is supported for Iceberg table type, please set 'is_external' property to false`. **Required form: `WITH (table_type='ICEBERG', is_external=false)`** (now emitted by `build_ctas_sql`).                                                                                                                 |
| Working cross-catalog CTAS source form — `build_ctas_sql(source_catalog="AwsDataCatalog")` 3-part source? | ✅ Quoted 3-part `"AwsDataCatalog"."<stage_db>"."<table>"` is accepted by CTAS (quoted identifiers are fine in `CREATE TABLE ... AS SELECT`).                                                                                                                                                                                                                                                                  |
| DROP form                                                                                                 | ⚠️ **Quoted `"db"."table"` FAILS to parse** under the federated S3 Tables catalog context (`mismatched input`). **`DROP TABLE IF EXISTS db.table` (unquoted) works** and `IF EXISTS` is supported — now emitted by `build_drop_sql`. `safe_ident` keeps unquoted names injection-safe.                                                                                                                         |
| Confirmed catalog id (the exact `--catalog` value)                                                        | `s3tablescatalog/semantic-layer-dev-analytics-tables` (AWS-managed S3 Tables integration; does **not** appear in `aws athena list-data-catalogs`).                                                                                                                                                                                                                                                             |
| Workgroup / result location                                                                               | `primary` → `s3://text-to-sql-databucket-2xgqbvkhtnm4/athena_results/` (workgroup-enforced; pass `--output-loc ""`).                                                                                                                                                                                                                                                                                           |
| CREATE privilege on the benchmark namespace                                                               | ⚠️ **Lake Formation gates new namespaces.** A fresh namespace returns `Insufficient Lake Formation permission(s): Required Create Table`. Grant it to the executing role: `aws lakeformation grant-permissions --principal '{"DataLakePrincipalIdentifier":"<role-arn>"}' --resource '{"Database":{"CatalogId":"<acct>:s3tablescatalog/<bucket>","Name":"<namespace>"}}' --permissions CREATE_TABLE DESCRIBE`. |
| Namespace creation                                                                                        | Via `aws s3tables create-namespace --table-bucket-arn <arn> --namespace <name>` (NOT Athena `CREATE SCHEMA`).                                                                                                                                                                                                                                                                                                  |
| Fallback if CTAS into `s3tablescatalog/` had failed                                                       | Not needed — CTAS works. (Plain Glue-DB-on-S3 remains a catalog-agnostic fallback if ever required.)                                                                                                                                                                                                                                                                                                           |

**Confirmed copy-pasteable templates (verified live):**

```sql
-- CTAS (managed Iceberg into S3 Tables); execution-context Catalog = the s3tablescatalog id, Database = target namespace
CREATE TABLE "<ns>"."<table>" WITH (table_type = 'ICEBERG', is_external = false)
AS SELECT * FROM "AwsDataCatalog"."<stage_db>"."<table>";
-- DROP (unquoted; quoted form fails to parse under the federated catalog)
DROP TABLE IF EXISTS <ns>.<table>;
```

**Exit criterion:** ✅ met — confirmed CTAS + DROP templates, catalog id, workgroup,
and the LF-grant prerequisite are all recorded above. Bulk ingest is unblocked
(still requires pinning the dataset SHAs and the Task 8b semantic-layer build).

---

## Benchmark semantic layer (agent binding) — result ✅ CONFIRMED (2026-07-15)

> The make-or-break integration for "prove generalization" — **passed live** for
> `bird_superhero`. No code or CDK changes were needed; the existing metadata-agent
> build path binds a brand-new benchmark DB.

**Why this is required:** the deployed metadata-query agent resolves the target
`database_name` / `catalog_id` from **KB-retrieval metadata**, not from a caller
parameter (`agents/metadata_query_agent/main.py`, lines ~339–362 read
`meta.get('database_name')` / `meta.get('catalog_id')` off the retrieved chunks;
retrieval is hard-filtered by `semantic_layer_id` + `version`, lines ~298–306). So
each benchmark DB needs its OWN layer id, built against the ingested schema, and the
query agent must be invoked with that id (`ontologyId`).

| Item                                                                 | Result                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| -------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| How the metadata agent was run against the ingested benchmark schema | Seeded a DynamoDB config record (`semantic-layer-dev-metadata`, `id=semantic-rag-bird_superhero`, `type=SemanticRAG`, `dataSources`=the 10 manifest tables with `catalogId=s3tablescatalog/semantic-layer-dev-analytics-tables`), then invoked the **existing** metadata runtime (`semantic_layer_dev_metadata-…`, payload `{"id":"semantic-rag-bird_superhero"}`) via Cognito M2M — same entrypoint as the insurance flow, not forked. Build completed: 10/10 tables, 10 doc+sidecar pairs under `metadata/semantic-rag-bird_superhero/v1/…`, auto-triggered KB ingestion (10 new docs indexed, 0 failed). |
| KB strategy chosen                                                   | **Shared KB (`RAEDBATFHV`) with per-chunk db metadata + per-layer retrieval filter.** Each benchmark DB gets its own `semantic_layer_id`; the query agent's `andAll[equals semantic_layer_id, equals semantic_layer_version]` filter scopes retrieval to it. No new KB / vector index per DB — cheapest path, already supported by the code. Scales to the 11–18 BIRD DBs as distinct layer ids.                                                                                                                                                                                                            |
| Verified gate G4 transcript                                          | Query agent invoked with `ontologyId=semantic-rag-bird_superhero`, question "How many superheroes are there?" → **executed SQL `SELECT COUNT(id) FROM bird_superhero.superhero`** (targets the benchmark DB, NOT `normalized`), returned **750** (exact match to source), `provenance.tier=semantic_sql`, `grounded=true`, sources = the 10 `bird_superhero` tables, no error.                                                                                                                                                                                                                              |

**LF prerequisite (confirmed):** the metadata-agent, query-agent, **and the three
benchmark-eval Lambda functions** each need Lake Formation `SELECT`+`DESCRIBE` on
the benchmark namespace (they are distinct principals from the ingesting role).
Granted per-role on `bird_superhero` before the build. **The codeBased eval
Lambdas execute the GOLD SQL on Athena**, so without their LF grant they fail with
`CATALOG_NOT_FOUND` (surfaced as batch status `FAILED` / `GoldExecutionFailed`)
even though their IAM policy already carries the glue/s3tables/athena actions — LF
is the data-access gate.

NOTE: the codeBased evaluator is deployed as **THREE separate Lambda functions**
(`BenchmarkEvalHandlerExecutionAccuracy` / `RewardVES` / `SoftF1`, each with
`EVAL_METRIC` in its env — AgentCore's batch path does NOT pass `evaluatorName`,
so one shared function can't self-identify its metric). **Each of the three
execution roles** needs the LF grant. All three are now auto-discovered by
`deploy_datasets.py` (they share the `BenchmarkEvalHandler` name prefix and a
`lambda.amazonaws.com` trust policy), so no ARN needs to be passed — `--grant-roles`
is for EXTRA principals only, and `--grants-only` re-applies the set to namespaces
that are already ingested (DESCRIBE on the DB + SELECT/DESCRIBE on the table
wildcard).

**⚠️ REDEPLOY TRAP (2026-07-27):** LF grants are keyed by the principal **role ARN**,
and the agentcore CDK role names carry a random suffix
(`…SemanticQueryAgentRole2FA-<rand>`). If a stack redeploy RECREATES a query/metadata
agent role (new ARN), its LF grants **silently vanish** and every subsequent
table-touching query fails with `Insufficient Lake Formation permission(s): Required
Describe on <db>` (a 623/624-failure wipe-out was traced to exactly this on the VKG
`SemanticQueryAgentRole`). After ANY agentcore redeploy that may touch these roles,
re-grant before running evals — `--grants-only` re-applies the full grant set to
every ALREADY-ingested namespace without staging or ingesting anything:

```bash
python scripts/benchmarks/deploy_datasets.py \
  --benchmark bird \
  --catalog s3tablescatalog/semantic-layer-dev-analytics-tables \
  --grants-only            # add --dry-run first to see the principals it resolved
```

It re-discovers the roles from live IAM, so a role that was REPLACED (new ARN) is
picked up automatically and no ARN has to be pasted by hand. `--bucket` is not
required in this mode. Run it once per benchmark (`--benchmark bird`, then `cwd`);
`--only db1,db2` narrows it to specific namespaces.

(An in-place role UPDATE keeps the ARN and the grants survive; only a REPLACE drops them.
Verify with `aws lakeformation list-permissions --principal … --resource '{"Table":{…"TableWildcard":{}}}'`.)

**The grant list above was INCOMPLETE — it omitted the ontology BUILD role
(2026-08-04), later the hybrid semantic-layer BUILD role (2026-08-14), and later
still the REST API Lambda (2026-08-18).** The full principal set that needs a grant
on every benchmark DB is **ten** roles, not five:

| Role name prefix                   | Needs the grant because                                       |
| ---------------------------------- | ------------------------------------------------------------- |
| `OntologyGenerationAgentRo-<rand>` | **Phase 1 `get_single_table_schema` reads the Glue schema**   |
| `MetadataAgentRole0A1C1619-<rand>` | metadata layer build reads schemas                            |
| `SemanticLayerCreationAgen-<rand>` | **hybrid layer build — the VKG phase probes table schemas**   |
| `SemanticQueryAgentRole2FA-<rand>` | VKG query agent executes SQL                                  |
| `SemanticLayerQueryAgentRo-<rand>` | hybrid query agent executes SQL                               |
| `MetadataQueryAgentRoleB4A-<rand>` | SemanticRAG query agent executes SQL                          |
| `BenchmarkEvalHandlerExecu-<rand>` | codeBased evaluator runs gold SQL (EX)                        |
| `BenchmarkEvalHandlerRewar-<rand>` | codeBased evaluator runs gold SQL (R-VES)                     |
| `BenchmarkEvalHandlerSoftF-<rand>` | codeBased evaluator runs gold SQL (Soft-F1)                   |
| `lambda-LambdaExecutionRole-<rand>` | **governed-rules PUBLISH gate smoke-executes the rule's SQL** |

The driver resolves all ten itself (`_AGENT_ROLE_NAME_PARTS` in
`deploy_datasets.py`) rather than trusting `--grant-roles`, precisely because a
hand-maintained list is the wrong shape for a failure this quiet. Note that
`SemanticLayerCreationAgen-<rand>` is matched on **(name substring, trust
principal)**, not name alone: CloudFormation truncates logical ids to 25 characters,
so the hybrid creation RUNTIME role and the six CodeBuild / custom-resource roles of
its container build all collapse to the same physical prefix. Only the runtime role
is assumable by `bedrock-agentcore.amazonaws.com`, so the trust policy is what keeps
LF `SELECT` on benchmark data away from build service roles.

Missing the **hybrid build role** fails in exactly the 2026-08-04 shape: the VKG
phase logs `Insufficient Lake Formation permission(s): Required Describe on <table>`
as a WARN ("composing without schema") and the build carries on.

Missing the **ontology role** fails differently — and far more dangerously — than
missing a query role. A query-role gap produces loud per-query errors. An
ontology-role gap produces a **`completed` build that measures nothing**: every
`glue.get_table` returns AccessDenied, `get_single_table_schema` returns
`{"error": …}` without raising, and the agent then models each table from the LLM's
own memory of the public benchmark schema and reports success. Observed after the
2026-08-04 from-scratch deploy: 10 of 11 BIRD layers reached `completed` while being
pure guesses. `bird_thrombosis_prediction` was the only failure, and only because
all 3 of its tables failed, leaving no fragments to assemble — with one more table
the whole run would have looked green.

Fingerprint of a guessed ontology (both visible in
`s3://<artifacts>/ontologies/<layer_id>/ontology.nq`):

1. a `notes` quad admitting it: _"Schema and sample data could not be introspected
   due to Lake Formation Describe permission restrictions; modeled from the
   canonical BIRD … schema"_, and
2. **every** `rdfs:range` is `xsd:string` **while the real Glue schema has
   non-string columns** — i.e. the ontology flattened type information that
   actually exists upstream.

The qualifier in signal 2 matters and was learned the hard way (2026-08-05). The
first version of the check treated all-string ranges as proof on their own, and it
aborted a _correct_ `bird_toxicology` build: that database is genuinely all-string
in Glue (4 tables, 11 columns, every one `string`), so all-string ranges were the
right answer. Signal 2 is therefore judged against ground truth from
`glue.get_tables` on the source namespace, not against an assumption that real
schemas always contain numeric or date types.

Measured contrast on the same database, guessed vs. genuinely introspected:
`bird_toxicology` guessed = 71 quads **plus the admission**, versus genuinely
introspected = 215 quads, no admission, all-string ranges matching all-string Glue.
On a mixed-type database the separation is much wider —
`bird_california_schools` 2662 quads / `[double, long, string]`.

`notebooks/11_bird_semantic_layer.ipynb` and `13_cwd_semantic_layer.ipynb` assert
both signals per build (`_assert_schema_introspected`, with `_glue_column_types`
supplying the ground truth), so this regression aborts on the FIRST database rather
than after all 11. Do not weaken either check into a warning — and if the Glue
schema itself cannot be read, the guard raises rather than passing, because it
cannot verify anything without ground truth.

**Exit criterion:** ✅ met — `bird_superhero` has a semantic layer and the deployed
agent answers a question with SQL hitting the benchmark tables. The generalization
thesis is proven end-to-end; the eval notebooks can now produce valid numbers for
this layer id.

---

## Related

- Eval dataset & JSONL schema: [`data/eval/README.md`](../../data/eval/README.md)
- Driver notebooks — a VKG semantic-layer build + a benchmark eval per benchmark:
  `notebooks/11_bird_semantic_layer.ipynb` + `notebooks/12_bird_benchmark_eval.ipynb` (BIRD),
  `notebooks/13_cwd_semantic_layer.ipynb` + `notebooks/14_cwd_benchmark_eval.ipynb` (CWD).
  Execution metrics (EX / R-VES / Soft-F1) are scored **server-side** by the codeBased AgentCore
  evaluator Lambda ([`cdk/lib/stacks/backend/benchmark-eval-handler/`](../../cdk/lib/stacks/backend/benchmark-eval-handler/)),
  not client-side in the notebook. (Spider 2.0 remains a stretch benchmark — `ingest_spider2.py`
  exists, no eval notebook yet.)
