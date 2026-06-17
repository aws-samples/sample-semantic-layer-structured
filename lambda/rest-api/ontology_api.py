"""
Ontology API Endpoints

Provides REST API endpoints for managing ontologies:
- Create/update ontology configurations
- Get ontology configuration
- List all ontologies
- Build ontology from data sources
- Get build status
- Upload ontology files
- Delete ontology
"""

import logging
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

from services.ontology_service import OntologyService
from services.glue_service import GlueService

logger = logging.getLogger(__name__)

# Create FastAPI app for ontology endpoints
app = FastAPI(title="Ontology API")

# Initialize services
ontology_service = OntologyService()
glue_service = GlueService()


# Pydantic models for request/response validation
class OntologyConfig(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None  # Normalized namespace (lowercase, no spaces)
    type: Optional[str] = None  # 'VKG' or 'SemanticRAG'
    dataSources: Optional[List[Dict[str, Any]]] = None
    configuration: Optional[Dict[str, Any]] = {}

    # Frontend fields
    dataSourcesDescription: Optional[str] = None
    useCasesDescription: Optional[str] = None
    selectedDataSources: Optional[List[Dict[str, Any]]] = None
    uploadedDocuments: Optional[List[Dict[str, Any]]] = None  # Multiple uploaded files
    createdBy: Optional[str] = None
    status: Optional[str] = None


class BuildOntologyRequest(BaseModel):
    dataSources: List[Dict[str, str]]


class ReviseOntologyRequest(BaseModel):
    annotations: List[Dict[str, str]]


# ============================================================================
# Ontology Configuration Endpoints
# ============================================================================

@app.post("/config")
async def create_ontology_config(config: OntologyConfig):
    """
    Create or update an ontology configuration

    Args:
        config: Ontology configuration with name, description, data sources

    Returns:
        Created ontology configuration
    """
    try:
        result = ontology_service.create_metadata_config(config.dict())
        return JSONResponse(content=result, status_code=201)

    except Exception as e:
        logger.warning(f"Error creating ontology config: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/config/{ontology_id}")
async def get_ontology_config(ontology_id: str):
    """
    Get ontology configuration by ID

    Args:
        ontology_id: Unique identifier for the ontology

    Returns:
        Ontology configuration or 404 if not found
    """
    try:
        config = ontology_service.get_metadata_config(ontology_id)

        if not config:
            raise HTTPException(status_code=404, detail="Ontology not found")

        return JSONResponse(content=config)

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Error retrieving ontology config: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/list")
async def list_ontologies():
    """
    List all ontology configurations

    Returns:
        List of ontology summaries
    """
    try:
        ontologies = ontology_service.list_ontologies()
        return JSONResponse(content={'ontologies': ontologies, 'count': len(ontologies)})

    except Exception as e:
        logger.warning(f"Error listing ontologies: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/config/{ontology_id}")
async def delete_ontology(ontology_id: str):
    """
    Delete an ontology and all associated files

    Args:
        ontology_id: Unique identifier for the ontology

    Returns:
        Deletion status
    """
    try:
        result = ontology_service.delete_metadata(ontology_id)
        return JSONResponse(content=result)

    except Exception as e:
        logger.warning(f"Error deleting ontology: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Ontology Building Endpoints
# ============================================================================

@app.post("/build/{ontology_id}")
async def build_ontology(ontology_id: str):
    """
    Build an ontology from configured data sources using AgentCore Runtime (ASYNC)

    This endpoint:
    1. Validates the ontology configuration
    2. Updates status to 'building' in DynamoDB
    3. Triggers asynchronous Lambda invocation to run the build in background
    4. Returns immediately with status 'building'

    The actual build process (executed asynchronously):
    - Extracts metadata from AWS Glue
    - Retrieves ontology patterns from Knowledge Base
    - Generates N-QUADS with Virtual KG mappings
    - Persists to Neptune
    - Saves Turtle format to S3

    Use the /build-status/{ontology_id} endpoint to poll for completion status.

    Args:
        ontology_id: Unique identifier for the ontology

    Returns:
        Immediate response with status 'building'
    """
    try:
        logger.info(f"Starting async ontology build for: {ontology_id}")

        # Start async build (returns immediately)
        result = ontology_service.start_build_metadata_async(ontology_id)

        logger.info(f"Async ontology build triggered for: {ontology_id}")
        return JSONResponse(content=result, status_code=202)  # 202 Accepted

    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"Validation error: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning(f"Error starting ontology build: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/build-status/{ontology_id}")
async def get_build_status(ontology_id: str):
    """
    Get the build status of an ontology

    Args:
        ontology_id: Unique identifier for the ontology

    Returns:
        Build status information
    """
    try:
        status = ontology_service.get_build_status(ontology_id)
        return JSONResponse(content=status)

    except Exception as e:
        logger.warning(f"Error getting build status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/versions/{ontology_id}")
async def list_ontology_versions(ontology_id: str):
    """
    List all versions of an ontology

    Args:
        ontology_id: Unique identifier for the ontology

    Returns:
        List of ontology versions sorted by version number (newest first)
    """
    try:
        versions = ontology_service.get_metadata_versions(ontology_id)
        return JSONResponse(content={'versions': versions})

    except Exception as e:
        logger.warning(f"Error listing versions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/content/{ontology_id}/{version_id}")
async def get_ontology_content(ontology_id: str, version_id: str):
    """
    Get the ontology file content (N-QUADS format) for a specific version

    Args:
        ontology_id: Unique identifier for the ontology
        version_id: Version string (e.g., 'v1', 'v2')

    Returns:
        Ontology content with metadata
    """
    try:
        result = ontology_service.get_metadata_content(ontology_id, version_id)
        return JSONResponse(content=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.warning(f"Error fetching ontology content: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/revise/{ontology_id}/{version_id}")
async def revise_ontology(ontology_id: str, version_id: str, body: ReviseOntologyRequest):
    """
    Start a revision of an ontology based on annotations (ASYNC)

    This endpoint:
    1. Validates that annotations are provided
    2. Stores revision context into the v1 record
    3. Triggers asynchronous revision via AgentCore Runtime
    4. Returns immediately with status 'building'

    The actual revision process (executed asynchronously):
    - Reads the base version from S3
    - Applies annotations using Bedrock agent
    - Generates new N-QUADS for next version
    - Persists to Neptune
    - Creates versioned record (v3, v4, etc.)

    Use the /build-status/{ontology_id} endpoint to poll for completion status.

    Args:
        ontology_id: Unique identifier for the ontology
        version_id: Version to base revision on (e.g., 'v2')
        body: ReviseOntologyRequest with annotations

    Returns:
        Immediate response with status 'building' and nextVersion
    """
    try:
        if not body.annotations:
            raise HTTPException(status_code=400, detail="At least one annotation is required")
        result = ontology_service.start_revision_async(ontology_id, version_id, body.annotations)
        return JSONResponse(content=result, status_code=202)  # 202 Accepted
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning(f"Error starting revision: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# File Upload Endpoint
# ============================================================================

@app.post("/upload")
async def upload_ontology_file(
    file: UploadFile = File(...),
    id: str = Form(...)
):
    """
    Upload a reference document (Markdown, plain text, PDF, or DOCX).

    The server extracts plain text before storing — binary formats are
    never written to S3 verbatim. Unsupported extensions return HTTP 400.

    Args:
        file: The file to upload
        id: Unique identifier for the ontology

    Returns:
        Upload status and file location
    """
    import os as _os
    from services.ontology_service import _SUPPORTED_TEXT_EXTENSIONS
    ext = _os.path.splitext(file.filename.lower())[1]
    if ext not in _SUPPORTED_TEXT_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. "
                   f"Supported: {', '.join(sorted(_SUPPORTED_TEXT_EXTENSIONS))}",
        )

    try:
        file_content = await file.read()
        result = ontology_service.upload_metadata_file(
            file_content=file_content,
            filename=file.filename,
            id=id
        )
        return JSONResponse(content=result)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning(f"Error uploading ontology file: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Health check
@app.get("/health")
async def health_check():
    """Health check endpoint for ontology API"""
    return {"status": "healthy", "service": "ontology-api"}
