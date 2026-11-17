# Semantic Layer Tests

This directory contains all tests for the semantic layer project, organized into unit tests and integration tests.

## Directory Structure

```
tests/
├── README.md                                       # This file
├── unit/
│   ├── conftest.py                                # pytest stubs for heavyweight SDK deps
│   ├── test_ontology_agent*.py                    # VKG ontology generation agent
│   ├── test_ontology_query_*.py / test_query_agent*.py  # VKG query agent + Tier 2 cascade
│   ├── test_metadata_agent*.py                    # Semantic-RAG metadata enrichment agent
│   ├── test_metadata_query_*.py                   # Semantic-RAG query agent + Tier 2 cascade
│   ├── test_rag_*.py / test_vkg_*.py / test_tier2_*.py  # shared Tier 2 graph phases (router, slice, grounding, validators)
│   ├── test_tier3_*.py                            # Tier 3 supervisor loop + per-round WorkerBudget
│   ├── test_hybrid_*.py                           # Hybrid agents (incl. test_hybrid_isolation.py — the zero-import guard)
│   ├── test_nquads_literal_escapes.py / test_neptune_tools_*.py  # N-Quads literal grammar (all 4 parsers) + graph-IRI resolution
│   ├── test_key_probe.py / test_key_stamp.py / test_ontology_key_stamp_wiring.py  # Athena-MEASURED key uniqueness, end to end
│   ├── test_build_clears_previous_results.py      # a starting build clears the prior run's error + progress
│   ├── test_snowflake_*.py / test_bootstrap_connector.py / test_glue_service_connector_catalogs.py  # Athena Snowflake connector
│   ├── test_metric_*.py / test_metrics_router.py  # Tier 1 governed-metric lookup + CRUD
│   ├── test_rule_*.py / test_ruleminer.py / test_rules_router.py  # governed rules: models, RDF graph,
│   │                                               #   4-level validators, compiler, 5 test cases, publish gate,
│   │                                               #   query-time lookup/executor, mining, import, REST router
│   ├── test_rule_read_hardening.py                 # strict reads name the rule + field; one corrupt rule
│   │                                               #   does not 500 the list; RuleReadError is NOT a ValueError
│   ├── test_query_agent_rule_tier.py / test_advisory_knowledge_gap.py  # the rule tier inside the cascade
│   │                                               #   (metrics first) + the never-guess knowledge-gap answer
│   ├── test_knn_hydration_rules.py / test_neptune_service_rules.py / test_neptune_resolve_graph_uri_excludes_rules.py
│   │                                               #   rules KNN hydration + the /rules graph exclusion
│   ├── test_mcp_*.py                              # mcp-tools / mcp-proxy Lambdas + user-agent
│   ├── test_shared_*.py                           # shared building blocks (chat sessions, embedding, guardrails, knn)
│   ├── test_streaming_runner.py / test_query_*_chat_stream.py  # AG-UI streaming chat
│   ├── test_obo_middleware.py / test_identity_service.py  # gated OBO identity exchange
│   ├── test_authz_*.py / test_action_map.py        # Cedar policies, the ASGI PDP, the bootstrap
│   │                                               #   custom resource, and the route-mapping guard
│   │                                               #   (an unmapped route fails the build)
│   ├── test_namespace_service.py / test_namespaces_router.py / test_ontology_namespace_binding.py
│   │                                               #   namespace creation saga, lifecycle, REST surface
│   ├── test_sql_firewall.py / test_grant_profile.py / test_firewall_wiring.py  # SELECT-only enforcement
│   │                                               #   (deep AST scan), deny-wins grant merge, runtime binding
│   ├── test_eval_judges.py                         # custom SESSION LLM-as-Judge factory (GoalSuccess / FAF / SqlGrounded)
│   ├── test_exec_metrics.py / test_benchmark_*.py  # execution metrics (EX/R-VES/Soft-F1) + benchmark adapter/scorer + codeBased evaluator handler
│   ├── test_athena_bench.py / test_ingest_*.py / test_deploy_datasets.py  # benchmark ingestion CLIs (BIRD/CWD/Spider2), deploy driver + Athena run-and-fetch
│   └── …                                          # REST API services, memory hooks, eval, NER, etc.
│
└── integration/
    ├── conftest.py
    ├── test_ontology_agent_integration.py         # Real Athena + Neptune/S3 (6 tests)
    ├── test_query_agent_integration.py            # Real Athena + mock ontology (1 test)
    ├── test_tier1_e2e.py                          # Tier 1 governed-metric path (3 tests)
    ├── test_tier2_rag_e2e.py                      # Tier 2 Semantic-RAG path (1 test)
    ├── test_tier2_vkg_e2e.py                      # Tier 2 VKG path (1 test)
    ├── test_hybrid_end_to_end.py                  # Hybrid build + query path (3 tests)
    └── test_governed_rules_e2e.py                 # governed rules: Ontop smoke + graph round-trip
                                                   #   (5 tests, env-gated on GOVERNED_RULES_E2E_*)
```

**Test Counts** (Python, this directory):

- Unit tests: 2710 tests across 234 files (`tests/unit/`) — including 524 authorization /
  namespace / SQL-firewall (10 files), 317 governed-rules (26 files), 288 Hybrid
  (`test_hybrid_*.py`) and 50 Tier 3 tests
- Integration tests: 20 tests across 7 files (`tests/integration/`)

> The frontend (`frontend/src/**/__tests__`, Jest) and CDK (`cdk/**/*.test.ts`, Jest) suites
> live alongside their own code, not under `tests/`.

---

## Unit Tests

Unit tests validate basic functionality with mock data. They do NOT require AWS infrastructure.

**Location:** `tests/unit/`

### conftest.py — SDK Stubs

`tests/unit/conftest.py` stubs heavyweight runtime dependencies so all unit tests can import agent code without installing the full SDK:

- `bedrock_agentcore` / `bedrock_agentcore.runtime`
- `strands`, `strands.agent`, `strands.models`, `strands.tools.mcp`, `strands.types.exceptions`
- `mcp_proxy_for_aws`
- `opentelemetry` (hierarchy)

This conftest is loaded automatically by pytest for all tests under `tests/unit/`.

### Running Unit Tests

> **Setup:** install runtime + dev deps once —
> `pip install -r agents/requirements.txt -r requirements-dev.txt`
> (`requirements-dev.txt` provides `pytest` + `pytest-cov`; `agents/requirements.txt`
> provides the runtime imports — boto3, pydantic, sqlglot, networkx, … — that
> `conftest.py` does _not_ stub). The repo root is on `sys.path` via
> `[tool.pytest.ini_options] pythonpath = ["."]` in `pyproject.toml`, so the bare
> `pytest` command resolves `import agents...` without needing `python -m pytest`.

**All unit tests via pytest (recommended):**

```bash
cd <repo-root>
pytest tests/unit/ -v
```

**With coverage (matches the CI gate):**

```bash
# A few tests shell out to `cdk synth`, which needs the gitignored ontology-docs
# asset dir to exist; an empty placeholder is enough for the synth to resolve.
mkdir -p data/ontology-docs
pytest tests/unit/ --cov --cov-report=term-missing --cov-fail-under=66
```

Current measured coverage is **71%** (2708 passed / 2 skipped; floor enforced at 66
and ratcheting toward 80 — see `docs/plans/2026-06-19-test-coverage-improvements.md`).

**Individual test files via pytest:**

```bash
pytest tests/unit/test_ontology_agent.py -v
pytest tests/unit/test_ontology_athena_tools.py -v
pytest tests/unit/test_ontology_revision_mode.py -v
pytest tests/unit/test_query_agent.py -v
pytest tests/unit/test_metadata_agent.py -v
pytest tests/unit/test_metadata_query_agent.py -v
```

**Direct Python execution (also supported for run_all_tests() style files):**

```bash
python tests/unit/test_ontology_agent.py
python tests/unit/test_query_agent.py
python tests/unit/test_metadata_agent.py
python tests/unit/test_metadata_query_agent.py
```

### Coverage by Agent

#### Ontology Agent

**`test_ontology_agent.py`** — 9 tests

- ✅ Module imports (all Phase 1 + Phase 2 tools)
- ✅ Token manager functionality
- ✅ Tool definitions and callability (Phase 1 + Phase 2)
- ✅ `update_progress` response schema
- ✅ N-QUAD parsing regex logic
- ✅ Phase 1 + Phase 2 agent creation
- ✅ System prompt structure
- ✅ `invoke` entrypoint signature
- ✅ Document processing tool signatures

**`test_ontology_athena_tools.py`** — 33 tests

- ✅ `get_single_table_schema` — S3 Tables and Glue catalog routing via Athena
- ✅ Query context, query string, column parsing, header row filtering
- ✅ `sample_table_data` — S3 Tables and Glue catalog routing
- ✅ Sample size capping at 50, result parsing
- ✅ Exact-match bug fix for `read_local_nquads_file` / `update_nquads_in_file` (`coverage` vs `coverageproduct`)
- ✅ `append_fk_triples` — correct file targeting, content preservation
- ✅ `persist_file_to_neptune` — Lambda invocation, error paths
- ✅ Error paths for all tools (Athena failure, missing env vars, unknown table)

**`test_ontology_revision_mode.py`** — 5 tests

- ✅ `save_revision_to_s3` writes versioned `.nq` key (not `.ttl`)
- ✅ `persist_nquads_to_neptune` calls AgentCore Gateway MCP
- ✅ `build_revision_prompt` contains S3 paths and N-Quads reference
- ✅ `_run_revision_mode` uploads context files and invokes revision agent
- ✅ `invoke` routes to revision mode when `revisionMode=True`

**`test_nquads_literal_escapes.py`** — 31 tests

The N-Quads literal grammar is re-implemented in four places (producer, pre-flight,
gateway, frontend viewer), none importing the others — the Hybrid isolation guard
forbids it. So these tests pin the copies to **one another** rather than testing any
single one alone: a grammar that is narrower on one hop silently drops the line and
renders a grey em-dash, never an error.

- ✅ Gateway and pre-flight both accept every legal object form — escaped quote, backslash, `\n`, `\t`, typed literal, `@en` / `@en-GB` — parametrized over one shared corpus
- ✅ Both reject the same malformed lines, and the pre-flight regex is asserted to be _literally_ the gateway's
- ✅ The greedy trap: `"a \" b"` parses as ONE literal instead of ending at the escape
- ✅ A real harvested description with embedded quotes survives the round trip (this one broke a live rebuild at 95%)
- ✅ A multi-line description stays one physical line — a raw newline would split the quad
- ✅ `tool_persist_to_neptune` refuses the **whole** payload when any line is malformed (no silent partial load), and reports an empty payload rather than writing nothing quietly
- ✅ `OntologyEditor.jsx`'s parser is grepped for the same escape-aware literal alternative

#### Ontology Query Agent

**`test_query_agent.py`** — 6 tests

The deployed VKG agent is a deterministic Tier 2 Strands graph (not a ReAct tool
loop); the legacy single-shot agent and its bespoke `disambiguate_query_terms` /
`execute_sql_query` / `map_sql_results_to_rdf` @tools + the `QueryAnswer` model
have been removed. Tests cover the surviving graph-only surface (the deterministic
phases have their own dedicated test files):

- ✅ Module imports and token manager (`tier2_resolve`, `_run_athena_sql`, `invoke`)
- ✅ Legacy ReAct surface removed (factory, bespoke @tools, `SYSTEM_PROMPT`, `QueryAnswer`, `EXECUTION_PROMPT`)
- ✅ Model-id constants present and full Bedrock identifiers (`QUERY_MODEL_ID`, `JUDGE_MODEL_ID`)
- ✅ State management (`_agent_state` session marker)
- ✅ `invoke` entrypoint signature
- ✅ `_run_athena_sql` (Phase-5 deterministic Athena core) signature

#### Metadata Agent

**`test_metadata_agent.py`** — 11 tests

- ✅ Module imports (all 7 tools)
- ✅ Token manager
- ✅ Tool definitions and signatures (`get_database_tables`, `get_table_schema`, `sample_table_data`, `update_glue_table_metadata`, `update_glue_database_description`, `save_metadata_document_to_s3`, `update_progress`)
- ✅ `invoke` entrypoint signature
- ✅ `update_progress` response schema (mocked DynamoDB)
- ✅ Agent creation
- ✅ Per-table catalog routing (`_get_catalog_for_table`, S3 Tables vs Glue)

#### Metadata Query Agent

**`test_metadata_query_agent.py`** — 6 tests

The deployed agent is a deterministic Tier 2 Strands graph (not a ReAct tool
loop); the legacy single-shot agent and its bespoke `retrieve_kb_context` /
`disambiguate_query_terms` @tools + the `SYSTEM_PROMPT` have been removed. Tests
cover the surviving graph-only surface (the deterministic phases have their own
dedicated `test_tier2_*` / `test_rag_*` files):

- ✅ Module imports (`execute_sql_query`, `tier2_resolve`, `invoke`, helpers)
- ✅ Legacy ReAct surface removed (factory, bespoke @tools, `SYSTEM_PROMPT`)
- ✅ Graph-phase prompts present and reference their contracts (`EXECUTION_PROMPT` → execute_sql_query, `JUDGE_PROMPT` → SliceSufficiency)
- ✅ State management (per-session state dict, reset behavior)
- ✅ `execute_sql_query` tool signature (the sole Phase-5 model tool)
- ✅ `invoke` entrypoint signature

#### Shared Eval Judges

**`test_eval_judges.py`** — 7 tests

- ✅ Factory returns `[GoalSuccess, FinalAnswerFaithfulness, SqlGrounded]` ids in canonical order
- ✅ Every judge is SESSION-level, binary-scaled, on `JUDGE_MODEL_ID`
- ✅ RAG and VKG families register distinct prompt text (not silently the same)
- ✅ Registered instructions match the exported prompt constants (no transform)
- ✅ No SESSION-level judge references TRACE-only placeholders (e.g. `{expected_response}`)
- ✅ Unknown family raises `ValueError` before creating any evaluators
- ✅ Blank `name_suffix` auto-generates unique names across two calls

#### REST API Services

- `test_metadata_api.py` — 4 tests: API endpoint schemas
- `test_metadata_service.py` — 7 tests: MetadataService enrichment & query
- `test_agentcore_service.py` — 5 tests: AgentCoreService + annotations
- `test_ontology_service_versioning.py` — 7 tests: ontology versioning & retrieval
- `test_ontology_assembly_path.py` — 1 test: S3 metadata path storage
- `test_build_clears_previous_results.py` — 5 tests: a starting build clears the previous run's `error` and progress fields (both the metadata and the Hybrid entrypoint), so a running build never polls as "in progress, and here is the last failure"

#### Namespaces, Authorization & the SQL Firewall

524 tests across 10 files. The density is deliberate: in this subsystem a missing test does
not fail loudly — it silently permits.

- `test_authz_policies.py` — 97 tests: the Cedar policy set, one case per (role, action)
  pair for all six roles, plus the `denyAll` fallthrough
- `test_authz_middleware.py` — 36 tests: the ASGI policy-decision point — that it wraps
  every mounted sub-app, that an unresolvable namespace returns **403 and not 404** so the
  API cannot be used to enumerate namespaces, and that a route absent from `action_map.py`
  denies rather than defaulting to allow
- `test_action_map.py` — 157 tests: the structural guarantee, and the reason coverage here is
  not a matter of discipline. `test_every_live_route_is_mapped` is parametrized over the live
  FastAPI route table, so adding an endpoint without an action breaks the build instead of
  shipping an unguarded path; `test_unknown_routes_map_to_deny_all` pins the fallthrough, and
  `test_unbound_ontology_raises_rather_than_falling_back` pins that an ontology with no
  namespace binding raises instead of guessing one
- `test_sql_firewall.py` — 41 tests: SELECT-only enforcement, including the deep AST scan
  that catches a data-modifying statement nested in a CTE (`WITH d AS (DELETE … RETURNING *)
SELECT * FROM d`), which the pre-parse regex alone would miss
- `test_grant_profile.py` — 20 tests: the deny-wins merge. The load-bearing distinction is
  `allowlist is None` (unconstrained) versus `[]` (deny-all) — several tests exist only to
  pin that a permissive grant cannot erase a restrictive grant's `columnDenylist`
- `test_namespace_service.py` — 54 tests: the creation saga (name pre-flight → conditional-put
  reservation with a TTL → per-namespace Athena workgroup → finalize) and the
  `ACTIVE|ARCHIVED|DELETING|DELETED|DELETE_FAILED` lifecycle, including partial-failure paths
- `test_namespaces_router.py` — 29 tests: the `/namespaces` REST surface
- `test_authz_bootstrap.py` — 48 tests: the `AuthzBootstrap` custom resource's idempotency,
  and that it writes **no** `tableAllowlist`/`allowedMetrics` attributes — absent means
  unrestricted, so writing `[]` would lock every seeded principal out
- `test_firewall_wiring.py` — 34 tests: that the agent runtimes actually bind a caller
  profile per invocation, since the firewall is only as good as its context
- `test_ontology_namespace_binding.py` — 8 tests: the `namespaceId` binding on ontology
  records that the bootstrap backfills

> The unit suite has grown well beyond the agent-level files itemized above
> (2710 tests across 234 files). The Tier 1/Tier 2/Tier 3 cascade, the
> governed-rule tier, the Hybrid agents, MCP Lambdas, streaming chat, shared building blocks,
> benchmark ingestion CLIs, the codeBased evaluator handler, and the remaining REST API services
> each have their own `test_*.py` — run `pytest tests/unit/ -v` for the full list.
>
> **Note:** `get_ontology_from_neptune` (ontology query agent) and `retrieve_kb_context` KB calls are MCP Gateway / Bedrock tools only available at runtime — they are not tested in unit tests.

---

## Integration Tests

Integration tests validate functionality with real AWS infrastructure. Most tests skip gracefully when required environment variables are not set.

**Location:** `tests/integration/`

### Query Agent Integration Tests

Tests the Virtual KG query workflow against real Athena tables.

**Prerequisites:**

- Athena table accessible (set `TEST_TABLE` and `TEST_CATALOG_ID`)
- AWS credentials with Athena, S3, SSM permissions
- `ATHENA_RESULTS_BUCKET` or SSM parameter configured

**Run:**

```bash
cd <repo-root>

export AWS_REGION=us-east-1
export TEST_DATABASE=your_database_name
export TEST_TABLE=your_table_name
export TEST_CATALOG_ID=AWSDataCatalog   # or 's3tablescatalog/<bucket>'

python tests/integration/test_query_agent_integration.py
```

**Coverage:**

1. ✅ Disambiguation with mock ontology (no Neptune required)
2. ✅ Athena query execution (skips if `TEST_TABLE` not set)
3. ✅ SQL results to RDF mapping (skips if `TEST_TABLE` not set)
4. ✅ Query agent creation

### Ontology Agent Integration Tests

Tests ontology generation workflow against real Athena and S3.

**Prerequisites:**

- Athena table accessible (set `TEST_TABLE` and `TEST_CATALOG_ID`)
- AWS credentials with Athena, S3, SSM, DynamoDB permissions
- `ARTIFACTS_BUCKET` for S3 persistence test
- `NEPTUNE_GATEWAY_URL` for Neptune persistence test
- `KNOWLEDGE_BASE_ID` for RAG pattern retrieval

**Run:**

```bash
cd <repo-root>

export AWS_REGION=us-east-1
export TEST_DATABASE=your_glue_database
export TEST_TABLE=your_table_name
export TEST_CATALOG_ID=AWSDataCatalog
export ARTIFACTS_BUCKET=your-s3-bucket       # optional
export NEPTUNE_GATEWAY_URL=https://your-gateway-url  # optional
export KNOWLEDGE_BASE_ID=your-kb-id          # optional

python tests/integration/test_ontology_agent_integration.py
```

**Coverage:**

1. ✅ Athena connectivity via `get_single_table_schema` (skips if `TEST_TABLE` not set)
2. ✅ Table schema retrieval (skips if `TEST_TABLE` not set)
3. ✅ Token counting (always runs)
4. ✅ Neptune persistence via file-based workflow (skips if `NEPTUNE_GATEWAY_URL` not set)
5. ✅ S3 persistence (skips if `ARTIFACTS_BUCKET` not set)
6. ✅ Phase 1 agent invocation with real Athena schema (skips if `TEST_TABLE` not set)

---

## Environment Variables

### Unit Tests (no env vars required)

All unit tests run without any AWS environment variables.

### Integration Tests

| Variable                                                       | Required For                            | Description                                                                                                                                                                                                                                                                            |
| -------------------------------------------------------------- | --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AWS_REGION`                                                   | All                                     | AWS region (default: `us-east-1`)                                                                                                                                                                                                                                                      |
| `TEST_DATABASE`                                                | All                                     | Athena/Glue database name (default: `default`)                                                                                                                                                                                                                                         |
| `TEST_TABLE`                                                   | Athena/schema tests                     | Table name within `TEST_DATABASE`                                                                                                                                                                                                                                                      |
| `TEST_CATALOG_ID`                                              | Athena/schema tests                     | Catalog ID (default: `AWSDataCatalog`)                                                                                                                                                                                                                                                 |
| `NEPTUNE_GATEWAY_URL`                                          | Neptune persistence, governed-rules e2e | AgentCore Gateway URL                                                                                                                                                                                                                                                                  |
| `ARTIFACTS_BUCKET`                                             | S3 persistence                          | S3 bucket for ontology storage                                                                                                                                                                                                                                                         |
| `KNOWLEDGE_BASE_ID`                                            | RAG patterns                            | Bedrock Knowledge Base ID                                                                                                                                                                                                                                                              |
| `ATHENA_RESULTS_BUCKET`                                        | Athena queries                          | S3 bucket for Athena query results                                                                                                                                                                                                                                                     |
| `RUN_GOVERNED_RULES_E2E`                                       | governed-rules e2e                      | Set to `1` to arm `test_governed_rules_e2e.py`; without it every test in the file skips with a printed reason                                                                                                                                                                          |
| `GOVERNED_RULES_E2E_ONTOLOGY_ID`                               | governed-rules e2e                      | A **deployed** VKG layer's ontology id (the rules graph is its `/rules` sibling)                                                                                                                                                                                                       |
| `GOVERNED_RULES_E2E_SUBJECT_CLASS` / `…_BINDING` / `…_OUTCOME` | governed-rules e2e                      | Real IRIs from that layer — a mapped subject class, a `vkg:mapsToColumn` property to bind, and a declared outcome class. Wrong IRIs make the rule publish as _advisory_ with empty `compiled_sparql`, which skips Ontop entirely and passes vacuously                                  |
| `ATHENA_WORKGROUP`                                             | governed-rules e2e                      | The deployed workgroup (`<projectName>-workgroup`). The publish fixture executes the smoke query with **your** credentials, and `_run_athena_sql` falls back to the literal `primary` — so leaving this unset turns an IAM failure into what looks like an Ontop reformulation finding |

### AWS Credentials

Tests use the default AWS credential chain:

1. Environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
2. AWS CLI credentials (`~/.aws/credentials`)
3. IAM role (if running on EC2/ECS/Lambda)

---

## Automated enforcement

Tests run automatically at three points. **GitLab CI is the only authoritative
gate** — the git hook and the deploy hook are local conveniences and are bypassable.

### 1. GitLab CI — every push (`.gitlab-ci.yml`)

GitLab runs the pipeline on every push to `origin`. The `test` stage has three
hermetic jobs: `python-unit` (with coverage gate), `frontend` (Jest), `cdk` (Jest).
Integration tests are excluded (they need live AWS — run them manually, below).

To make a red pipeline **block merges**: GitLab → Settings → Merge requests →
enable _"Pipelines must succeed"_, and protect the default branch.

The coverage floor (`COV_FLOOR` in `.gitlab-ci.yml`, mirrored by `fail_under` in
`pyproject.toml`) starts at **66** and ratchets toward 80.

### 2. Local `pre-push` git hook (opt-in)

Runs the unit suite before a push leaves your machine. Enable once per clone:

```bash
git config core.hooksPath .githooks
```

Bypass intentionally with `git push --no-verify`. Not auto-installed — this is
feedback, not enforcement; CI is the gate.

### 3. `cdk deploy` gate — npm `predeploy`

`npm run deploy` (in `cdk/`) runs `predeploy` first: `tsc` build + CDK Jest +
the Python unit suite, aborting the deploy on any failure. **Standardize on
`npm run deploy`** — a bare `npx cdk deploy` skips the gate (npm lifecycle only
fires for `npm run`).

### Integration tests (manual, env-gated)

These need live AWS and are not run in CI:

```bash
export AWS_REGION=us-east-1
export TEST_DATABASE=... TEST_TABLE=... TEST_CATALOG_ID=...
export NEPTUNE_GATEWAY_URL=... ARTIFACTS_BUCKET=...
python tests/integration/test_query_agent_integration.py
python tests/integration/test_ontology_agent_integration.py

# Governed rules — needs a deployed VKG layer plus real IRIs from it.
# Full run instructions: assets/guides/GOVERNED_RULES.md §9. A PublishBlocked here is a
# finding about Ontop's coverage of rule-shaped SPARQL — report it verbatim.
export RUN_GOVERNED_RULES_E2E=1 GOVERNED_RULES_E2E_ONTOLOGY_ID=...
export GOVERNED_RULES_E2E_SUBJECT_CLASS=... GOVERNED_RULES_E2E_BINDING=... GOVERNED_RULES_E2E_OUTCOME=...
# The publish fixture runs the smoke query as YOU, via _run_athena_sql, which
# defaults to the literal 'primary' workgroup. Point it at the deployed one or the
# publish fails on IAM and the failure is indistinguishable from an Ontop finding.
export ATHENA_WORKGROUP=<projectName>-workgroup
pytest tests/integration/test_governed_rules_e2e.py -v
```

> `test_governed_rules_e2e.py` has **not been run yet** — until it does, the set of
> rule shapes Ontop cannot reformulate is unknown and the publish gate is the only
> thing between that gap and a query-time failure.

---

## Troubleshooting

### Unit Tests Fail on Import

**`ModuleNotFoundError: No module named 'mcp_proxy_for_aws'`:**

```bash
pip install mcp-proxy-for-aws
```

**`ModuleNotFoundError: No module named 'bedrock_agentcore'`** (when running via pytest):

- Ensure `tests/unit/conftest.py` is present — it stubs this module automatically
- Run from the project root: `pytest tests/unit/ -v`

**`ModuleNotFoundError: No module named 'bedrock_agentcore'`** (when running directly with `python`):

- The conftest.py is only loaded by pytest, not by direct Python invocation
- Install the full `agents/requirements.txt` or use pytest instead

**Import errors / module not found:**

- Run from the project root directory
- Verify `agents/` subdirectories exist with correct package names

### Integration Tests Fail

**Athena errors:**

- Verify `TEST_TABLE` exists in `TEST_DATABASE`
- Check `TEST_CATALOG_ID` matches the catalog type (Glue vs S3 Tables)
- Verify Athena workgroup permissions and results bucket

**Neptune persistence errors:**

- Verify `NEPTUNE_GATEWAY_URL` is set and reachable
- Check IAM permissions for `bedrock-agentcore` service

**AWS credentials:**

```bash
aws sts get-caller-identity
echo $AWS_REGION
```
