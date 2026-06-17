# Semantic Layer CDK Infrastructure

AWS CDK infrastructure for deploying a complete semantic layer with insurance data, knowledge graphs, and AI agents.

## Architecture Overview

This CDK application deploys **up to 21 stacks** (17 always-on + 4 conditional on the
`enableRealtimeReplication`, `enableBatchReplication`, and `enableOntologyAgents` feature flags).
See the **root [`README.md`](../README.md)** for the authoritative stack list, dependency graph,
deployment modes, and capability matrix — this file documents CDK-specific operational detail.

### Always-on stacks

1. **Networking** - VPC with public/private/isolated subnets for Neptune
2. **DynamoDB** - 12 insurance tables + metadata / chat-sessions / feedback / metrics tables
3. **Glue Catalog** - DynamoDB + Iceberg databases, crawlers
4. **Data Lake** - S3 Tables (Iceberg) bucket, artifacts / Athena-results / KB / logging buckets, Lake Formation grants
5. **Bedrock Knowledge Base** - two KBs (S3 Vectors): ontology-patterns + semantic-rag
6. **Athena** - workgroup + DynamoDB connector + Lake Formation admin chain
7. **AgentCore Memory** - single SemanticStrategy resource (lessons-learned)
8. **Guardrails** - Bedrock Guardrails (content filters + PII)
9. **CloudFront Storage** - CloudFront distribution + S3 website bucket + OAC
10. **Auth** - Cognito User Pool / Identity Pool / OAuth 2.0 (SPA + MCP + M2M clients)
11. **AgentCore** - up to 5 AgentCore Runtimes + ECR + Neptune/Ontop Gateway
12. **AgentCore Eval** - online evaluation configs for the query runtimes
13. **Doc Pipeline** - staged Lambda ingestion (chunk → NER → embed → link → index)
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
# Build
npm run build

# Deploy all stacks (includes automated triggers)
cdk deploy --all --require-approval never
```

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

- **Up to 5 AgentCore Runtimes** - `ontology` + `query` (VKG), `metadata` + `metadata-query` (Semantic RAG), `query-suggestions`. VKG runtimes are conditional on `enableOntologyAgents`.
- **Neptune/Ontop Gateway** - HTTP gateway + Ontop SPARQL→SQL translate Lambda (Java 21) for VKG Phase 5
- **JWT-inbound runtimes** - accept Cognito JWT (no SigV4) so the MCP/chat gateways and browser can invoke them
- **Shared ECR repo + CodeBuild (ARM64)** per agent
- **IAM Roles** (least-privilege per agent) + Lake Formation grants for Iceberg access

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

**API Routes:**

- `GET /health` - Health check (no auth)
- `ANY /api/{proxy+}` - All API endpoints (via CloudFront)
- `ANY /ontology/{proxy+}` - Ontology operations (direct)
- `ANY /datasource/{proxy+}` - Data source operations (direct)
- `ANY /query/{proxy+}` - Query operations (direct)
- `ANY /neptune/{proxy+}` - Neptune operations (direct)
- `GET /status` - System status (direct)

**Architecture:**

```
Client → CloudFront → API Gateway → Lambda → AgentCore Runtime
                                           ↓
                                    Secrets Manager (CloudFront domain)
```

Lambda delegates all data operations to AgentCore Runtime agents, maintaining a clean separation of concerns.

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
