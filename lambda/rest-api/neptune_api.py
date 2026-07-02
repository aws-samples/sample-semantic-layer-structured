"""
Neptune Graph API

This module provides REST endpoints for Neptune graph operations:
- Get graph summary statistics
- Get graph metadata
"""

import logging
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from services.neptune_service import NeptuneService

logger = logging.getLogger(__name__)

# Create FastAPI sub-application
app = FastAPI(
    title="Neptune Graph API",
    description="API for Neptune graph queries and statistics"
)

# Initialize Neptune service
neptune_service = NeptuneService()


@app.get("/graph/summary/{ontology_id}")
async def get_graph_summary(ontology_id: str):
    """
    Get summary statistics for a specific ontology graph

    Returns:
    - Total number of classes
    - Total number of properties
    - Total number of relationships
    - Graph metadata
    """
    try:
        logger.info(f"Getting graph summary for ontology: {ontology_id}")

        result = neptune_service.get_graph_summary(ontology_id)

        return JSONResponse(
            content=result,
            status_code=200
        )

    except Exception as e:
        logger.warning(f"Error getting graph summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get graph summary: {str(e)}"
        )


@app.get("/graph/stats/{ontology_id}")
async def get_graph_stats(ontology_id: str):
    """
    Get detailed statistics for a specific ontology graph

    Returns:
    - Class counts by type
    - Property counts by type
    - Relationship distribution
    - Virtual KG mappings
    """
    try:
        logger.info(f"Getting graph stats for ontology: {ontology_id}")

        result = neptune_service.get_graph_stats(ontology_id)

        return JSONResponse(
            content=result,
            status_code=200
        )

    except Exception as e:
        logger.warning(f"Error getting graph stats: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get graph stats: {str(e)}"
        )


@app.get("/graph/classes/{ontology_id}")
async def get_graph_classes(ontology_id: str):
    """
    Get list of all classes in the ontology graph
    """
    try:
        logger.info(f"Getting classes for ontology: {ontology_id}")

        result = neptune_service.get_graph_classes(ontology_id)

        return JSONResponse(
            content=result,
            status_code=200
        )

    except Exception as e:
        logger.warning(f"Error getting graph classes: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get graph classes: {str(e)}"
        )


@app.get("/graph/properties/{ontology_id}")
async def get_graph_properties(ontology_id: str):
    """
    Get list of all properties in the ontology graph
    """
    try:
        logger.info(f"Getting properties for ontology: {ontology_id}")

        result = neptune_service.get_graph_properties(ontology_id)

        return JSONResponse(
            content=result,
            status_code=200
        )

    except Exception as e:
        logger.warning(f"Error getting graph properties: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get graph properties: {str(e)}"
        )
