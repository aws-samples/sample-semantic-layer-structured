"""
Data Source API Endpoints

Provides REST API endpoints for managing data sources:
- List Glue databases (all catalogs)
- List Glue tables in a database
- Get table metadata
- Extract metadata for semantic metadata generation
- Start Glue crawler
- Get crawler status
"""

import logging
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional

from services.glue_service import GlueService

logger = logging.getLogger(__name__)

# Create FastAPI app for data source endpoints
app = FastAPI(title="Data Source API")

# Initialize services
glue_service = GlueService()


# Pydantic models for request/response validation
class DataSourceEntry(BaseModel):
    dataSource: Optional[str] = 'AwsDataCatalog'
    catalogId: Optional[str] = 'AWSDataCatalog'
    databaseName: str
    tableName: Optional[str] = None   # None = entire database selected
    tableId: Optional[str] = None


class ExtractMetadataRequest(BaseModel):
    dataSources: List[DataSourceEntry]


# ============================================================================
# Glue Database and Table Endpoints
# ============================================================================

@app.get("/glue/databases")
async def list_glue_databases():
    """
    List all AWS Glue databases across all catalogs (AWSDataCatalog + federated).

    Returns:
        List of database dicts, each including a 'catalogId' field.
    """
    try:
        databases = glue_service.list_databases()
        return JSONResponse(content={
            'databases': databases,
            'count': len(databases)
        })

    except Exception as e:
        logger.warning(f"Error listing Glue databases: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/glue/tables/{database_name}")
async def list_glue_tables(
    database_name: str,
    catalogId: Optional[str] = Query(None, description="Catalog ID (e.g. 's3tablescatalog/<bucket>'). Omit for AWSDataCatalog."),
):
    """
    List all tables in a specific Glue database.

    Args:
        database_name: Name of the Glue database
        catalogId: Optional catalog ID query parameter

    Returns:
        List of tables with metadata, each including a 'catalogId' field.
    """
    try:
        tables = glue_service.list_tables(database_name, catalog_id=catalogId)
        return JSONResponse(content={
            'databaseName': database_name,
            'catalogId': catalogId or 'AWSDataCatalog',
            'tables': tables,
            'count': len(tables)
        })

    except Exception as e:
        logger.warning(f"Error listing Glue tables: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/glue/metadata/{database_name}/{table_name}")
async def get_table_metadata(
    database_name: str,
    table_name: str,
    catalogId: Optional[str] = Query(None, description="Catalog ID. Omit for AWSDataCatalog."),
):
    """
    Get detailed metadata for a specific Glue table.

    Args:
        database_name: Name of the Glue database
        table_name: Name of the table
        catalogId: Optional catalog ID query parameter

    Returns:
        Table metadata including schema, partitions, and statistics.
    """
    try:
        metadata = glue_service.get_table_metadata(database_name, table_name, catalog_id=catalogId)
        return JSONResponse(content=metadata)

    except Exception as e:
        logger.warning(f"Error getting table metadata: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/extract-metadata")
async def extract_metadata(request: ExtractMetadataRequest):
    """
    Extract metadata from selected data sources for metadata generation.

    Args:
        request: List of data sources. Each entry may include 'catalogId',
                 'databaseName', 'tableName', and 'dataSource'.

    Returns:
        Extracted metadata for all selected tables including columns and detected relationships.
    """
    try:
        logger.info(f"Extracting metadata for {len(request.dataSources)} data sources")

        metadata = glue_service.extract_metadata_for_semantic_metadata(
            [ds.model_dump() for ds in request.dataSources]
        )

        return JSONResponse(content=metadata)

    except Exception as e:
        logger.warning(f"Error extracting metadata: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Health check
@app.get("/health")
async def health_check():
    """Health check endpoint for data source API"""
    return {"status": "healthy", "service": "datasource-api"}
