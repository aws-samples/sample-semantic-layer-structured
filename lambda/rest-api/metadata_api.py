"""
Metadata API Endpoints

Provides REST API endpoints for metadata enrichment and table KB documents.
Natural-language metadata querying runs over the streaming AG-UI chat path
(``/query/chat``) — there is no async submit/status/result surface here.
"""

import logging
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List, Dict

from services.metadata_service import MetadataService

logger = logging.getLogger(__name__)

# Create FastAPI app for metadata endpoints
app = FastAPI(title="Metadata API")

# Initialize service
metadata_service = MetadataService()


# Pydantic models for request/response validation
class MetadataEnrichRequest(BaseModel):
    id: str  # ontology config ID — backend reads dataSources from DynamoDB
    targetTables: Optional[List[str]] = None   # e.g. ["database.table"] — if set, only re-enrich these
    annotations: Optional[List[Dict[str, str]]] = None  # [{target, instruction}]


class ReviseMetadataRequest(BaseModel):
    annotations: List[Dict[str, str]]


# ============================================================================
# Metadata Enrichment Endpoints
# ============================================================================

@app.post("/enrich", status_code=202)
async def enrich_metadata(request: MetadataEnrichRequest):
    """
    Start metadata enrichment for a database (ASYNC)

    This endpoint:
    1. Validates the database and catalog information
    2. Triggers asynchronous enrichment job
    3. Returns immediately with job ID and status 'enriching'

    The actual enrichment process (executed asynchronously):
    - Retrieves table and column metadata from AWS Glue
    - Uses Bedrock agent with MetadataService to generate enriched descriptions
    - Persists enriched metadata back to data catalog

    Use the /enrich/status/{job_id} endpoint to poll for completion status.

    Args:
        request: Metadata enrichment request with database name and optional catalog ID

    Returns:
        Immediate response with job ID and status 'enriching'
    """
    try:
        logger.info(f"Starting async metadata enrichment for ontology: {request.id}")

        result = metadata_service.start_metadata_enrichment(
            id=request.id,
            target_tables=request.targetTables,
            annotations=request.annotations,
        )

        logger.info(f"Metadata enrichment triggered with job_id: {result.get('job_id')}")
        return JSONResponse(content=result, status_code=202)  # 202 Accepted

    except ValueError as e:
        logger.warning(f"Validation error: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning(f"Error starting metadata enrichment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/enrich/status/{job_id}")
async def get_enrichment_status(job_id: str):
    """
    Get the status of a metadata enrichment job

    Args:
        job_id: Unique identifier for the enrichment job

    Returns:
        Job status information including progress and any errors
    """
    try:
        status = metadata_service.get_enrichment_status(job_id)

        if status.get('status') == 'NOT_FOUND':
            raise HTTPException(status_code=404, detail="Enrichment job not found")

        return JSONResponse(content=status)

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Error getting enrichment status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/revise/{id}/{version_id}", status_code=202)
async def revise_metadata(id: str, version_id: str, body: ReviseMetadataRequest):
    """
    Start a versioned revision run (ASYNC).

    Stamps v1 with revisionMode=True + targetVersion, invokes the metadata agent.
    Poll status via GET /enrich/status/{id}.

    Returns immediately with status 'building' and the next version label.
    """
    try:
        if not body.annotations:
            raise HTTPException(status_code=400, detail="At least one annotation is required")
        result = metadata_service.start_metadata_revision(id, version_id, body.annotations)
        return JSONResponse(content=result, status_code=202)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning(f"Error starting metadata revision: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Knowledge Base Table Metadata
# ============================================================================

@app.get("/table/{database_name}/{table_name}")
async def get_table_kb_metadata(
    database_name: str,
    table_name: str,
    catalog_id: str,
    semantic_layer_id: str,
    semantic_layer_version: str,
):
    """
    Return the AI-enriched metadata for a single table from the S3 document
    written by the metadata agent (source of truth for the Bedrock Knowledge Base).

    The metadata agent writes documents under
    ``metadata/{semantic_layer_id}/{semantic_layer_version}/{catalog_id}/{database_name}/{table_name}.md``,
    so all four scoping fields are required to locate the document.

    Returns structured JSON: description, columns [{name, type, description}].
    404 when the document has not been generated yet.
    """
    try:
        result = metadata_service.get_table_kb_metadata(
            database_name=database_name,
            table_name=table_name,
            catalog_id=catalog_id,
            semantic_layer_id=semantic_layer_id,
            semantic_layer_version=semantic_layer_version,
        )
        if not result.get('success'):
            raise HTTPException(status_code=404, detail=result.get('error', 'Not found'))
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Error getting KB table metadata for {database_name}.{table_name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Health Check
# ============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint for metadata API"""
    return {"status": "healthy", "service": "metadata-api"}
