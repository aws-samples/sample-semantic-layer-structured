"""
Query API Endpoints

Provides REST API endpoints for natural language query processing:
- Submit natural language query
- Get query result
- Get query status
- List query history
- Delete query
"""

import logging
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.query_service import QueryService
from services.agentcore_service import AgentCoreService

logger = logging.getLogger(__name__)

# Create FastAPI app for query endpoints
app = FastAPI(title="Query API")

# Initialize services
query_service = QueryService()
agentcore_service = AgentCoreService()


# Pydantic models for request/response validation
class SubmitQueryRequest(BaseModel):
    question: str
    id: str


# ============================================================================
# Query Submission and Results Endpoints
# ============================================================================

@app.post("/submit")
async def submit_query(request: SubmitQueryRequest):
    """
    Submit a natural language query for processing

    This endpoint:
    1. Converts natural language to SQL using Amazon Bedrock
    2. Executes the SQL query on Amazon Athena
    3. Returns a query ID for tracking

    Args:
        request: Query request with question and ontology ID

    Returns:
        Query ID and initial status
    """
    try:
        result = query_service.submit_query(
            question=request.question,
            id=request.id,
        )

        return JSONResponse(content=result, status_code=202)

    except Exception as e:
        logger.error(f"Error submitting query: {e}")  # nosemgrep: logging-error-without-handling — exception converted to HTTPException, not re-raised as-is
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/result/{query_id}")
async def get_query_result(query_id: str):
    """
    Get the result of a completed query

    Args:
        query_id: Unique identifier for the query

    Returns:
        Query results including:
        - Original question
        - Generated SQL query
        - Result rows (up to 1000)
        - Execution metadata
    """
    try:
        result = query_service.get_query_result(query_id)

        if result.get('status') == 'NOT_FOUND':
            raise HTTPException(status_code=404, detail="Query not found")

        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting query result: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{query_id}")
async def get_query_status(query_id: str):
    """
    Get the status of a query

    Args:
        query_id: Unique identifier for the query

    Returns:
        Query status (SUBMITTED, RUNNING, SUCCEEDED, FAILED, CANCELLED)
    """
    try:
        status = query_service.get_query_status(query_id)

        if status.get('status') == 'NOT_FOUND':
            raise HTTPException(status_code=404, detail="Query not found")

        return JSONResponse(content=status)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting query status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Query Suggestions Endpoint
# ============================================================================

@app.get("/suggestions/{ontology_id}")
async def get_query_suggestions(ontology_id: str):
    """
    Generate AI-powered suggested questions for a semantic metadata layer.

    Invokes the Query Suggestions Agent synchronously. The agent retrieves
    schema context from the Bedrock Knowledge Base and generates 5-8 diverse
    questions relevant to the ontology's data model.

    Args:
        ontology_id: The ontology config ID to generate suggestions for

    Returns:
        JSON with 'suggestions' list: [{"category": str, "question": str}]
    """
    try:
        logger.info(f"Generating query suggestions for ontology: {ontology_id}")
        result = agentcore_service.invoke_suggestions_agent(id=ontology_id)

        if not result.get('success'):
            raise HTTPException(status_code=500, detail="Suggestions agent failed")

        data = result.get('data', {})
        if 'error' in data:
            logger.error(f"Suggestions agent returned error: {data['error']}")
            raise HTTPException(status_code=500, detail=data['error'])

        return JSONResponse(content=data)

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Configuration error for suggestions: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting query suggestions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Health check
@app.get("/health")
async def health_check():
    """Health check endpoint for query API"""
    return {"status": "healthy", "service": "query-api"}
