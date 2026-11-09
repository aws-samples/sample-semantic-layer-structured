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
import json
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


# ============================================================================
# Authorization (ENABLE_AUTHZ) — deny-by-default policy enforcement point
# ============================================================================
# Installed BEFORE the CORS middleware on purpose. Starlette's add_middleware
# prepends, so the last middleware added is the outermost one; registering authz
# first leaves CORS outside it, which is what puts CORS headers on the 401/403
# responses authz emits. Without that ordering a denial reaches the browser as an
# opaque network error instead of a readable 403.
#
# The middleware itself no-ops until ENABLE_AUTHZ is truthy, so it is safe to
# install unconditionally; that keeps one code path rather than two.
_NAMESPACES_TABLE = os.environ.get("NAMESPACES_TABLE", "")

# Lazily-built singletons for the two resolvers. Built on first use so importing
# main.py needs no namespace configuration, and cached so a resolver does not
# rebuild a boto3 resource on every request.
_namespace_service_instance = None
_authz_ontology_service_instance = None


def _namespace_service_singleton():
    """Return the process-wide NamespaceService, building it on first use.

    Returns:
        The shared NamespaceService.
    """
    global _namespace_service_instance
    if _namespace_service_instance is None:
        from services.namespace_service import NamespaceService

        _namespace_service_instance = NamespaceService()
    return _namespace_service_instance


def _authz_ontology_service_singleton():
    """Return the process-wide OntologyService used for namespace binding lookups.

    Returns:
        The shared OntologyService.
    """
    global _authz_ontology_service_instance
    if _authz_ontology_service_instance is None:
        from services.ontology_service import OntologyService

        _authz_ontology_service_instance = OntologyService()
    return _authz_ontology_service_instance


def _resolve_namespace_for_authz(identifier: str):
    """Resolve a namespace id-or-name to ``(canonical id, status)`` for the PDP.

    Args:
        identifier: The namespace segment taken from the request path.

    Returns:
        A ``(namespace id, status)`` tuple, or None when no such namespace
        exists. Exceptions propagate: the middleware turns a lookup failure into
        a 503 rather than a denial, so an outage is never reported as "forbidden".
    """
    found = _namespace_service_singleton().resolve(identifier=identifier)
    if found is None:
        return None
    return (found.id, found.status)


def _resolve_ontology_namespace_for_authz(ontology_id: str):
    """Resolve the namespace an ontology is bound to.

    Args:
        ontology_id: The ontology identifier from the request path.

    Returns:
        The owning namespace id, or None when the ontology does not exist or
        carries no binding. The middleware denies in both cases.
    """
    config = _authz_ontology_service_singleton().get_metadata_config(ontology_id)
    if not config:
        return None
    return str(config.get("namespaceId") or "") or None


if _NAMESPACES_TABLE:
    from authz.middleware import AuthzMiddleware

    logger.info("Installing authorization middleware (enforcement gated on ENABLE_AUTHZ)")
    app.add_middleware(
        AuthzMiddleware,
        ontology_namespace_resolver=_resolve_ontology_namespace_for_authz,
        namespace_resolver=_resolve_namespace_for_authz,
    )
else:
    logger.warning(
        "Skipping authorization middleware — NAMESPACES_TABLE not set, so no "
        "namespace registry exists to authorize against"
    )

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


# Governed rules (enableGovernedRules). VKG-only: rules are RDF in the
# <ontologyId>/rules Neptune named graph, so the Neptune gateway is a hard
# requirement — mounting without it would 500 on every call, which is worse
# than a clean 404. Everything the router calls is injected HERE; the modules
# under agents/shared/rule_*.py stay pure and unit-testable.
_RULES_ENABLED = os.environ.get("RULES_ENABLED", "").lower() == "true"
_NEPTUNE_GATEWAY_URL = os.environ.get("NEPTUNE_GATEWAY_URL", "")
if _RULES_ENABLED and _NEPTUNE_GATEWAY_URL:
    from routers.rules import build_router as build_rules_router
    from routers.rules import register_rule_error_handlers
    from services.rule_service import RuleService
    from services.neptune_service import NeptuneService
    from services.ontology_service import OntologyService
    from agents.shared.rule_publisher import PublishBlocked
    from agents.shared.rule_publisher import publish_rule as _publish_gate
    from agents.shared.rule_validator import validate_rule as _validate_rule

    # How long a publish smoke query may run before the gate gives up. The
    # query is a `LIMIT 1` probe, so anything slower than this means the
    # connector is cold rather than the SQL being wrong.
    _SMOKE_QUERY_MAX_WAIT_SECONDS = 60

    def _athena_coordinate_for_layer(*, config: dict, namespace: str) -> dict:
        """Resolve where a layer's tables actually live, for the smoke query.

        Read from the layer's stored configuration rather than from the Ontop
        translation. Ontop derives its `database` from the ontology's
        `vkg:mapsToTable` values, and a HYBRID layer's thin VKG stores those bare
        (`cards`, not `bird_card_games.cards`) — deliberately, because the hybrid
        query path matches on the bare name. So the derivation offers the
        alphabetically-first TABLE as the schema and Athena answers
        SCHEMA_NOT_FOUND, blocking every rule on every hybrid layer.

        `dataSources[0]` is the source of truth, NOT the top-level
        `catalogId`/`databaseName` — those are null on a real hybrid layer. Same
        shape the query agent reads in `_catalog_database_for_layer`.

        Args:
            config: The layer's metadata configuration, as returned by
                `OntologyService.get_metadata_config`. May be None for an
                unknown id.
            namespace: The layer (ontology) id — named in every error, because
                the reason is rendered verbatim in the admin UI.

        Returns:
            dict: ``{"database": str, "catalog": str}``, both non-blank.

        Raises:
            ValueError: If the config is missing, has no data sources, or a data
                source carries only half the pair. Never defaulted: a guessed
                catalog would smoke-test a coordinate the rule will never run
                against, which is the failure this resolver exists to remove.
        """
        if not config:
            raise ValueError(
                f"no stored configuration for layer '{namespace}' — it cannot "
                "be resolved to an Athena catalog and database"
            )
        data_sources = config.get("dataSources") or []
        if not data_sources:
            raise ValueError(
                f"layer '{namespace}' has no dataSources, so there is no "
                "Athena catalog or database to run the rule's smoke query in"
            )
        first = data_sources[0] or {}
        catalog = str(
            first.get("catalogId") or first.get("catalog_id") or ""
        ).strip()
        database = str(
            first.get("databaseName") or first.get("database_name") or ""
        ).strip()
        missing = [
            name
            for name, value in (("catalog", catalog), ("database", database))
            if not value
        ]
        if missing:
            raise ValueError(
                f"layer '{namespace}' data source names no Athena "
                f"{' or '.join(missing)}"
            )
        return {"database": database, "catalog": catalog}

    def _execute_athena_sql(
        *, sql: str, database: str, catalog: str
    ) -> dict:
        """Run one SQL statement on Athena and wait for a terminal state.

        Only the publish gate calls this, with the `LIMIT 1` smoke query Ontop
        produced from the compiled rule. Nothing is read out of the result —
        reaching SUCCEEDED is the whole signal.

        Args:
            sql: The statement to execute.
            database: Schema name within the catalog.
            catalog: Athena data catalog name.

        Returns:
            dict: ``{"query_execution_id": str, "state": "SUCCEEDED"}``.

        Raises:
            UnsafeSQLError: If the SQL firewall rejects the statement — the rule
                compiled to something that is not a read-only query.
            RuntimeError: If the query fails, is cancelled, or is still running
                when the budget expires. The Athena `StateChangeReason` is
                included verbatim — that text is what makes a blocked publish
                actionable for the admin.
        """
        import time as _time

        from agents.shared.sql_firewall import enforce_sql

        # SQL firewall. This statement is Ontop's translation of a rule an admin
        # authored, so the rule's shape reaches Athena — a read-only smoke query
        # is the contract, and this is what enforces it rather than assuming it.
        #
        # No grant profile is passed, so only the structural checks apply. That is
        # deliberate: reaching this code required the middleware to authorize
        # `publishRule` on the namespace, which is an administrative action, and
        # the table/column restrictions exist to constrain *data readers*. Passing
        # a publisher's analyst-scoped allowlist would block a legitimate publish.
        # A denial here raises and surfaces as a blocked publish with the reason.
        sql = enforce_sql(sql=sql)

        athena = boto3.client("athena")

        output_location = os.environ.get("ATHENA_OUTPUT_LOCATION")
        if not output_location:
            bucket = os.environ.get("ARTIFACTS_BUCKET", "")
            if bucket:
                output_location = f"s3://{bucket}/athena-results/"

        start_kwargs: dict = {
            "QueryString": sql,
            "QueryExecutionContext": {"Database": database, "Catalog": catalog},
            "WorkGroup": os.environ.get("ATHENA_WORKGROUP", "primary"),
        }
        if output_location:
            start_kwargs["ResultConfiguration"] = {"OutputLocation": output_location}

        query_execution_id = athena.start_query_execution(**start_kwargs)[
            "QueryExecutionId"
        ]
        logger.info(
            "Rule publish smoke query submitted: query_id=%s catalog=%s database=%s",
            query_execution_id, catalog, database,
        )

        waited = 0
        state = "QUEUED"
        status: dict = {}
        while waited < _SMOKE_QUERY_MAX_WAIT_SECONDS:
            status = athena.get_query_execution(
                QueryExecutionId=query_execution_id
            )
            state = status["QueryExecution"]["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            _time.sleep(2)  # nosemgrep: arbitrary-sleep - intentional Athena query status polling loop
            waited += 2

        if state != "SUCCEEDED":
            reason = (
                status.get("QueryExecution", {})
                .get("Status", {})
                .get("StateChangeReason", "Unknown")
            )
            # A still-RUNNING query is a cold connector, not bad SQL. Naming the
            # two apart keeps an admin from rewriting a rule that was fine.
            if state in ("QUEUED", "RUNNING"):
                raise RuntimeError(
                    f"smoke query was still {state} after "
                    f"{_SMOKE_QUERY_MAX_WAIT_SECONDS}s "
                    f"(query_id={query_execution_id}); the connector for "
                    f"catalog '{catalog}' is likely cold-starting"
                )
            raise RuntimeError(
                f"smoke query {state} (query_id={query_execution_id}): {reason}"
            )

        return {"query_execution_id": query_execution_id, "state": state}

    def _rule_service() -> RuleService:
        """Construct a RuleService bound to live Neptune + the Ontop gateway.

        The pure rule modules take their AWS collaborators as parameters; this
        factory is the single place those get bound to real clients.

        Returns:
            RuleService: A service wired to the live rules graph.
        """
        neptune = NeptuneService()

        def _ontology_graph_uri(*, namespace: str) -> str:
            """Ontology graph for a namespace — NOT the /rules sibling.

            Resolved from Neptune rather than built from a prefix: the real graph
            URI embeds a slug of the layer NAME plus a build timestamp, which is
            not derivable from the id. A constructed URI would name a graph that
            does not exist, and every validation level would fail closed for the
            wrong reason.

            Args:
                namespace: Ontology id.

            Returns:
                str: The newest ontology graph IRI for that id.
            """
            return neptune.resolve_ontology_graph_uri(id=namespace)

        def _validate(*, rule):
            """Run the four-level validator against the live ontology graph.

            Args:
                rule: The rule to validate.

            Returns:
                tuple: ``(list[ValidationReport], list[str])`` — reports and
                    the unresolved-input list.
            """
            return _validate_rule(
                rule=rule,
                ask=neptune.ask,
                select=neptune.select_in_graph,
                ontology_graph_uri=_ontology_graph_uri(namespace=rule.namespace),
            )

        def _publish(*, rule):
            """Run the publish gate, including Ontop translation + smoke SQL.

            Args:
                rule: The APPROVED rule to publish.

            Returns:
                Rule: A PUBLISHED copy of the rule.

            Raises:
                PublishBlocked: If the layer has no ontology graph, or its stored
                    configuration names no Athena coordinate. Both are reported
                    as a blocked publish (422 with the reason) rather than a 500,
                    because "build the layer first" is an admin action.
            """
            try:
                graph_uri = _ontology_graph_uri(namespace=rule.namespace)
            except ValueError as exc:
                raise PublishBlocked(str(exc)) from exc
            # Where the smoke query runs. Resolved from the layer's own config
            # because the Ontop translation cannot supply it for a hybrid layer
            # (see _athena_coordinate_for_layer).
            try:
                coordinate = _athena_coordinate_for_layer(
                    config=OntologyService().get_metadata_config(
                        id=rule.namespace
                    ),
                    namespace=rule.namespace,
                )
            except ValueError as exc:
                raise PublishBlocked(str(exc)) from exc
            # ontologyJson is what Ontop needs to reformulate; the same payload
            # the Tier 2 agent fetches in its Phase 1.
            ontology_json = neptune.get_ontology(id=rule.namespace)
            return _publish_gate(
                rule=rule,
                ask=neptune.ask,
                select=neptune.select_in_graph,
                translate=neptune.translate_sparql_to_sql,
                execute_sql=_execute_athena_sql,
                ontology_graph_uri=graph_uri,
                ontology_json=ontology_json,
                coordinate=coordinate,
            )

        def _read_artifact(key: str) -> dict:
            """Read a mined-rules JSON artefact from the artifacts bucket.

            The doc pipeline's ruleminer stage and this Lambda are pointed at the
            SAME bucket by app.ts (both take ``dataLakeStack.artifactsBucket``);
            if that ever diverges every import 404s with no other symptom.

            Args:
                key: S3 key, as reported by the pipeline's ``minedRulesKey``.

            Returns:
                dict: The parsed artefact body.

            Raises:
                KeyError: If the object does not exist. Surfaced as a 404 —
                    a stale key is a caller error, not a server fault.
            """
            bucket = os.environ["ARTIFACTS_BUCKET"]
            s3 = boto3.client("s3")
            try:
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            except s3.exceptions.NoSuchKey as exc:
                # boto3 raises NoSuchKey, which the router's `except KeyError`
                # would NOT catch — translate it here so a stale key 404s
                # instead of surfacing as a 500.
                raise KeyError(key) from exc
            return json.loads(body.decode("utf-8"))

        return RuleService(
            read_rules=neptune.read_rules,
            read_rules_partitioned=neptune.read_rules_partitioned,
            write_rule=neptune.write_rule,
            delete_rule=neptune.delete_rule,
            publish=_publish,
            validate_fn=_validate,
            read_artifact=_read_artifact,
        )

    logger.info("Mounting governed-rules router at /rules")
    app.include_router(build_rules_router(_rule_service), prefix="/rules")
    # Turns an unreadable stored rule into a 500 that NAMES the rule, on every
    # rules route at once.
    register_rule_error_handlers(app)
else:
    logger.info(
        "Skipping /rules router — RULES_ENABLED=%s, gateway configured=%s",
        _RULES_ENABLED,
        bool(_NEPTUNE_GATEWAY_URL),
    )

# Namespaces — the governance boundary rules, metrics and ontologies hang off.
# Mounted at the root rather than under a prefix because it is the top-level
# resource, and gated by AuthzMiddleware like everything else.
if _NAMESPACES_TABLE:
    from routers.namespaces import build_router as build_namespaces_router

    logger.info("Mounting namespaces router at /namespaces")
    app.include_router(build_namespaces_router(_namespace_service_singleton))
else:
    logger.info("Skipping /namespaces router — NAMESPACES_TABLE not set")

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
