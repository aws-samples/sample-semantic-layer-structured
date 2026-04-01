# Semantic Layer Tests

This directory contains all tests for the semantic layer project, organized into unit tests and integration tests.

## Directory Structure

```
tests/
├── README.md                                       # This file
├── unit/
│   ├── conftest.py                                # pytest stubs for heavyweight SDK deps
│   ├── test_ontology_agent.py                     # Ontology agent — imports, tools, prompts (9 tests)
│   ├── test_ontology_athena_tools.py              # Ontology agent — Athena integration tools (25 tests)
│   ├── test_ontology_revision_mode.py             # Ontology agent — revision mode workflow (5 tests)
│   ├── test_query_agent.py                        # Ontology query agent — tools, disambiguation (12 tests)
│   ├── test_metadata_agent.py                     # Metadata agent — tools, token mgr, catalog routing (7 tests)
│   ├── test_metadata_query_agent.py               # Metadata query agent — tools, prompts, state (8 tests)
│   ├── test_metadata_agent_annotations.py         # Metadata agent — annotation hint injection (2 tests)
│   ├── test_metadata_api.py                       # REST API endpoint schemas (3 tests)
│   ├── test_metadata_service.py                   # Metadata enrichment & query service (7 tests)
│   ├── test_agentcore_service.py                  # AgentCore service + annotations (2 tests)
│   ├── test_ontology_service_versioning.py        # Ontology versioning & retrieval (6 tests)
│   └── test_ontology_assembly_path.py             # S3 metadata path storage (1 test)
│
└── integration/
    ├── test_ontology_agent_integration.py         # Real Athena + Neptune/S3 (6 tests)
    └── test_query_agent_integration.py            # Real Athena + mock ontology (5 tests)
```

**Test Counts:**
- Unit tests: ~87 tests across 13 files
- Integration tests: 11 tests across 2 files
- **Total: ~98 tests**

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

**All unit tests via pytest (recommended):**
```bash
cd /Users/huthmac/Documents/AWS/00_workspace/semantic-layer
pytest tests/unit/ -v
```

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

**`test_ontology_athena_tools.py`** — 25 tests
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

#### Ontology Query Agent
**`test_query_agent.py`** — 12 tests
- ✅ Module imports and token manager
- ✅ State management (`_agent_state` reset and structure)
- ✅ Local tool definitions (`disambiguate_query_terms`, `execute_sql_query`, `map_sql_results_to_rdf`)
- ✅ Disambiguation logic with mock ontology
- ✅ `invoke` entrypoint signature
- ✅ Agent creation
- ✅ Disambiguation false-positive fix (`coverage` class + table → CLEAR)
- ✅ `QueryAnswer` structured output Pydantic model
- ✅ KB context returns error JSON when `BEDROCK_KB_ID` unset
- ✅ KB synonym disambiguation
- ✅ SPARQL context confidence boosting

#### Metadata Agent
**`test_metadata_agent.py`** — 7 tests
- ✅ Module imports (all 7 tools)
- ✅ Token manager
- ✅ Tool definitions and signatures (`get_database_tables`, `get_table_schema`, `sample_table_data`, `update_glue_table_metadata`, `update_glue_database_description`, `save_metadata_document_to_s3`, `update_progress`)
- ✅ `invoke` entrypoint signature
- ✅ `update_progress` response schema (mocked DynamoDB)
- ✅ Agent creation
- ✅ Per-table catalog routing (`_get_catalog_for_table`, S3 Tables vs Glue)

**`test_metadata_agent_annotations.py`** — 2 tests
- ✅ Annotation hint injection into agent prompts

#### Metadata Query Agent
**`test_metadata_query_agent.py`** — 8 tests
- ✅ Module imports (all 3 tools + helpers)
- ✅ Query prompts (`SYSTEM_PROMPT` non-empty, `QUERY_MODEL_ID` set, tool names referenced)
- ✅ State management (`_agent_state` keys, reset behavior)
- ✅ Tool definitions and signatures
- ✅ `invoke` entrypoint signature
- ✅ Agent creation
- ✅ `retrieve_kb_context` returns error JSON when `BEDROCK_KB_ID` unset
- ✅ Disambiguation with mock KB context

#### REST API Services
- `test_metadata_api.py` — 3 tests: API endpoint schemas
- `test_metadata_service.py` — 7 tests: MetadataService enrichment & query
- `test_agentcore_service.py` — 2 tests: AgentCoreService + annotations
- `test_ontology_service_versioning.py` — 6 tests: ontology versioning & retrieval
- `test_ontology_assembly_path.py` — 1 test: S3 metadata path storage

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
cd /Users/huthmac/Documents/AWS/00_workspace/semantic-layer

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
cd /Users/huthmac/Documents/AWS/00_workspace/semantic-layer

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

| Variable | Required For | Description |
|----------|-------------|-------------|
| `AWS_REGION` | All | AWS region (default: `us-east-1`) |
| `TEST_DATABASE` | All | Athena/Glue database name (default: `default`) |
| `TEST_TABLE` | Athena/schema tests | Table name within `TEST_DATABASE` |
| `TEST_CATALOG_ID` | Athena/schema tests | Catalog ID (default: `AWSDataCatalog`) |
| `NEPTUNE_GATEWAY_URL` | Neptune persistence | AgentCore Gateway URL |
| `ARTIFACTS_BUCKET` | S3 persistence | S3 bucket for ontology storage |
| `KNOWLEDGE_BASE_ID` | RAG patterns | Bedrock Knowledge Base ID |
| `ATHENA_RESULTS_BUCKET` | Athena queries | S3 bucket for Athena query results |

### AWS Credentials

Tests use the default AWS credential chain:
1. Environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
2. AWS CLI credentials (`~/.aws/credentials`)
3. IAM role (if running on EC2/ECS/Lambda)

---

## CI/CD Integration

### Unit Tests in CI/CD

```yaml
- name: Install dependencies
  run: pip install -r agents/requirements.txt

- name: Run unit tests
  run: pytest tests/unit/ -v
```

### Integration Tests in CI/CD

```yaml
- name: Configure AWS credentials
  uses: aws-actions/configure-aws-credentials@v1
  with:
    role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
    aws-region: us-east-1

- name: Run integration tests
  env:
    TEST_DATABASE: ${{ secrets.TEST_DATABASE }}
    TEST_TABLE: ${{ secrets.TEST_TABLE }}
    TEST_CATALOG_ID: ${{ secrets.TEST_CATALOG_ID }}
    NEPTUNE_GATEWAY_URL: ${{ secrets.NEPTUNE_GATEWAY_URL }}
    ARTIFACTS_BUCKET: ${{ secrets.ARTIFACTS_BUCKET }}
  run: |
    python tests/integration/test_query_agent_integration.py
    python tests/integration/test_ontology_agent_integration.py
```

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
