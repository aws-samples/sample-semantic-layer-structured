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

**API Applications (4):**
- **ontology_api.py** - 7 endpoints for ontology management
- **datasource_api.py** - 6 endpoints for data source operations (AWS Glue)
- **query_api.py** - 5 endpoints for natural language query processing
- **neptune_api.py** - 6 endpoints for knowledge graph operations (Amazon Neptune)

**Services (4):**
- **services/ontology_service.py** - Ontology configuration and generation using Amazon Bedrock
- **services/glue_service.py** - AWS Glue Data Catalog integration
- **services/query_service.py** - Natural language to SQL conversion and Athena execution
- **services/neptune_service.py** - Amazon Neptune SPARQL query and graph operations

## Environment Variables

The following environment variables are configured by the CDK stack:

| Variable | Description | Set By |
|----------|-------------|--------|
| AWS_REGION | AWS region (auto-provided by Lambda runtime) | Lambda |
| STACK_NAME | CDK stack name prefix | CDK |
| STACK_ENVIRONMENT | Environment (dev/staging/prod) | CDK |
| DATA_BUCKET | S3 bucket name for file storage | CDK |
| COGNITO_USER_POOL_ID | Cognito User Pool ID | CDK |
| COGNITO_APP_CLIENT_ID | Cognito App Client ID | CDK |
| ATHENA_DATABASE | Athena database name | CDK |
| ATHENA_WORKGROUP | Athena workgroup name | CDK |
| NEPTUNE_CONNECTION_SECRET_NAME | Secrets Manager secret name for Neptune connection (optional) | CDK |
| NEPTUNE_ENDPOINT | Neptune cluster endpoint (optional, legacy - use secret instead) | CDK |
| NEPTUNE_LOADER_ROLE_ARN | IAM role for Neptune loader (optional, legacy - use secret instead) | CDK |
| ONTOLOGY_GENERATION_MODEL_ID | Bedrock model for ontology generation | CDK |
| TEXT_TO_SQL_MODEL_ID | Bedrock model for text-to-SQL | CDK |

**Neptune Configuration:**
- The Lambda function automatically retrieves Neptune connection details from AWS Secrets Manager when `NEPTUNE_CONNECTION_SECRET_NAME` is provided
- The secret contains: endpoint, readerEndpoint, port, sparqlEndpoint, loaderEndpoint, and loaderRoleArn
- This approach avoids cyclic dependencies in CDK deployment
- Legacy environment variables (`NEPTUNE_ENDPOINT`, `NEPTUNE_LOADER_ROLE_ARN`) are still supported for backward compatibility

## API Endpoints

All endpoints are served through API Gateway at `https://<api-gateway-url>/*` with JWT authentication (except `/health`).

### Health & Status (No Auth)
- `GET /health` - Health check for monitoring/ALB
- `GET /status` - System status and configuration

### Ontology API (`/ontology/*`) - 7 endpoints

**Configuration:**
- `POST /ontology/config` - Create/update ontology configuration
- `GET /ontology/config/{ontology_id}` - Get ontology configuration
- `GET /ontology/list` - List all ontologies
- `DELETE /ontology/config/{ontology_id}` - Delete ontology

**Building:**
- `POST /ontology/build/{ontology_id}` - Build ontology from data sources
- `GET /ontology/build-status/{ontology_id}` - Get build status

**Upload:**
- `POST /ontology/upload` - Upload ontology file (OWL, RDF, Turtle, Markdown)

### Data Source API (`/datasource/*`) - 6 endpoints

**Glue Catalog:**
- `GET /datasource/glue/databases` - List all Glue databases
- `GET /datasource/glue/tables/{database_name}` - List tables in database
- `GET /datasource/glue/metadata/{database_name}/{table_name}` - Get table metadata

**Metadata:**
- `POST /datasource/extract-metadata` - Extract metadata for ontology generation

**Crawlers:**
- `POST /datasource/glue/crawler/{crawler_name}/start` - Start Glue crawler
- `GET /datasource/glue/crawler/{crawler_name}/status` - Get crawler status

### Query API (`/query/*`) - 5 endpoints

**Query Execution:**
- `POST /query/submit` - Submit natural language query
- `GET /query/result/{query_id}` - Get query results
- `GET /query/status/{query_id}` - Get query status

**History:**
- `GET /query/history?limit=50` - List query history
- `DELETE /query/{query_id}` - Delete query from history

### Neptune API (`/neptune/*`) - 6 endpoints

**SPARQL:**
- `POST /neptune/sparql` - Execute SPARQL query

**Graph Info:**
- `GET /neptune/graph/summary/{ontology_id}` - Get graph summary
- `GET /neptune/graph/stats/{ontology_id}` - Get detailed statistics

**Loading:**
- `POST /neptune/load` - Load ontology from S3 to Neptune
- `GET /neptune/load/status/{load_id}` - Get load job status

**Management:**
- `DELETE /neptune/graph/{ontology_id}` - Clear graph data

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
