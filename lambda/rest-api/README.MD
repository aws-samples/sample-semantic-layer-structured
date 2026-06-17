# Semantic Layer - Lambda REST API

This directory contains the REST API backend for the Semantic Layer Knowledge Graph application, deployed as an AWS Lambda function with API Gateway.

## Architecture

This is a **serverless REST API** built with:

- **FastAPI** - Modern Python web framework for building APIs
- **Mangum** - ASGI adapter that wraps FastAPI for AWS Lambda compatibility
- **AWS Lambda** - Serverless compute (Docker container deployment)
- **API Gateway HTTP API** - RESTful endpoint routing with JWT authorization
- **Amazon Cognito** - User authentication and JWT token validation

The application is containerized using Docker and deployed to AWS Lambda (up to 10GB container support).

### Key Components

**Core Infrastructure:**

- **main.py** - Lambda handler with Mangum adapter, mounts all sub-applications
- **auth.py** - JWT token validation using AWS Cognito JWKS
- **Dockerfile** - Container definition for Lambda deployment

**Mounted sub-applications** (mounted in `main.py`):

- **ontology_api.py** (`/ontology`) - ontology config, build, versioning, upload
- **datasource_api.py** (`/datasource`) - Glue Data Catalog browsing + metadata extraction
- **query_api.py** (`/query`) - suggested questions, chat-session list/rehydrate/archive, per-turn feedback (natural-language querying itself is served by the streaming **Chat Gateway**, not an async REST submit/status/result path)
- **neptune_api.py** (`/neptune`) - Neptune graph summary / stats / classes / properties
- **metadata_api.py** (`/metadata`) - Semantic-RAG enrichment, revision, single-table KB metadata (mounted only when `enableSemanticRag`)
- **lessons_api.py** (`/lessons`) - AgentCore Memory lessons (admin Lessons tab)
- **feedback_api.py** (`/feedback`) - 👍/👎 turn ratings (admin Feedback tab)
- **documents_api.py** (`/documents`) - supplementary docs → doc-pipeline → KB
- **groundtruth_api.py** (`/groundtruth`) - per-layer evaluation dataset
- **evaluations_api.py** (`/evaluations`) - AgentCore evaluation runs
- **routers/metrics.py** (`/metrics`) - governed-metrics CRUD + lifecycle (Tier 1); mounted only when `METRICS_TABLE` is set

**Services** (`services/`):

- **ontology_service.py** - ontology configuration, versioning, and generation orchestration
- **glue_service.py** - AWS Glue Data Catalog integration
- **agentcore_service.py** - invokes the AgentCore query/enrichment runtimes
- **neptune_service.py** - Amazon Neptune SPARQL query and graph operations
- **guardrail_service.py** - Bedrock Guardrails INPUT/OUTPUT screening + PII redaction
- **chat_session_service.py** / **feedback_service.py** / **agentcore_memory_service.py** - chat transcripts, feedback, and lessons stores
- **metadata_service.py** / **metric_service.py** - Semantic-RAG enrichment + governed metrics
- **document_service.py** / **groundtruth_service.py** / **evaluation_service.py** - doc pipeline, ground truth, and evaluation runs
- **identity_service.py** / **obo_middleware.py** - gated on-behalf-of (OBO) identity exchange (off by default)

## Environment Variables

The following environment variables are configured by the CDK stack:

| Variable                       | Description                                                                 | Set By |
| ------------------------------ | --------------------------------------------------------------------------- | ------ |
| AWS_REGION                     | AWS region (auto-provided by Lambda runtime)                                | Lambda |
| STACK_NAME                     | CDK stack name prefix                                                       | CDK    |
| STACK_ENVIRONMENT              | Environment (dev/staging/prod)                                              | CDK    |
| DATA_BUCKET                    | S3 bucket name for file storage                                             | CDK    |
| COGNITO_USER_POOL_ID           | Cognito User Pool ID                                                        | CDK    |
| COGNITO_APP_CLIENT_ID          | Cognito App Client ID                                                       | CDK    |
| ATHENA_DATABASE                | Athena database name                                                        | CDK    |
| ATHENA_WORKGROUP               | Athena workgroup name                                                       | CDK    |
| NEPTUNE_CONNECTION_SECRET_NAME | Secrets Manager secret name for Neptune connection (optional)               | CDK    |
| NEPTUNE_ENDPOINT               | Neptune cluster endpoint (optional fallback when the secret is unavailable) | CDK    |
| ONTOLOGY_GENERATION_MODEL_ID   | Bedrock model for ontology generation                                       | CDK    |
| TEXT_TO_SQL_MODEL_ID           | Bedrock model for text-to-SQL                                               | CDK    |

**Neptune Configuration:**

- The Lambda function automatically retrieves Neptune connection details from AWS Secrets Manager when `NEPTUNE_CONNECTION_SECRET_NAME` is provided
- The secret contains: endpoint, readerEndpoint, port, sparqlEndpoint, loaderEndpoint, and loaderRoleArn
- This approach avoids cyclic dependencies in CDK deployment
- `NEPTUNE_ENDPOINT` is read only as a fallback when the secret is unavailable

## API Endpoints

All endpoints are served through API Gateway at `https://<api-gateway-url>/*` with JWT authentication (except `/health`).

### Health & Status (No Auth)

- `GET /health` - Health check for monitoring/ALB
- `GET /status` - System status and configuration

### Ontology API (`/ontology/*`)

**Configuration:**

- `POST /ontology/config` - Create/update ontology configuration
- `GET /ontology/config/{ontology_id}` - Get ontology configuration
- `GET /ontology/list` - List all ontologies
- `DELETE /ontology/config/{ontology_id}` - Delete ontology

**Building:**

- `POST /ontology/build/{ontology_id}` - Build ontology from data sources
- `GET /ontology/build-status/{ontology_id}` - Get build status

**Versioning:**

- `GET /ontology/versions/{ontology_id}` - List ontology version history
- `GET /ontology/content/{ontology_id}/{version_id}` - Retrieve version content
- `POST /ontology/revise/{ontology_id}/{version_id}` - Start a versioned revision

**Upload:**

- `POST /ontology/upload` - Upload reference document (OWL, RDF, Turtle, Markdown)

### Data Source API (`/datasource/*`)

**Glue Catalog:**

- `GET /datasource/glue/databases` - List all Glue databases
- `GET /datasource/glue/tables/{database_name}` - List tables in database
- `GET /datasource/glue/metadata/{database_name}/{table_name}` - Get table metadata

**Metadata:**

- `POST /datasource/extract-metadata` - Extract metadata for ontology generation

### Query API (`/query/*`)

> Natural-language querying is served by the streaming **Chat AgentCore Gateway** (SSE),
> not by this REST API. There is no async `submit`/`status`/`result` surface here.

- `GET /query/suggestions/{ontology_id}` - AI-generated suggested questions (synchronous)
- `GET /query/sessions` · `GET /query/sessions/{session_id}` · `DELETE /query/sessions/{session_id}` - chat session list / transcript rehydrate / archive
- `POST /query/feedback` - record a 👍/👎 turn rating (+ optional comment)

### Neptune API (`/neptune/*`)

- `GET /neptune/graph/summary/{ontology_id}` - Get graph summary
- `GET /neptune/graph/stats/{ontology_id}` - Get detailed statistics
- `GET /neptune/graph/classes/{ontology_id}` - List ontology classes
- `GET /neptune/graph/properties/{ontology_id}` - List ontology properties

### Metadata API (`/metadata/*`, Semantic RAG)

- `POST /metadata/enrich` - trigger the Metadata Agent (accepts optional `annotations` for annotation-only mode)
- `GET /metadata/enrich/status/{job_id}` - poll enrichment progress
- `POST /metadata/revise/{id}/{version_id}` - start a versioned metadata revision
- `GET /metadata/table/{database_name}/{table_name}` - get AI-enriched KB metadata for a single table

### Platform APIs

- `/lessons/{id}` - AgentCore Memory lessons (list/delete) for the admin Lessons tab
- `/feedback/{id}` - feedback records (list/delete) for the admin Feedback tab
- `/documents/{id}` - supplementary docs upload/list/delete → doc-pipeline → KB
- `/groundtruth/{id}` - per-layer ground-truth dataset upload/get/delete
- `/evaluations/{id}` - AgentCore evaluation runs (list/get/trigger/delete)
- `/metrics/namespaces/{ns}/metrics` - governed-metrics CRUD + `:publish` lifecycle (Tier 1; mounted only when `METRICS_TABLE` is set)

## Response Format

All API responses follow this format:

```json
{
  "success": true,
  "data": { ... },
  "message": "Optional message",
  "error": "Error message if success=false"
}
```

## Authentication

All endpoints (except `/health` and `/status`) require a valid JWT token from Amazon Cognito:

```bash
Authorization: Bearer <jwt-token>
```

## Building and Deploying

### Build Docker Container

```bash
# Build for Lambda (ARM64 architecture)
docker build --platform=linux/arm64 -t semantic-layer-lambda:latest .

# Tag for ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-east-1.amazonaws.com
docker tag semantic-layer-lambda:latest <account-id>.dkr.ecr.us-east-1.amazonaws.com/<repo-name>:latest

# Push to ECR
docker push <account-id>.dkr.ecr.us-east-1.amazonaws.com/<repo-name>:latest
```

### Deploy via CDK

```bash
# From repository root
npm run cdk deploy */*/backend/lambda-rest-api
```

## Local Testing

```bash
# Set up environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt


uvicorn main:app --reload --port 8000
```
