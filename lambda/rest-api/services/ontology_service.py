"""
metadata Service for Managing Knowledge Graph Ontologies

This service provides methods for:
- Creating and managing metadata configurations
- Generating ontologies from data sources using Amazon Bedrock
- Storing and retrieving metadata files from S3
- Building ontologies from metadata
"""

import logging
import os
import re
import uuid
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from decimal import Decimal
import io
import boto3
from botocore.exceptions import ClientError
from services.agentcore_service import AgentCoreService
from services.neptune_service import NeptuneService
from services.delta_planner_service import DeltaPlannerService
from services.glue_service import GlueService

logger = logging.getLogger(__name__)

DEFAULT_NAMESPACE_ID_ENV = 'DEFAULT_NAMESPACE_ID'


def default_namespace_id() -> str:
    """Return the namespace an ontology binds to when the caller names none.

    Set by the authz bootstrap to the seeded default namespace's id, so existing
    single-tenant deployments keep working. Read per call rather than at import
    so a configuration update takes effect without a new container.

    Returns:
        The default namespace id, or ``''`` when namespaces are not provisioned —
        in which case ontology configs carry no binding and namespace-scoped
        authorization for those routes denies rather than guessing.
    """
    return os.environ.get(DEFAULT_NAMESPACE_ID_ENV, '')


def convert_decimals(obj):
    """
    Convert DynamoDB Decimal types to int or float for JSON serialization

    Args:
        obj: Object potentially containing Decimal types

    Returns:
        Object with Decimals converted to int/float
    """
    if isinstance(obj, list):
        return [convert_decimals(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, Decimal):
        # Convert to int if it's a whole number, otherwise float
        if obj % 1 == 0:
            return int(obj)
        else:
            return float(obj)
    else:
        return obj


def _version_num(version_str: str) -> int:
    """
    Parse the integer from a version string like 'v1', 'v10'.

    Args:
        version_str: Version string to parse (e.g., 'v1', 'v10', 'v100')

    Returns:
        Integer version number, or 0 if no number found
    """
    m = re.search(r'\d+', version_str or 'v0')
    return int(m.group()) if m else 0


_SUPPORTED_TEXT_EXTENSIONS = {'.md', '.markdown', '.txt', '.pdf', '.docx'}


def extract_text_from_file(file_content: bytes, filename: str) -> str:
    """
    Extract plain UTF-8 text from an uploaded reference document.

    Supports: .md .markdown .txt  (UTF-8 decode)
              .pdf                 (pypdf)
              .docx                (python-docx)

    Args:
        file_content: Raw bytes of the uploaded file.
        filename: Original filename — extension determines parser.

    Returns:
        Extracted plain text.

    Raises:
        ValueError: If the extension is not in the supported set.
    """
    ext = os.path.splitext(filename.lower())[1]

    if ext in ('.md', '.markdown', '.txt'):
        return file_content.decode('utf-8', errors='replace').strip()

    if ext == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(file_content))
        pages = [page.extract_text() or '' for page in reader.pages]
        return '\n'.join(pages).strip()

    if ext == '.docx':
        from docx import Document
        doc = Document(io.BytesIO(file_content))
        paragraphs = [p.text for p in doc.paragraphs]
        return '\n'.join(paragraphs).strip()

    raise ValueError(
        f"Unsupported file type '{ext}'. "
        f"Supported: {', '.join(sorted(_SUPPORTED_TEXT_EXTENSIONS))}"
    )


class OntologyService:
    """Service class for metadata management operations"""

    def __init__(self):
        """Initialize S3, DynamoDB, Bedrock, and AgentCore clients"""
        self.s3_client = boto3.client('s3')
        self.dynamodb = boto3.resource('dynamodb')
        self.bedrock_runtime = boto3.client('bedrock-runtime')
        self.agentcore_service = AgentCoreService()

        # Use ARTIFACTS_BUCKET for storing files
        self.artifacts_bucket = os.getenv('ARTIFACTS_BUCKET')
        if not self.artifacts_bucket:
            logger.warning("ARTIFACTS_BUCKET environment variable not set")

        # Use DynamoDB for metadata storage
        self.table_name = os.getenv('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
        self.table = self.dynamodb.Table(self.table_name)

        logger.info(f"OntologyService initialized with bucket: {self.artifacts_bucket}, table: {self.table_name}")

    def create_metadata_config(self, metadata_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create or update an metadata configuration in DynamoDB

        Args:
            metadata_data: Dictionary containing metadata configuration
                - name: metadata namespace (lowercase, no spaces)
                - dataSources: List of selected data sources
                - configuration: Additional configuration options
                - dataSourcesDescription: Description of data sources (from frontend)
                - useCasesDescription: Use cases description (from frontend)
                - selectedDataSources: Selected tables (from frontend)
                - status: Status (from frontend)
                - createdBy: Creator info (from frontend)

        Returns:
            Dictionary with metadata ID and configuration
        """
        try:
            id = metadata_data.get('id') or str(uuid.uuid4())
            timestamp = datetime.now(timezone.utc).isoformat()

            # Log incoming data for debugging
            logger.info(f"Creating/updating metadata config for: {id}")
            logger.info(f"Received dataSources field: {metadata_data.get('dataSources')}")
            logger.info(f"Received selectedDataSources field: {metadata_data.get('selectedDataSources')}")

            # Retrieve existing config if updating
            existing_config = None
            if metadata_data.get('id'):
                existing_config = self.get_metadata_config(id)
                if existing_config:
                    logger.info(f"Existing config found with {len(existing_config.get('dataSources', []))} dataSources")

            # Determine dataSources value explicitly
            # Priority: dataSources > selectedDataSources > existing > empty list
            data_sources = None
            if metadata_data.get('dataSources') is not None:
                data_sources = metadata_data.get('dataSources')
            elif metadata_data.get('selectedDataSources') is not None:
                data_sources = metadata_data.get('selectedDataSources')
            elif existing_config and existing_config.get('dataSources') is not None:
                data_sources = existing_config.get('dataSources')
            else:
                data_sources = []

            # Build config merging existing and new data.
            # Preserve the existing active version on updates; default to 'v1' for new records.
            active_version = existing_config.get('version', 'v1') if existing_config else 'v1'
            config = {
                'id': id,
                'version': active_version,
                'type': metadata_data.get('type') or (existing_config.get('type') if existing_config else 'VKG'),
                'name': metadata_data.get('name') or (existing_config.get('name') if existing_config else 'untitled'),
                'dataSources': data_sources,
                'configuration': metadata_data.get('configuration') or (existing_config.get('configuration') if existing_config else {}),
                'status': metadata_data.get('status') or (existing_config.get('status') if existing_config else 'draft'),
                'createdAt': existing_config.get('createdAt') if existing_config else timestamp,
                'updatedAt': timestamp,
                # databaseName is intentionally NOT stored at the top level.
                # Database names live inside each dataSources entry only.
            }

            logger.info(f"Storing dataSources in config: {len(data_sources) if data_sources else 0} items")

            # Add optional frontend-specific fields if provided, preserving existing values
            if metadata_data.get('dataSourcesDescription'):
                config['dataSourcesDescription'] = metadata_data['dataSourcesDescription']
            elif existing_config and existing_config.get('dataSourcesDescription'):
                config['dataSourcesDescription'] = existing_config['dataSourcesDescription']

            if metadata_data.get('useCasesDescription'):
                config['useCasesDescription'] = metadata_data['useCasesDescription']
            elif existing_config and existing_config.get('useCasesDescription'):
                config['useCasesDescription'] = existing_config['useCasesDescription']

            # Note: selectedDataSources is already mapped to dataSources above
            # so we don't store it separately to avoid redundancy

            # Handle multiple uploaded documents
            if metadata_data.get('uploadedDocuments'):
                config['uploadedDocuments'] = metadata_data['uploadedDocuments']
            elif existing_config and existing_config.get('uploadedDocuments'):
                config['uploadedDocuments'] = existing_config['uploadedDocuments']

            if metadata_data.get('createdBy'):
                config['createdBy'] = metadata_data['createdBy']
            elif existing_config and existing_config.get('createdBy'):
                config['createdBy'] = existing_config['createdBy']

            # Namespace binding. Every ontology belongs to exactly one namespace,
            # and that binding is what lets the authorization middleware turn
            # "/ontology/config/{id}" into a namespace-scoped decision — a route
            # whose ontology carries no namespaceId is denied outright rather
            # than evaluated against the platform. Preserved on update: an
            # ontology cannot be moved between namespaces by re-saving its
            # config, which would silently transfer it out from under the grants
            # that govern it.
            if existing_config and existing_config.get('namespaceId'):
                config['namespaceId'] = existing_config['namespaceId']
            elif metadata_data.get('namespaceId'):
                config['namespaceId'] = metadata_data['namespaceId']
            elif default_namespace_id():
                config['namespaceId'] = default_namespace_id()

            # Store configuration in DynamoDB
            self.table.put_item(Item=config)

            logger.info(f"Successfully saved metadata configuration to DynamoDB")
            logger.info(f"Final config - id: {id}, name: {config.get('name')}, dataSources count: {len(config.get('dataSources', []))}, uploadedDocuments count: {len(config.get('uploadedDocuments', []))}, status: {config.get('status')}")
            return config

        except Exception as e:
            logger.warning(f"Error creating metadata config: {e}", exc_info=True)
            raise

    def get_metadata_config(self, id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve the active (highest-version) metadata configuration from DynamoDB.

        The highest version record is the active record — v1 becomes inactive
        once v2 is created, v2 becomes inactive once v3 is created, etc.

        Args:
            id: Unique identifier for the metadata

        Returns:
            Dictionary containing metadata configuration or None if not found
        """
        try:
            from boto3.dynamodb.conditions import Key as DKey
            resp = self.table.query(KeyConditionExpression=DKey('id').eq(id))
            items = resp.get('Items', [])
            if not items:
                logger.warning(f"metadata config not found in DynamoDB: {id}")
                return None
            item = max(items, key=lambda i: _version_num(i.get('version', 'v0')))
            logger.info(f"Retrieved metadata config from DynamoDB: {id} (version={item.get('version')})")
            return convert_decimals(item)

        except ClientError as e:
            logger.warning(f"Error retrieving metadata config from DynamoDB: {e}", exc_info=True)
            raise

    def list_ontologies(self) -> List[Dict[str, Any]]:
        """
        List all metadata configurations from DynamoDB

        Returns:
            List of metadata configuration summaries including latestVersion
        """
        try:
            # Scan all items to determine latest version per ontology
            response = self.table.scan()
            all_items = response.get('Items', [])

            while 'LastEvaluatedKey' in response:
                response = self.table.scan(
                    ExclusiveStartKey=response['LastEvaluatedKey']
                )
                all_items.extend(response.get('Items', []))

            # Group items by id and find v1 config + latest version
            from collections import defaultdict
            groups = defaultdict(list)
            for item in all_items:
                item = convert_decimals(item)
                groups[item.get('id')].append(item)

            ontologies = []
            for ontology_id, versions in groups.items():
                # Use the highest-version record as the active config for display.
                config = max(versions, key=lambda v: _version_num(v.get('version', 'v0')))
                latest_version = config.get('version', 'v1')

                ontologies.append({
                    'id': config.get('id'),
                    'name': config.get('name'),
                    'type': config.get('type', 'VKG'),
                    'dataSourcesDescription': config.get('dataSourcesDescription', ''),
                    'useCasesDescription': config.get('useCasesDescription', ''),
                    'status': config.get('status', 'unknown'),
                    'updatedAt': config.get('updatedAt', ''),
                    'dataSourceCount': len(config.get('dataSources', [])) if config.get('dataSources') else 0,
                    'latestVersion': latest_version,
                })

            logger.info(f"Listed {len(ontologies)} ontologies from DynamoDB")
            return ontologies

        except Exception as e:
            logger.warning(f"Error listing ontologies from DynamoDB: {e}", exc_info=True)
            raise

    def compute_delta_plan(self, id: str) -> Dict[str, Any]:
        """Compute (do not run) the delta plan for a VKG layer.

        Args:
            id: The layer/ontology id.

        Returns:
            The DeltaPlan dict (see DeltaPlannerService.compute_plan).
        """
        return DeltaPlannerService(
            metadata_table=self.table, glue_service=GlueService(),
            artifacts_bucket=os.environ.get("ARTIFACTS_BUCKET", ""),
            region=os.environ.get("AWS_REGION", "us-east-1"),
        ).compute_plan(id, layer_type="VKG")

    def start_build_metadata_async(
        self, id: str, target_tables: Optional[List[str]] = None,
        removed_tables: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Start building an metadata asynchronously using AgentCore Runtime

        This method:
        1. Validates the metadata configuration
        2. Scopes the build (deltaTargetTables) and, for delta builds with
           removed tables, runs VKG cleanup BEFORE invoking the agent
        3. Updates status to 'pending' in DynamoDB
        4. Invokes AgentCore agent with id only
        5. Returns immediately with status 'pending'

        The AgentCore agent handles everything:
        - Reads config from DynamoDB
        - Builds system/user prompts from config
        - Processes tables in background thread
        - Updates DynamoDB with progress after each table
        - Updates status to 'completed' or 'failed'

        Args:
            id: Unique identifier for the metadata
            target_tables: Optional list of "db.table" strings to scope a delta
                build. When set, written to config['deltaTargetTables'] so the
                agent's loop honors it. When None/empty, a full build is run and
                any stale scope is cleared.
            removed_tables: Optional list of "db.table" strings removed since the
                last build. When set (delta build), VKG cleanup deletes their
                Neptune triples + S3 fragments before the agent is invoked.

        Returns:
            Dictionary with immediate status response (pending)
        """
        try:
            # Get metadata configuration
            config = self.get_metadata_config(id)
            if not config:
                raise ValueError(f"metadata configuration not found: {id}")

            # Validate that data sources are selected
            data_sources = config.get('dataSources', [])
            if not data_sources:
                raise ValueError(f"No data sources selected for metadata: {id}")

            # Scope the build: a delta build records deltaTargetTables so the
            # agent's Task 15 filter processes only those tables; a full build
            # clears any stale scope so the agent processes everything.
            if target_tables:
                config['deltaTargetTables'] = list(target_tables)
            else:
                config.pop('deltaTargetTables', None)   # full build clears scope

            # For a delta build with removed tables, run VKG cleanup (Neptune
            # per-table delete + S3 fragment delete) BEFORE invoking the agent.
            if removed_tables:
                stored = config.get("tableFingerprints") or {}
                removed = []
                for dt in removed_tables:
                    db, tbl = dt.split(".", 1)
                    fp = stored.get(dt) or {}
                    cat = fp.get("catalogId", "AWSDataCatalog")
                    if not fp:
                        logger.warning(
                            "cleanup: removed table %s has no stored fingerprint; "
                            "defaulting catalog (Neptune delete still scoped by db.table)", dt)
                    removed.append((db, tbl, cat))
                DeltaPlannerService(
                    metadata_table=self.table, glue_service=GlueService(),
                    artifacts_bucket=os.environ.get("ARTIFACTS_BUCKET", ""),
                    region=os.environ.get("AWS_REGION", "us-east-1"),
                ).cleanup_removed(id, version=config['version'], layer_type="VKG",
                                  removed=removed)

            # Update status to 'pending' (queued for processing). Clear the previous
            # run's error/progress first — see _clear_previous_build_results.
            self._clear_previous_build_results(config)
            config['status'] = 'pending'
            config['updatedAt'] = datetime.now(timezone.utc).isoformat()
            config['buildStartedAt'] = datetime.now(timezone.utc).isoformat()
            self.table.put_item(Item=config)

            logger.info(f"Starting async metadata build for {id} with {len(data_sources)} data sources")

            try:
                # Invoke AgentCore with just the id
                # Agent will read DynamoDB, build prompts, and process asynchronously
                response = self.agentcore_service.invoke_ontology_agent(
                    id=id
                )

                logger.info(f"AgentCore agent started for metadata: {id}")

            except Exception as agent_error:
                logger.error(f"Failed to invoke AgentCore: {agent_error}")  # nosemgrep: logging-error-without-handling — exception converted to ValueError with status update
                # Update status to failed if AgentCore invocation fails
                config['status'] = 'failed'
                config['error'] = f"Failed to start agent: {str(agent_error)}"
                config['updatedAt'] = datetime.now(timezone.utc).isoformat()
                self.table.put_item(Item=config)
                raise ValueError(f"Failed to start agent: {str(agent_error)}")

            # Return immediately with pending status
            return {
                'id': id,
                'status': 'pending',
                'message': 'metadata build started. Poll /metadata/build-status/{id} for status.',
                'dataSourceCount': len(data_sources)
            }

        except Exception as e:
            logger.warning(f"Error starting async metadata build: {e}", exc_info=True)
            raise

    def dispatch_build(
        self, id: str, target_tables: Optional[List[str]] = None,
        removed_tables: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Route a build request to the correct per-type build entrypoint.

        Reads the ``type`` field from the ontology config in DynamoDB and
        dispatches:

        - ``'VKG'`` / ``'SemanticRAG'`` -> existing ``start_build_metadata_async``
          (the AgentCore agent reads the type from DynamoDB and branches
          internally, so both non-hybrid modes share this entrypoint — unchanged
          behaviour). Delta-scope params (``target_tables`` / ``removed_tables``)
          are forwarded so a delta build scopes ``deltaTargetTables`` and runs VKG
          cleanup for removed tables.
        - ``'Hybrid'`` -> new ``start_build_hybrid_async`` (full build only; the
          delta params do not apply and are ignored).

        ``type`` defaults to ``'VKG'`` when absent, matching the existing
        behaviour of the per-type build methods (which default the stored
        ``type`` to ``'VKG'``).

        :param id: Unique identifier for the ontology/metadata config.
        :param target_tables: Optional "db.table" list scoping a delta build
            (VKG/SemanticRAG only). ``None``/empty -> full build.
        :param removed_tables: Optional "db.table" list removed since the last
            build (VKG/SemanticRAG delta only) -> VKG cleanup before the agent runs.
        :returns: The immediate status dict returned by the selected builder.
        :raises ValueError: If the config is not found or ``type`` is unknown.
        """
        config = self.get_metadata_config(id)
        if not config:
            raise ValueError(f"Configuration not found: {id}")

        # Default to 'VKG' to match existing behaviour when type is absent.
        build_type = config.get('type') or 'VKG'
        if build_type not in ('Hybrid', 'VKG', 'SemanticRAG'):
            raise ValueError(f"Unknown build type '{build_type}' for config: {id}")

        # Ingest uploaded documents BEFORE the per-type dispatch, so all three
        # layer types get the identical document path. Placing it here rather
        # than in the three build entrypoints is the point: a document uploaded
        # to a VKG layer and the same document uploaded to a SemanticRAG layer
        # must be chunked, linked and indexed the same way.
        self._start_document_ingestion(ontology_id=id)

        if build_type == 'Hybrid':
            logger.info(f"Dispatching build for {id} to Hybrid path")
            return self.start_build_hybrid_async(id)
        if build_type in ('VKG', 'SemanticRAG'):
            logger.info(f"Dispatching build for {id} to {build_type} path")
            return self.start_build_metadata_async(
                id, target_tables=target_tables, removed_tables=removed_tables,
            )

        # Unreachable: build_type was validated above. Kept so a future type
        # added to the guard but not to the dispatch fails loudly.
        raise ValueError(f"Unhandled build type '{build_type}' for config: {id}")

    def _get_document_service(self):
        """Return the DocumentService used for build-time document ingestion.

        Built lazily and cached on the instance so importing this module does not
        pull in the document stack, and so tests can inject a stand-in by
        assigning ``self._document_service`` before the first call.

        :returns: A ``DocumentService`` instance.
        """
        existing = getattr(self, '_document_service', None)
        if existing is None:
            # Local import: keeps the document stack out of this module's import
            # graph, which several unit tests load without the doc env vars.
            from services.document_service import DocumentService
            existing = DocumentService()
            self._document_service = existing
        return existing

    def _start_document_ingestion(self, *, ontology_id: str) -> Dict[str, Any]:
        """Re-run the doc pipeline for every document uploaded to this layer.

        Called from ``dispatch_build`` for all three layer types. Every document
        is re-ingested rather than only the un-indexed ones: the Link stage links
        document entities to the layer's ontology classes, and a build is exactly
        when those classes change, so a document indexed against the previous
        build is stale.

        Failures never block the build — a layer whose structured half builds
        fine must not be held hostage by one unparseable upload. Each failure is
        logged and counted; the per-document status row keeps its stage flags, so
        the steward can see which documents did not progress.

        :param ontology_id: The semantic layer whose documents to ingest.
        :returns: Counts dict with ``documents``, ``started`` and ``failed``.
        """
        summary: Dict[str, Any] = {'documents': 0, 'started': 0, 'failed': 0}
        try:
            docs = self._get_document_service().list_documents(ontology_id=ontology_id)
        except Exception as exc:  # noqa: BLE001 — never block the build
            logger.warning(
                "document fan-out: could not list documents for %s: %s",
                ontology_id, exc, exc_info=True,
            )
            return summary

        summary['documents'] = len(docs)
        if not docs:
            return summary

        service = self._get_document_service()
        for doc in docs:
            doc_id = doc.get('docId') or ''
            try:
                # Unique execution name per build: Step Functions rejects a
                # duplicate name for 90 days, which would silently no-op every
                # rebuild's re-ingestion.
                service.start_pipeline(
                    ontology_id=ontology_id,
                    doc_id=doc_id,
                    s3_bucket=doc.get('s3Bucket') or '',
                    s3_key=doc.get('s3Key') or '',
                    filename=doc.get('filename') or '',
                    execution_name=f"build-{doc_id}-{uuid.uuid4().hex[:8]}",
                )
                summary['started'] += 1
            except Exception as exc:  # noqa: BLE001 — never block the build
                summary['failed'] += 1
                logger.warning(
                    "document fan-out: pipeline start failed for doc %s (layer %s): %s",
                    doc_id, ontology_id, exc, exc_info=True,
                )

        logger.info(
            "document fan-out for %s: %d document(s), %d started, %d failed",
            ontology_id, summary['documents'], summary['started'], summary['failed'],
        )
        return summary

    def start_build_hybrid_async(self, id: str) -> Dict[str, Any]:
        """Start a Hybrid build asynchronously via the creation AgentCore Runtime.

        Mirrors ``start_build_metadata_async``: validates the config, marks it
        ``pending`` in DynamoDB, then invokes the Hybrid creation agent (which
        reads its config from DynamoDB and processes in the background). The
        agent is reached through ``invoke_hybrid_creation_agent`` — the build
        (creation) path only; query dispatch happens via the chat gateway.

        :param id: Unique identifier for the Hybrid ontology config
            (used as the AgentCore session_id).
        :returns: Immediate status dict with ``status`` ``'pending'``.
        :raises ValueError: If the config is missing, has no data sources, or the
            agent invocation fails.
        """
        try:
            config = self.get_metadata_config(id)
            if not config:
                raise ValueError(f"Hybrid configuration not found: {id}")

            data_sources = config.get('dataSources', [])
            if not data_sources:
                raise ValueError(f"No data sources selected for Hybrid config: {id}")

            # Mark queued for processing before invoking the agent. Clear the
            # previous run's error/progress first — see
            # _clear_previous_build_results.
            self._clear_previous_build_results(config)
            config['status'] = 'pending'
            config['updatedAt'] = datetime.now(timezone.utc).isoformat()
            config['buildStartedAt'] = datetime.now(timezone.utc).isoformat()
            self.table.put_item(Item=config)

            logger.info(
                f"Starting async Hybrid build for {id} with {len(data_sources)} data sources"
            )

            try:
                self.agentcore_service.invoke_hybrid_creation_agent(id=id)
                logger.info(f"Hybrid creation agent started for: {id}")
            except Exception as agent_error:
                logger.error(f"Failed to invoke Hybrid creation agent: {agent_error}")  # nosemgrep: logging-error-without-handling — exception converted to ValueError with status update
                config['status'] = 'failed'
                config['error'] = f"Failed to start agent: {str(agent_error)}"
                config['updatedAt'] = datetime.now(timezone.utc).isoformat()
                self.table.put_item(Item=config)
                raise ValueError(f"Failed to start agent: {str(agent_error)}")

            return {
                'id': id,
                'status': 'pending',
                'message': 'Hybrid build started. Poll /build-status/{id} for status.',
                'dataSourceCount': len(data_sources),
            }

        except Exception as e:
            logger.warning(f"Error starting async Hybrid build: {e}", exc_info=True)
            raise

    def get_metadata_versions(self, id: str) -> List[Dict[str, Any]]:
        """
        Get all versions of an metadata, sorted by version number descending

        Args:
            id: Unique identifier for the metadata

        Returns:
            List of version records sorted by version number (newest first)
        """
        from boto3.dynamodb.conditions import Key as DKey
        response = self.table.query(
            KeyConditionExpression=DKey('id').eq(id)
        )
        items = [convert_decimals(i) for i in response.get('Items', [])]
        items.sort(key=lambda i: _version_num(i.get('version', 'v0')), reverse=True)
        return [
            {'version': i.get('version'), 'status': i.get('status'),
             'metadataPath': i.get('metadataPath', ''), 'updatedAt': i.get('updatedAt', '')}
            for i in items
        ]

    # Attributes written by a build RUN, as opposed to its configuration. They
    # describe the outcome of one attempt, so they must not survive into the next
    # one: ``get_build_status`` returns ``error`` unconditionally and the progress
    # fields whenever they are present, which meant a freshly-started build polled
    # as "in progress, and by the way here is the previous run's failure". Observed
    # on 2026-08-15: a rebuild that was 3 minutes into its KB phase still reported
    # the prior run's VKG error verbatim.
    _BUILD_RESULT_ATTRIBUTES = (
        'error',
        'tablesProcessed',
        'totalTables',
        'currentTable',
        'progressPercent',
    )

    def _clear_previous_build_results(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Drop the previous run's outcome attributes from a config, in place.

        Called when a build is marked ``pending`` so that polling reflects only the
        run now in flight.

        Args:
            config: The DynamoDB metadata item about to be written back.

        Returns:
            The same dict, with every attribute in ``_BUILD_RESULT_ATTRIBUTES``
            removed.
        """
        for attribute in self._BUILD_RESULT_ATTRIBUTES:
            config.pop(attribute, None)
        return config

    def get_build_status(self, id: str) -> Dict[str, Any]:
        """
        Get the build status of an metadata including real-time progress

        Args:
            id: Unique identifier for the metadata

        Returns:
            Dictionary with build status information including progress tracking
        """
        try:
            config = self.get_metadata_config(id)
            if not config:
                return {
                    'id': id,
                    'status': 'not_found'
                }

            # Build response with progress tracking fields
            response = {
                'id': id,
                'status': config.get('status', 'unknown'),
                'metadataPath': config.get('metadataPath'),
                'error': config.get('error'),
                'updatedAt': config.get('updatedAt')
            }

            # Include progress tracking fields if available
            if 'tablesProcessed' in config:
                response['tablesProcessed'] = config.get('tablesProcessed')
            if 'totalTables' in config:
                response['totalTables'] = config.get('totalTables')
            if 'currentTable' in config:
                response['currentTable'] = config.get('currentTable')
            if 'progressPercent' in config:
                response['progressPercent'] = config.get('progressPercent')

            return response

        except Exception as e:
            logger.warning(f"Error getting build status: {e}", exc_info=True)
            raise

    def _get_versioned_config(self, id: str, version: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve a specific version of an metadata configuration from DynamoDB

        Args:
            id: Unique identifier for the metadata
            version: Version string (e.g., 'v1', 'v2')

        Returns:
            Dictionary containing metadata configuration or None if not found
        """
        resp = self.table.get_item(Key={'id': id, 'version': version})
        item = resp.get('Item')
        return convert_decimals(item) if item else None

    def get_metadata_content(self, id: str, version_id: str) -> Dict[str, Any]:
        """
        Get the metadata file content (N-QUADS format) from S3 for a specific version

        Args:
            id: Unique identifier for the metadata
            version_id: Version string (e.g., 'v1', 'v2')

        Returns:
            Dictionary with content, version, and s3Path
        """
        config = self._get_versioned_config(id, version_id)
        if not config:
            raise ValueError(f"Version {version_id} not found for metadata {id}")
        metadata_path = config.get('metadataPath')
        if not metadata_path:
            raise ValueError("No metadata file stored for this version. "
                             "Ensure the agent assembly step has run successfully.")
        key = metadata_path.replace(f's3://{self.artifacts_bucket}/', '')
        obj = self.s3_client.get_object(Bucket=self.artifacts_bucket, Key=key)
        content = obj['Body'].read().decode('utf-8')
        return {'content': content, 'version': version_id, 's3Path': metadata_path}

    def upload_metadata_file(self, file_content: bytes, filename: str, id: str) -> Dict[str, Any]:
        """
        Upload a reference document, extracting its text before storage.

        The original binary is not stored. A .txt version containing the
        extracted plain text is written to S3 so agent document tools can
        read it line-by-line without binary parsing.

        Args:
            file_content: Raw bytes from the HTTP upload.
            filename: Original filename (used to choose the parser).
            id: Ontology ID — determines S3 prefix.

        Returns:
            Dict with 'filename', 'path' (S3 URI of .txt file), 'status'.

        Raises:
            ValueError: Propagated from extract_text_from_file for bad types.
        """
        try:
            # Extract plain text (raises ValueError for unsupported types)
            text = extract_text_from_file(file_content, filename)

            # Always store as .txt so agents can read it as plain text
            stem = os.path.splitext(filename)[0]
            txt_filename = stem + '.txt'
            file_key = f'ontologies/{id}/uploaded/{txt_filename}'

            self.s3_client.put_object(
                Bucket=self.artifacts_bucket,
                Key=file_key,
                Body=text.encode('utf-8'),
                ContentType='text/plain',
            )

            file_path = f's3://{self.artifacts_bucket}/{file_key}'

            # Record in DynamoDB config (preserve original filename for display)
            config = self.get_metadata_config(id)
            if config:
                config.setdefault('uploadedFiles', []).append({
                    'filename': filename,
                    'path': file_path,
                    'uploadedAt': datetime.now(timezone.utc).isoformat(),
                })
                config['updatedAt'] = datetime.now(timezone.utc).isoformat()
                self.table.put_item(Item=config)

            logger.info(f"Uploaded and extracted: {filename} → {file_key}")
            return {'id': id, 'filename': filename, 'path': file_path, 'status': 'uploaded'}

        except Exception as e:
            logger.warning(f"Error uploading metadata file: {e}", exc_info=True)
            raise

    def delete_metadata(self, id: str) -> Dict[str, Any]:
        """
        Delete an metadata from DynamoDB, S3, and Neptune.

        Args:
            id: Unique identifier for the metadata

        Returns:
            Dictionary with deletion status including Neptune result
        """
        try:
            # Delete all version records for this id from DynamoDB
            from boto3.dynamodb.conditions import Key as DKey
            resp = self.table.query(
                KeyConditionExpression=DKey('id').eq(id),
                ProjectionExpression='id, version',
            )
            for item in resp.get('Items', []):
                self.table.delete_item(Key={'id': item['id'], 'version': item['version']})
            logger.info(f"Deleted {len(resp.get('Items', []))} version record(s) from DynamoDB: {id}")

            # List and delete all S3 objects for this metadata
            prefix = f'ontologies/{id}/'
            objects_to_delete = []

            paginator = self.s3_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=self.artifacts_bucket, Prefix=prefix):
                for obj in page.get('Contents', []):
                    objects_to_delete.append({'Key': obj['Key']})

            # delete_objects accepts max 1000 keys per call — batch accordingly
            for i in range(0, len(objects_to_delete), 1000):
                batch = objects_to_delete[i:i + 1000]
                self.s3_client.delete_objects(
                    Bucket=self.artifacts_bucket,
                    Delete={'Objects': batch}
                )
            if objects_to_delete:
                logger.info(f"Deleted {len(objects_to_delete)} files from S3 for metadata: {id}")

            # Delete Neptune named graph — best-effort, do not fail the overall delete
            neptune_result = None
            try:
                neptune_service = NeptuneService()
                neptune_result = neptune_service.delete_graph(id)
                if neptune_result.get('success'):
                    logger.info(
                        f"Deleted Neptune graph for metadata {id}: "
                        f"{neptune_result.get('triples_deleted', 0)} triples removed"
                    )
                else:
                    logger.warning(
                        f"Neptune graph delete returned non-success for metadata {id}: "
                        f"{neptune_result.get('error') or neptune_result.get('message')}"
                    )
            except Exception as neptune_err:
                logger.warning(f"Neptune graph cleanup failed for metadata {id} (continuing): {neptune_err}")
                neptune_result = {'success': False, 'error': str(neptune_err)}

            return {
                'id': id,
                'status': 'deleted',
                'filesDeleted': len(objects_to_delete),
                'neptuneGraph': neptune_result,
            }

        except Exception as e:
            logger.warning(f"Error deleting metadata: {e}", exc_info=True)
            raise

    def start_revision_async(
        self, id: str, base_version: str, annotations: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Start a revision of an metadata asynchronously using AgentCore Runtime

        This method:
        1. Computes the next version number from existing versions
        2. Reads the current active config (highest-version record)
        3. Stamps it with revision context (revisionMode, targetVersion, instructions)
        4. Invokes AgentCore agent with id
        5. Returns immediately with status 'building'

        Args:
            id: Unique identifier for the metadata
            base_version: Version to base revisions on (e.g., 'v2')
            annotations: List of annotation dictionaries with revision instructions

        Returns:
            Dictionary with immediate status response (building)
        """
        try:
            # Get all versions to compute next version number
            all_versions = self.get_metadata_versions(id)
            highest = max((_version_num(v['version']) for v in all_versions), default=1)
            next_version = f'v{highest + 1}'

            # Get current active config (highest-version record)
            current_config = self.get_metadata_config(id)
            if not current_config:
                raise ValueError(f"metadata {id} not found")

            # Stamp the active record with revision context
            current_config.update({
                'revisionMode': True,
                'revisionBaseVersion': base_version,
                'revisionInstructions': annotations,
                'targetVersion': next_version,
                'status': 'pending',
                'updatedAt': datetime.now(timezone.utc).isoformat(),
            })

            # Persist updated v1 record to DynamoDB
            self.table.put_item(Item=current_config)

            logger.info(f"Started revision for metadata {id}: base={base_version}, next={next_version}")

            # Invoke AgentCore agent to execute the revision
            try:
                self.agentcore_service.invoke_ontology_agent(id=id)
                logger.info(f"AgentCore agent invoked for revision of metadata: {id}")
            except Exception as agent_error:
                logger.error(f"Failed to invoke AgentCore for revision: {agent_error}")  # nosemgrep: logging-error-without-handling — exception converted to ValueError with status update
                # Update status to failed if AgentCore invocation fails
                current_config['status'] = 'failed'
                current_config['error'] = f"Failed to start revision agent: {str(agent_error)}"
                current_config['updatedAt'] = datetime.now(timezone.utc).isoformat()
                self.table.put_item(Item=current_config)
                raise ValueError(f"Failed to start revision agent: {str(agent_error)}")

            # Return immediately with building status
            return {
                'id': id,
                'status': 'building',
                'currentVersion': base_version,
                'nextVersion': next_version,
                'message': f'Revision started. New version {next_version} pending.',
            }

        except Exception as e:
            logger.warning(f"Error starting async metadata revision: {e}", exc_info=True)
            raise

