"""
Lambda handler for REST API (FastAPI with Mangum adapter)

This module creates the main FastAPI application for the Semantic Layer REST API,
mounts all sub-applications (ontology, datasource, query, neptune), and wraps everything
with the Mangum adapter for AWS Lambda compatibility.

Key features:
- Ontology management and generation using Amazon Bedrock
- Data source integration with AWS Glue
- Natural language query processing with Athena
- Knowledge graph operations with Amazon Neptune
- Uses Mangum adapter for Lambda compatibility
- Environment variables automatically provided by Lambda
- Single entry point (no separate server process)
"""

# Load environment variables FIRST before any other imports that depend on them
from dotenv import load_dotenv

load_dotenv()

import os
import sys
import logging
import boto3
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

# Capability flag — when false, the /metadata sub-app is not imported or mounted.
# Import is gated because metadata_api transitively depends on the semantic-rag KB,
# which is not provisioned when this flag is off.
ENABLE_SEMANTIC_RAG = os.environ.get("ENABLE_SEMANTIC_RAG", "false").lower() == "true"

# Import the sub-applications
from ontology_api import app as ontology_api_app
from datasource_api import app as datasource_api_app
from query_api import app as query_api_app
from neptune_api import app as neptune_api_app
from lessons_api import app as lessons_api_app
from feedback_api import app as feedback_api_app
from documents_api import app as documents_api_app
from groundtruth_api import app as groundtruth_api_app
from evaluations_api import app as evaluations_api_app
from monitoring_api import app as monitoring_api_app

if ENABLE_SEMANTIC_RAG:
    from metadata_api import app as metadata_api_app
else:
    metadata_api_app = None

# Initialize SSM client for parameter retrieval
ssm_client = None
_agentcore_runtime_arn_cache = None  # Cache for runtime ARN

# Configure logging for Lambda
# IMPORTANT: Configure root logger to ensure all child loggers (interviewer_api, candidate_api, etc.)
# properly write to CloudWatch. Using basicConfig alone is insufficient for Lambda.
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Remove any existing handlers to avoid duplicates
if root_logger.handlers:
    for handler in root_logger.handlers:
        root_logger.removeHandler(handler)

# Add stdout handler with consistent formatting
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
root_logger.addHandler(handler)

# Create module-specific logger (inherits from root)
logger = logging.getLogger("lambda_rest_api")
logger.setLevel(logging.INFO)

# Log environment info on cold start
logger.info("=" * 60)
logger.info(f"COGNITO_USER_POOL_ID: {os.getenv('COGNITO_USER_POOL_ID')}")
logger.info(f"COGNITO_APP_CLIENT_ID: {os.getenv('COGNITO_APP_CLIENT_ID')}")
logger.info(f"AWS_REGION: {os.getenv('AWS_REGION')}")
logger.info("=" * 60)

# Create main FastAPI application
app = FastAPI(
    title="Semantic Layer REST API",
    description="REST API for Semantic Layer",
    version="1.0.0",
)

# Add CORS middleware - Configured with specific origins for security
# Get CloudFront domain from Secrets Manager for CORS configuration
allowed_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]  # Default for local dev

cloudfront_secret_name = os.environ.get("CLOUDFRONT_DOMAIN_SECRET_NAME")
if cloudfront_secret_name:
    try:
        secrets_client = boto3.client("secretsmanager")
        response = secrets_client.get_secret_value(SecretId=cloudfront_secret_name)
        cloudfront_domain = response.get("SecretString", "").strip()
        if cloudfront_domain:
            allowed_origins.append(f"https://{cloudfront_domain}")
            logger.info(f"Added CloudFront domain to CORS: https://{cloudfront_domain}")
        else:
            logger.warning("CloudFront domain secret is empty")
    except Exception as e:
        logger.warning(f"Failed to retrieve CloudFront domain from Secrets Manager: {e}")
else:
    logger.warning("CLOUDFRONT_DOMAIN_SECRET_NAME not set. Using localhost only for CORS.")

logger.info(f"CORS allowed origins: {allowed_origins}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,  # Specific origins only - no wildcards
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# Health check endpoint (no authentication required)
@app.get("/health")
async def health_check():
    """Health check endpoint for ALB/monitoring"""
    return {
        "status": "healthy",
        "service": "semantic-layer-rest-api",
        "version": "1.0.0",
        "environment": os.environ.get("STACK_ENVIRONMENT", "dev"),
        "runtime": "lambda",
    }


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Semantic Layer REST API",
        "version": "1.0.0",
        "runtime": "lambda",
        "endpoints": {
            "health": "/health",
            "status": "/status",
            "ontology": "/ontology/*",
            "datasource": "/datasource/*",
            "query": "/query/*",
            "neptune": "/neptune/*",
            "metadata": "/metadata/*",
        },
    }


# System status endpoint
@app.get("/status")
async def get_status():
    """Get system status and configuration"""
    # Check Neptune configuration
    neptune_configured = (
        os.getenv('NEPTUNE_CONNECTION_SECRET_NAME') is not None 
    )

    return {
        "status": "operational",
        "services": {
            "ontology": "available",
            "datasource": "available",
            "query": "available",
            "neptune": "available" if neptune_configured else "not_configured"
        },
        "environment": os.environ.get("STACK_ENVIRONMENT", "dev"),
        "region": os.environ.get("AWS_REGION", "unknown")
    }



# This API focuses on RESTful operations for ontology management, data sources,
# queries, and Neptune graph operations.


# Mount sub-applications
# These handle all the business logic for semantic layer operations
logger.info("Mounting ontology API at /ontology")
app.mount("/ontology", ontology_api_app)

logger.info("Mounting data source API at /datasource")
app.mount("/datasource", datasource_api_app)

logger.info("Mounting query API at /query")
app.mount("/query", query_api_app)

logger.info("Mounting neptune API at /neptune")
app.mount("/neptune", neptune_api_app)

logger.info("Mounting lessons API at /lessons")
app.mount("/lessons", lessons_api_app)

logger.info("Mounting feedback API at /feedback")
app.mount("/feedback", feedback_api_app)

logger.info("Mounting documents API at /documents")
app.mount("/documents", documents_api_app)

logger.info("Mounting groundtruth API at /groundtruth")
app.mount("/groundtruth", groundtruth_api_app)

logger.info("Mounting evaluations API at /evaluations")
app.mount("/evaluations", evaluations_api_app)

logger.info("Mounting monitoring API at /monitoring")
app.mount("/monitoring", monitoring_api_app)

if ENABLE_SEMANTIC_RAG and metadata_api_app is not None:
    logger.info("Mounting metadata API at /metadata")
    app.mount("/metadata", metadata_api_app)
else:
    logger.info("Skipping /metadata mount — ENABLE_SEMANTIC_RAG is false")

# Tier 1 governed-metric authoring (progressive disclosure). The KNN index
# is in-memory and rebuilt on agent runtime cold-start from DDB, so the REST
# API only needs the metrics table to be wired — no OpenSearch dependency.
_METRICS_TABLE = os.environ.get("METRICS_TABLE", "")
if _METRICS_TABLE:
    from routers.metrics import build_router as build_metrics_router
    from services.metric_service import MetricService
    from agents.shared import embedding

    def _metric_service() -> MetricService:
        """Construct a MetricService bound to live DDB. Embeddings persist
        on the metric row; agent runtimes hydrate their in-memory KNN
        index from DDB on cold start, so no cross-process KNN mirror runs
        from this Lambda."""
        table = boto3.resource("dynamodb").Table(_METRICS_TABLE)
        return MetricService(
            ddb_table=table,
            embed_fn=embedding.embed_text,
        )

    logger.info("Mounting governed-metrics router at /metrics")
    app.include_router(build_metrics_router(_metric_service), prefix="/metrics")
else:
    logger.info("Skipping /metrics router — METRICS_TABLE not set")

logger.info("FastAPI app initialized successfully")


# Lambda handler using Mangum for HTTP events
# This wraps the FastAPI app to make it compatible with AWS Lambda
# lifespan="off" disables FastAPI's lifespan events for Lambda compatibility
mangum_handler = Mangum(app, lifespan="off")

logger.info("Mangum handler created - ready to handle Lambda events")


# ============================================================================
# Lambda Handler - Direct HTTP via Mangum
# ============================================================================

def handler(event, context):
    """
    Main Lambda handler.

    All API Gateway requests are handled via Mangum/FastAPI. Chat is served
    exclusively through the streaming AG-UI path (the chat AgentCore Gateway),
    not this REST handler.
    """
    return mangum_handler(event, context)
