# Semantic Layer CDK Infrastructure

AWS CDK infrastructure for deploying a complete semantic layer with insurance data, knowledge graphs, and AI agents.

## Architecture Overview

This CDK application deploys **up to 21 stacks** (17 always-on + 4 conditional on the
`enableRealtimeReplication`, `enableBatchReplication`, and `enableOntologyAgents` feature flags).
Other flags — including `enableHybrid` (default `true`), which adds the two Hybrid runtimes,
`enableSnowflakeConnector` (default `true`), which adds the Athena Snowflake connector, and
`enableGovernedRules` (default `true`), which arms the `/rules` API (`RULES_ENABLED` on the REST
Lambda) and the query-time rule tier (`GOVERNED_RULES_ENABLED` on the ontology-query **and**
semantic-layer-query runtimes), and `enableAuthz` (default `false`), which arms Cedar enforcement
in the REST API — toggle resources _inside_ always-deployed stacks rather than adding stacks.
See the **root [`README.md`](../README.md)** for the authoritative stack list, dependency graph,
deployment modes, and capability matrix — this file documents CDK-specific operational detail.

### Always-on stacks

1. **Networking** - VPC with public/private/isolated subnets for Neptune
2. **DynamoDB** - 12 insurance tables + metadata / chat-sessions / feedback / metrics / namespaces / grants tables
3. **Glue Catalog** - DynamoDB + Iceberg databases, crawlers
4. **Data Lake** - S3 Tables (Iceberg) bucket, artifacts / Athena-results / KB / logging buckets, Lake Formation grants
5. **Bedrock Knowledge Base** - two KBs (S3 Vectors): ontology-patterns + semantic-rag
6. **Athena** - workgroup + DynamoDB connector + Lake Formation admin chain
7. **AgentCore Memory** - single SemanticStrategy resource (lessons-learned)
8. **Guardrails** - Bedrock Guardrails (content filters + PII)
9. **CloudFront Storage** - CloudFront distribution + S3 website bucket + OAC
10. **Auth** - Cognito User Pool / Identity Pool / OAuth 2.0 (SPA + MCP + M2M clients)
11. **AgentCore** - up to 7 AgentCore Runtimes + ECR + Neptune/Ontop Gateway
12. **AgentCore Eval** - online evaluation configs for the query runtimes
13. **Doc Pipeline** - staged Lambda ingestion (chunk → NER → embed → link → ruleminer → index)
14. **Lambda REST API** - FastAPI container on Lambda + HTTP API (JWT)
15. **MCP Server** - MCP Gateway (CUSTOM_JWT) + mcp-tools Lambda + Chat Gateway
16. **MCP Proxy** - MCP OAuth 2.0 proxy (HTTP API + Lambda)
17. **Frontend** - React build + S3 sync + CloudFront invalidation (CodeBuild)

### Conditional stacks

- **Neptune** (`enableOntologyAgents=true`) - RDF/SPARQL cluster in VPC
- **Stream Processor** (`enableRealtimeReplication=true`) - DynamoDB Streams → PyIceberg → S3 Tables
- **Zero-ETL** + **Normalized Views** (`enableBatchReplication=true`) - Glue Zero-ETL + 40 Iceberg MVs

## Prerequisites

- **AWS Account** with appropriate permissions
- **Node.js 18+** and npm
- **AWS CDK CLI** v2.128.0 or later
- **TypeScript** 5.3+
- **AWS CLI** configured with credentials
- **Docker** (for agent container images)

## Installation

```bash
# Navigate to CDK directory
cd cdk

# Install dependencies
npm install

# Build TypeScript
npm run build

# Bootstrap CDK (first time only)
cdk bootstrap
```

## Configuration

The CDK app automatically detects your AWS account and region from:

- AWS CLI configuration
- Environment variables (CDK_DEFAULT_ACCOUNT, CDK_DEFAULT_REGION)
- AWS credentials file

Optional configuration in `bin/app.ts`:

- `autoStartCrawlers`: Auto-start Glue crawlers on deployment (default: true)
- `autoStartIngestion`: Auto-start Bedrock KB ingestion (default: true)

## Deployment

### Full Automated Deployment

Deploy all stacks with automated setup:

```bash
# Deploy all stacks (includes automated triggers).
# `npm run deploy` runs the `predeploy` gate first (tsc build + CDK Jest +
# Python unit suite), then `cdk deploy --all`. Prefer it over a bare
# `cdk deploy --all`, which skips the test gate.
npm run deploy
```

> To skip CDK's interactive approval prompts in automation, run the underlying
> command directly: `npm run build && npm test && cdk deploy --all --require-approval never`
> (this preserves the test gate while bypassing prompts).

This will:

1. ✅ Create VPC and networking
2. ✅ Create DynamoDB tables with synthetic data
3. ✅ Create S3 data lake buckets (historical data, artifacts, logging)
4. ✅ Create Glue databases and crawlers
5. ✅ **Auto-start Glue crawlers** to discover schemas
6. ✅ Create Neptune cluster in VPC
7. ✅ Create two Bedrock Knowledge Bases backed by S3 Vectors (ontology-patterns + semantic-rag)
8. ✅ **Auto-deploy ontology patterns to S3**
9. ✅ **Auto-start Knowledge Base ingestion**
10. ✅ Create Athena workgroup and DynamoDB connector
11. ✅ Create AgentCore Runtime with Strands agents
12. ✅ Create CloudFront distribution and S3 bucket for frontend
13. ✅ Create Cognito User Pool with CloudFront callback URLs
14. ✅ Create Bedrock Guardrails for AI safety
15. ✅ Create Lambda REST API with API Gateway
16. ✅ **Auto-build and deploy React frontend** via CodeBuild
17. ✅ Configure CloudFront with API Gateway origin

### Deploy Individual Stacks

```bash
# Deploy in dependency order
npm run build

# Backend infrastructure (1-8)
cdk deploy semantic-layer-networking
cdk deploy semantic-layer-dynamodb
cdk deploy semantic-layer-data-lake
cdk deploy semantic-layer-glue-catalog
cdk deploy semantic-layer-neptune
cdk deploy semantic-layer-bedrock-kb
cdk deploy semantic-layer-athena
cdk deploy semantic-layer-agentcore
cdk deploy semantic-layer-cloudfront-storage
cdk deploy semantic-layer-auth
cdk deploy semantic-layer-guardrails
cdk deploy semantic-layer-lambda-api
cdk deploy semantic-layer-frontend
```

**Important:** CloudFront Storage must be deployed before Auth Stack to ensure Cognito receives the correct callback URLs.

### View Changes Before Deployment

```bash
npm run diff
```

### Synthesize CloudFormation Templates

```bash
npm run synth
```

## Stack Details

### 1. Networking Stack

Creates VPC with:

- 2 Availability Zones
- Public, Private, and Isolated subnets
- NAT Gateway for private subnet internet access
- VPC Endpoints for S3, DynamoDB, Bedrock, Athena

**Outputs:**

- VPC ID
- Security Group IDs

### 2. DynamoDB Stack

Creates:

- **Insurance Data Table** - Single-table design with GSIs
  - Partition key: `pk`
  - Sort key: `sk`
  - 3 Global Secondary Indexes
  - DynamoDB Streams enabled
- **Ontology Metadata Table** - Tracks ontology versions
- **Namespaces Table** (`${projectName}-namespaces`) - namespace records
  (`NS#<uuid>` / `METADATA`) and name reservations (`NS_NAME#<name>` /
  `RESERVATION`). TTL on `ttl` so a crashed creation cannot hold a name
  forever; GSI `NameByIndex` on `name` for the uniqueness pre-flight and
  name → id resolution
- **Grants Table** (`${projectName}-grants`) - role grants keyed
  `Namespace::<nsId>#User::<sanitized>` (or `…#Group::<name>`) / `ROLE#<role>`,
  carrying the optional data restrictions `tableAllowlist`, `columnDenylist`
  and `allowedMetrics`. GSI `PrincipalIndex` on `principalKey` — this is what
  both the REST API's PDP and the agent-side SQL firewall read

**Access Patterns:**

- Get party by ID
- Query coverages by policy
- Query holdings by policy
- Query financial activities by policy

### 3. Data Lake Stack

Creates buckets + Lake Formation grants:

- **S3 Tables** - analytical data as Apache Iceberg tables
- **Artifacts** - ontologies (Turtle), metadata documents (Markdown)
- **Athena Results** - query output storage (7-day lifecycle)
- **Knowledge Base** - source docs for the Bedrock KBs
- **Logging** - access logs

All buckets have:

- Encryption at rest
- Versioning enabled
- Public access blocked

### 4. Glue Catalog Stack

Creates:

- **DynamoDB Database** - Catalog for operational data
- **Historical Database** - Catalog for S3 Parquet data
- **DynamoDB Crawler** - Runs daily at 2 AM UTC
- **S3 Crawler** - Runs daily at 3 AM UTC

### 5. Neptune Stack

Creates:

- **Neptune Cluster** (v1.3.2.0)
  - Primary instance: db.r6g.xlarge
  - Reader instance: db.r6g.large
- IAM authentication enabled
- Encryption at rest
- Audit logging enabled
- Backup retention: 7 days

**Endpoints:**

- SPARQL: `https://ENDPOINT:8182/sparql`
- Loader: `https://ENDPOINT:8182/loader`

### 6. Bedrock Knowledge Base Stack

Creates:

- **Two Knowledge Bases** with Titan Embed Text v2 (1024-dim)
- **S3 Vectors** vector store (not OpenSearch Serverless)
- S3 data sources: ontology patterns (VKG) + enriched metadata (Semantic RAG)

### 7. Athena Stack

Creates:

- **Athena Workgroup** for semantic layer queries
- **DynamoDB Connector** Lambda for federated queries
- **Data Catalog** for DynamoDB access

**Query Capabilities:**

- Query current DynamoDB data
- Query historical S3 Parquet data
- Cross-source joins (DynamoDB + S3)

### 8. AgentCore Stack

Creates:

- **Up to 7 AgentCore Runtimes** - `ontology` + `query` (VKG), `metadata` + `metadata-query` (Semantic RAG), `query-suggestions`, plus `semantic-layer-query` + `semantic-layer-creation` (Hybrid). VKG runtimes are conditional on `enableOntologyAgents`; the two Hybrid runtimes on `enableHybrid` (default `true`, so 7 runtimes is the default deploy).
- **Neptune/Ontop Gateway** - HTTP gateway + Ontop SPARQL→SQL translate Lambda (Java 21) for VKG Phase 5
- **JWT-inbound runtimes** - accept Cognito JWT (no SigV4) so the MCP/chat gateways and browser can invoke them
- **Shared ECR repo + CodeBuild (ARM64)** per agent
- **Benchmark-eval codeBased evaluator Lambda** (`benchmark-eval-handler/`) - one Python 3.12 Lambda backing three AgentCore codeBased evaluators (EX / R-VES / Soft-F1); executes predicted vs. gold SQL on Athena per trace and returns one metric score (ARN is a stack output; the benchmark-eval notebooks register the evaluators against it)
- **IAM Roles** (least-privilege per agent) + Lake Formation grants for Iceberg access. The grant set is uniform across the six agent roles — DB `DESCRIBE` + table-wildcard `SELECT`/`DESCRIBE` on the `normalized` namespace and on the DynamoDB federated database — including both Hybrid roles. The IAM `lakeformation:GetDataAccess` action alone is **not** sufficient — the grant is a separate authorization. Keep the set uniform: a missing grant does not fail the build, it makes the schema probe log a WARN and the agent model the table from the LLM's memory instead, and at query time it surfaces as `TABLE_OR_VIEW_NOT_FOUND` rather than `ACCESS_DENIED`

**Agent Capabilities:**

- Generate RDF ontologies from Glue Data Catalog
- Load ontologies into Neptune
- Execute federated queries via Athena
- Retrieve patterns from Bedrock Knowledge Base

### 9. CloudFront Storage Stack

Creates CloudFront distribution and S3 bucket for frontend hosting:

- **S3 Website Bucket** with:
  - Server-side encryption (S3-managed)
  - Block all public access (CloudFront OAC only)
  - Access logging to logging bucket
  - Auto-delete on stack removal
- **CloudFront Distribution** with:
  - Origin Access Control (OAC) for S3
  - Custom cache policy for SPA routing
  - HTTPS redirect (TLS 1.2+)
  - Error pages (404/403 → index.html)
  - Logging to S3
  - Price class: US, Canada, Europe

**Why This Stack Exists:**
Created before AuthStack to provide CloudFront URL for Cognito callback URLs. This solves the circular dependency issue.

**Outputs:**

- CloudFront URL: `https://<distribution-id>.cloudfront.net`
- Distribution ID for cache invalidation
- S3 bucket name for deployment

### 10. Auth Stack

Creates Cognito authentication infrastructure:

- **Cognito User Pool** with:
  - Email-based sign-in
  - Self-registration enabled
  - Email verification required
  - Password policy (8+ chars, uppercase, lowercase, numbers, symbols)
  - Feature plan: ESSENTIALS (includes threat protection)
  - User groups: Admin, Users
- **User Pool Client** with:
  - OAuth 2.0 flows enabled
  - Callback URLs: CloudFront + localhost
  - Logout URLs: CloudFront + localhost
  - Token validity: 8 hours
  - Auth flows: SRP, Admin, Custom
- **Identity Pool** with:
  - Authenticated role with scoped permissions
  - Unauthenticated role (DENY all)
- **Regional WAF Web ACL** with:
  - IP rate limiting (3000 req/5min)
  - AWS Managed Rules (Common, Bot Control, Bad Inputs, Unix, SQLi)

**Authenticated Role Permissions:**

- Bedrock model invocation (Claude, Nova, Titan)
- Bedrock Guardrail application
- Bedrock Knowledge Base retrieval
- S3 bucket access (artifacts)
- DynamoDB access (operational tables)
- Transcribe (for live assistant)
- CloudWatch Logs
- X-Ray tracing

**Authentication Mode:**

- **Direct mode** (default): Username/password in React app
- **OAuth mode**: Cognito Hosted UI (set `enableDirectAuth` context to false)

**Outputs:**

- User Pool ID
- User Pool Client ID
- Identity Pool ID
- Authentication mode

### 11. Guardrails Stack

Creates Bedrock Guardrails for AI safety:

- **Content Filtering** - Block harmful content
- **PII Redaction** - Remove sensitive information
- **Topic Denial** - Prevent off-topic responses
- **Word Filters** - Block prohibited terms

**Applied To:**

- AgentCore Runtime responses
- Frontend chat interface
- API responses

### 12. Lambda REST API Stack

Creates serverless REST API:

- **Lambda Function** (ARM64, Docker) with:
  - 1024 MB memory
  - 15-minute timeout
  - No VPC (public for faster cold starts)
  - ARM64 build via CodeBuild (avoids cross-compilation)
- **HTTP API Gateway** with:
  - JWT Authorizer (Cognito)
  - CORS configuration
  - CloudWatch access logs
  - Custom domain support

**API Routes** — there are only three, deliberately:

- `GET /health` - health check (no auth)
- `OPTIONS /{proxy+}` - CORS preflight, **no authorizer** (a preflight carries no `Authorization`
  header, so requiring one would break every browser call)
- `ANY /{proxy+}` - everything else, behind the Cognito JWT authorizer

There is no per-sub-app route. `ANY` on a single catch-all is a hard requirement, not a
simplification: each route × method produces an `AWS::Lambda::Permission` statement, and Lambda's
resource policy caps at **20 KB** (~43 statements). With this many mounted sub-apps an explicit
per-path, per-method route set exceeds it and the deploy fails.

That constraint is also why authorization is **ASGI middleware inside the app** rather than an API
Gateway REQUEST authorizer: a REQUEST authorizer on the catch-all would mean re-implementing JWT
validation, and a per-route authorizer set blows the same budget. Routing all paths through one
integration means FastAPI's router is the only thing that knows the real path — so the PDP has to
live there too. See the root README's
[Namespaces, Authorization & the SQL Firewall](../README.md#namespaces-authorization--the-sql-firewall).

**Architecture:**

```
Client → CloudFront → API Gateway → Lambda → AgentCore Runtime
                                           ↓
                                    Secrets Manager (CloudFront domain)
```

Lambda delegates all data operations to AgentCore Runtime agents, maintaining a clean separation of concerns.

**Authorization is installed here, and armed separately.** The stack wires the Cedar
policy-decision point into the FastAPI app and mounts `/namespaces`, both keyed off
`NAMESPACES_TABLE`. `ENABLE_AUTHZ` (from the `enableAuthz` context flag, default `false`) is a
_second_, independent switch that decides only whether a Deny becomes a `403`. So a normal deploy
lands the tables, the router, the seeding and the firewall with nothing denying, and enforcement is
armed on a follow-up deploy — see the root README's Deployment Modes.

- The function's role gets read/write on the **namespaces** and **grants** tables, and
  `athena:CreateWorkGroup` / `TagResource` / `GetWorkGroup` so `NamespaceService` can create a
  per-namespace workgroup.
- `requirements.txt` adds **`cedarpy`** — a Rust-backed wheel. The CodeBuild project is native
  ARM64, so it resolves (or builds) without cross-compilation.

**`AuthzBootstrap` custom resource.** A one-shot Lambda (`../authz-bootstrap/index.py`, behind a
`cr.Provider`) that runs on every deploy and is idempotent. It seeds the `default` namespace as
`ACTIVE` with its workgroup, writes the name reservation **with no TTL**, writes a `RES#GLOBAL`
`platform-admin` grant for each `-c platformAdminUsers=` principal, and **backfills `namespaceId`
onto existing ontology metadata rows**. That backfill is load-bearing: the runtime namespace
resolver raises on a missing binding rather than defaulting, so without it every ontology-scoped
route on a pre-existing layer would 500 once enforcement is armed.

Note what the bootstrap deliberately does _not_ write: a grant it creates carries **no**
`tableAllowlist` / `allowedMetrics` attributes at all. An absent restriction means unrestricted; an
empty list would mean deny-all.

**Athena grants — one exception to that delegation.** Everything the API does with Athena is
metadata (the admin data-source picker listing catalogs, databases and tables), except the
governed-rule **publish gate**, which executes the compiled rule's `LIMIT 1` smoke query itself.
That query runs as the Lambda's own principal, so the role also carries:

- `athena:StartQueryExecution` / `GetQueryExecution` / `StopQueryExecution` on
  `${projectName}-workgroup` (`GetQueryResults` is deliberately absent — the gate waits for
  `SUCCEEDED` and reads nothing out)
- read/write on the **Athena results bucket**. The workgroup sets
  `enforceWorkGroupConfiguration`, so results land in its own location no matter what the client
  requests, written as the _calling_ principal — the bucket must therefore be the one
  `AthenaStack` configured on the workgroup, not the artifacts bucket `main.py` falls back to
- read/write on each connector **spill** bucket (+ `Decrypt` on the DynamoDB connector's spill
  KMS key). A `LIMIT 1` reads records, and a connector returns records by spilling them to S3 for
  the caller to read back — which the metadata-only picker never had to do
- `ATHENA_WORKGROUP`, injected from the same value the grant is scoped to. `main.py` falls back
  to the literal `primary`, so a grant on `${projectName}-workgroup` is inert if the env var is
  missing

None of this fails the deploy or the API call — it fails the _query_, and the admin sees a `422`
naming `StartQueryExecution`, which reads like a broken rule. Each of the four is pinned by
`__tests__/rules-publish-athena-grants.test.ts` (6 tests).

**Outputs:**

- API Gateway endpoint URL
- Lambda function name and ARN
- CloudFront header secret (for origin verification)

### 13. Frontend Stack

Builds and deploys React application:

- **CodeBuild Project** with:
  - Node.js 18 runtime
  - ARM64 compute for faster builds
  - Automatic npm install and build
  - CloudFront cache invalidation on deployment
- **Custom Resource Provider** with:
  - onEvent handler (starts build)
  - isComplete handler (polls build status)
  - 15-minute timeout
- **CloudFront Integration** with:
  - API Gateway origin at `/api/*` path
  - Health check at `/health`
  - Custom header verification

**Build Environment Variables:**

- `REACT_APP_API_URL` - API endpoint
- `REACT_APP_USER_POOL_ID` - Cognito User Pool
- `REACT_APP_USER_POOL_CLIENT_ID` - Cognito Client
- `REACT_APP_USER_POOL_DOMAIN` - OAuth domain
- `REACT_APP_AUTH_MODE` - Authentication mode (direct/oauth)
- `REACT_APP_CUSTOMER_NAME` - Application branding
- `REACT_APP_CUSTOMER_LOGO` - Logo path

**Deployment Flow:**

1. FrontendStack triggers CodeBuild
2. CodeBuild downloads source from S3 asset
3. Runs `npm install && npm run build`
4. Deploys build artifacts to S3 website bucket
5. Invalidates CloudFront cache
6. Custom resource polls until complete

**Frontend Features:**

- Single Page Application (SPA) with React Router
- AWS Cloudscape Design System components
- Cognito authentication integration
- AG-UI streaming chat over Server-Sent Events (SSE)
- Responsive design
- Error boundaries

## Accessing the Application

After deployment, access the semantic layer application:

```bash
# Get CloudFront URL from stack outputs
aws cloudformation describe-stacks \
  --stack-name semantic-layer-cloudfront-storage \
  --query 'Stacks[0].Outputs[?OutputKey==`CloudFrontURL`].OutputValue' \
  --output text
```

**Default URL format:** `https://<distribution-id>.cloudfront.net`

### Create a User

```bash
# Get User Pool ID
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name semantic-layer-auth \
  --query 'Stacks[0].Outputs[?OutputKey contains `UserPoolId`].OutputValue' \
  --output text)

# Create admin user
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username admin@example.com \
  --user-attributes Name=email,Value=admin@example.com Name=email_verified,Value=true \
  --temporary-password "TempPass123!" \
  --message-action SUPPRESS

# Add to Admin group
aws cognito-idp admin-add-user-to-group \
  --user-pool-id $USER_POOL_ID \
  --username admin@example.com \
  --group-name Admin
```

### Login

1. Navigate to CloudFront URL
2. Click "Sign In"
3. Enter credentials (change password on first login)
4. Access semantic layer features

## Post-Deployment Steps

The deployment is largely automated. Here are the optional verification steps:

### 1. Verify Synthetic Data (Auto-Loaded)

Synthetic insurance data is automatically loaded during DynamoDB stack deployment (when `loadSyntheticData: true`):

```bash
# Verify data was loaded
aws dynamodb scan \
  --table-name semantic-layer-insurance-data \
  --select COUNT

# Check specific items
aws dynamodb get-item \
  --table-name semantic-layer-insurance-data \
  --key '{"pk":{"S":"PARTY#P001"},"sk":{"S":"PARTY#P001"}}'
```

### 2. Monitor Glue Crawlers (Auto-Started)

Glue crawlers start automatically on deployment. Monitor their progress:

```bash
# Check DynamoDB crawler status
aws glue get-crawler --name semantic-layer-dynamodb-crawler

# Check S3 crawler status
aws glue get-crawler --name semantic-layer-s3-crawler
```

Status will be `RUNNING` → `STOPPING` → `READY`. Typically takes 5-15 minutes.

### 3. Verify Knowledge Base Ingestion (Auto-Started)

Ontology patterns are automatically deployed to S3 and ingestion starts automatically:

```bash
# Get ingestion job ID from stack outputs
aws cloudformation describe-stacks \
  --stack-name semantic-layer-bedrock-kb \
  --query 'Stacks[0].Outputs'

# Check ingestion status
aws bedrock-agent get-ingestion-job \
  --knowledge-base-id KB_ID \
  --data-source-id DS_ID \
  --ingestion-job-id JOB_ID
```

### 4. Verify AgentCore Runtime Deployment

AgentCore Runtime agents are automatically deployed by the AgentCoreStack:

```bash
# Get AgentCore Runtime ARNs
aws cloudformation describe-stacks \
  --stack-name semantic-layer-agentcore \
  --query 'Stacks[0].Outputs[?contains(OutputKey,`RuntimeArn`)].[OutputKey,OutputValue]' \
  --output table
```

**Agents Deployed** (up to 5, depending on feature flags):

- **Ontology Agent** / **Query Agent** (VKG) - generate ontologies, query via SPARQL→SQL→Athena
- **Metadata Agent** / **Metadata Query Agent** (Semantic RAG) - enrich metadata, query via KB→SQL→Athena
- **Query Suggestions Agent** - dynamic suggested questions

All agents are built (CodeBuild ARM64) and deployed on Bedrock AgentCore Runtime using the Strands SDK during stack creation.

### 5. Load Sample Ontology to Neptune (Optional)

To load a sample ontology into Neptune, use the NeptuneLoaderTrigger construct or manually:

```typescript
// Add to neptune-stack.ts or create a separate stack
new NeptuneLoaderTrigger(this, 'SampleOntologyLoader', {
  neptuneEndpoint: neptuneStack.clusterEndpoint,
  neptunePort: neptuneStack.port,
  s3Path: 's3://your-bucket/ontologies/sample-ontology.ttl',
  iamRoleArn: neptuneStack.loadRole.roleArn,
  vpc: networkingStack.vpc,
  securityGroup: neptuneStack.securityGroup,
  format: 'turtle',
});
```

## Testing

### Test Frontend Application

```bash
# Open in browser
open https://$(aws cloudformation describe-stacks \
  --stack-name semantic-layer-cloudfront-storage \
  --query 'Stacks[0].Outputs[?OutputKey==`DistributionDomainName`].OutputValue' \
  --output text)
```

**Test Authentication:**

1. Click "Sign In"
2. Enter user credentials
3. Verify successful authentication
4. Check JWT token in browser developer tools

**Test API Integration:**

1. Navigate to "Data Sources" page
2. Verify DynamoDB tables are listed
3. Click "Query" to test semantic queries
4. Check CloudWatch Logs for Lambda execution

### Test REST API Directly

```bash
# Get API endpoint
API_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name semantic-layer-lambda-api \
  --query 'Stacks[0].Outputs[?OutputKey==`RestApiUrl`].OutputValue' \
  --output text)

# Test health check (no auth)
curl $API_ENDPOINT/health

# Test authenticated endpoint (requires JWT token)
# 1. Login via frontend to get token
# 2. Use token in Authorization header
curl -H "Authorization: Bearer <jwt-token>" \
     $API_ENDPOINT/api/status
```

### Query DynamoDB via Athena

```sql
SELECT * FROM dynamodb_catalog."semantic-layer-insurance-data" LIMIT 10;
```

### Query Historical Data via Athena

```sql
SELECT policy_id, COUNT(*) as claim_count
FROM insurance_historical.claims
WHERE year = 2024
GROUP BY policy_id;
```

### Query Snowflake via Athena

**On by default** (`enableSnowflakeConnector: true` in `cdk.json`). The
[AthenaSnowflakeConnector](https://docs.aws.amazon.com/athena/latest/ug/connectors-snowflake.html)
is deployed with one catalog per Snowflake **database**, configured from `cdk/.env`
(`SNOWFLAKE_ACCOUNT` and `SNOWFLAKE_WAREHOUSE` are required — synth fails loudly without them):

```bash
cdk deploy semantic-layer-dev-athena semantic-layer-dev-agentcore
```

Context still overrides `.env` per invocation, and `-c enableSnowflakeConnector=false` opts out:

```bash
cdk deploy semantic-layer-dev-athena semantic-layer-dev-agentcore \
  -c snowflakeAccount=<org>-<account> \
  -c snowflakeWarehouse=<WAREHOUSE> \
  -c snowflakeDatabases=FHIR_DB,SNOWFLAKE_SAMPLE_DATA
```

> ⚠️ The default used to be `false`, which made every deploy destructive: the resources are
> conditional on context rather than disabled by it, so a deploy without the flag **deleted** the
> live connector, secret, spill bucket, role and catalog. `bin/__tests__/app-flags.test.ts` fails if
> the default flips back — see [`README.md`](README.md#feature-flags).

Then populate the key-pair credential — it is deliberately **not** in the CloudFormation
template, since template bodies are readable via `cloudformation:GetTemplate` — and re-apply
the identifier casing, which any connector deploy resets:

```bash
npm run sync:snowflake-secret     # from cdk/, reads cdk/.env
npm run set:snowflake-casing      # both are chained into `npm run deploy:snowflake`
```

```sql
-- The Athena "database" level is a Snowflake SCHEMA; the Snowflake database is the catalog.
SHOW DATABASES IN snowflake_fhir_db;
SELECT * FROM snowflake_fhir_db.fhir_r4.patients LIMIT 10;
```

A catalog cannot span Snowflake databases: a connector's connection string pins exactly one
`db=`, and it lists schemas only within that database. Each entry in `snowflakeDatabases`
therefore gets its own connector Lambda and catalog; the credential secret, the execution
role and the spill bucket are shared (one `SpillPrefix` per database). Every agent role that
runs Athena SQL is granted invoke plus the `athena:GetDataCatalog` family, so no agent code
changes — without that second grant the first agent query fails `CATALOG_NOT_FOUND`, which
reads as a missing catalog rather than a missing permission. The **REST API** Lambda role is
in that set too, not just the agents: the governed-rule publish gate executes a `LIMIT 1`, so
it needs the spill grant the metadata-only data-source picker did not — see [§12](#12-lambda-rest-api-stack).

Snowflake object names are upper case while Athena lower-cases identifiers before handing
them to a connector, so a table that resolves in Snowflake reports "not found" through Athena
until the Lambda's `casing_mode` env var is `UPPER`. The SAR template has no parameter for
it, hence the post-deploy script above.

See [`cdk/README.md`](README.md) for the full setup, including the Snowflake-side bootstrap
and why the connector runs outside the VPC.

### Query Neptune via SPARQL

```sparql
PREFIX : <http://insurance-ontology.example.com/>

SELECT ?party ?policy
WHERE {
  ?policy :hasParty ?party .
}
LIMIT 10
```

## Monitoring

View CloudWatch metrics and logs:

```bash
# Frontend build logs
aws logs tail /aws/codebuild/semantic-layer-frontend-build --follow

# Lambda REST API logs
aws logs tail /aws/lambda/semantic-layer-lambda-api-rest-api --follow

# API Gateway access logs
aws logs tail /aws/apigateway/semantic-layer-lambda-api-rest-api --follow

# AgentCore Runtime logs
aws logs tail /aws/bedrock/agentcore/semantic-layer --follow

# Neptune logs
aws logs tail /aws/neptune/semantic-layer-neptune-cluster/audit --follow

# Athena query history
aws athena list-query-executions --work-group semantic-layer-workgroup
```

### CloudFront Monitoring

```bash
# View CloudFront metrics
aws cloudwatch get-metric-statistics \
  --namespace AWS/CloudFront \
  --metric-name Requests \
  --dimensions Name=DistributionId,Value=<distribution-id> \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 300 \
  --statistics Sum

# Check CloudFront cache hit rate
aws cloudwatch get-metric-statistics \
  --namespace AWS/CloudFront \
  --metric-name CacheHitRate \
  --dimensions Name=DistributionId,Value=<distribution-id> \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 300 \
  --statistics Average
```

### Cognito Monitoring

```bash
# View sign-in attempts
aws cloudwatch get-metric-statistics \
  --namespace AWS/Cognito \
  --metric-name SignInSuccesses \
  --dimensions Name=UserPool,Value=<user-pool-id> \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 300 \
  --statistics Sum
```

## Monitoring and Management

### View All Stack Outputs

```bash
# Get all outputs for a specific stack
aws cloudformation describe-stacks \
  --stack-name semantic-layer-agentcore \
  --query 'Stacks[0].Outputs'

# Get environment configuration for agents
aws cloudformation describe-stacks \
  --stack-name semantic-layer-agentcore \
  --query 'Stacks[0].Outputs[?OutputKey==`AgentEnvironmentConfig`].OutputValue' \
  --output text | jq '.'
```

### Check Crawler Status

```bash
# List all crawlers
aws glue get-crawlers --query 'Crawlers[?Name contains `semantic-layer`].[Name, State, LastCrawl.Status]' --output table
```

### View Neptune Cluster Status

```bash
# Get Neptune cluster details
aws neptune describe-db-clusters \
  --db-cluster-identifier semantic-layer-neptune-cluster
```

### Monitor Knowledge Base Ingestion

```bash
# List all ingestion jobs
aws bedrock-agent list-ingestion-jobs \
  --knowledge-base-id $KB_ID \
  --data-source-id $DS_ID
```

## Cleanup

```bash
# Destroy all stacks
cdk destroy --all

# Or destroy in reverse dependency order
# Frontend & API (13-9)
cdk destroy semantic-layer-frontend
cdk destroy semantic-layer-lambda-api
cdk destroy semantic-layer-guardrails
cdk destroy semantic-layer-auth
cdk destroy semantic-layer-cloudfront-storage

# Backend infrastructure (8-1)
cdk destroy semantic-layer-agentcore
cdk destroy semantic-layer-athena
cdk destroy semantic-layer-bedrock-kb
cdk destroy semantic-layer-neptune
cdk destroy semantic-layer-glue-catalog
cdk destroy semantic-layer-data-lake
cdk destroy semantic-layer-dynamodb
cdk destroy semantic-layer-networking
```

**Important:** Some resources have `RemovalPolicy.DESTROY` and will be automatically deleted:

- ✅ S3 buckets (with auto-delete objects)
- ✅ DynamoDB tables
- ✅ CloudFront distribution
- ✅ Cognito User Pool
- ✅ Lambda functions
- ✅ ECR repositories

**Manually clean up if needed:**

```bash
# Delete DynamoDB table manually
aws dynamodb delete-table --table-name semantic-layer-insurance-data

# Empty and delete S3 buckets
aws s3 rb s3://semantic-layer-artifacts-ACCOUNT --force
aws s3 rb s3://semantic-layer-historical-data-ACCOUNT --force
```
