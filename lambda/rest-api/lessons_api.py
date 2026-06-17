"""Lessons-learned API — read/delete surface over AgentCore Memory.

Agents persist turns into Bedrock AgentCore Memory through the Strands
``LessonsMemoryHooks`` provider (PII-redacted via Bedrock Guardrails). This
API only exposes the admin surface:

  - ``GET /{ontology_id}`` — list long-term semantic-strategy records
  - ``DELETE /{ontology_id}/{record_id}`` — delete one record by id

There is intentionally no write endpoint: every memory write must run
through the guardrail-redaction hook in the agent runtime.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from services.agentcore_memory_service import AgentCoreMemoryService

logger = logging.getLogger(__name__)

app = FastAPI(title="Lessons API")
memory_service = AgentCoreMemoryService()


@app.get("/{ontology_id}")
async def list_lessons(
    ontology_id: str,
    # AgentCore's ListMemoryRecords hard-caps maxResults at 100; bound the
    # query param to match so the contract is honest end-to-end.
    limit: int = Query(default=50, ge=1, le=100),
):
    """Admin UI — list long-term memory records for one ontology.

    Returns ``[]`` when ``LESSONS_MEMORY_ID`` is not configured so the UI
    degrades gracefully in environments without a deployed memory resource.
    """
    items = memory_service.list_records(
        ontology_id=ontology_id,
        max_results=limit,
    )
    return JSONResponse(content={'lessons': items})


@app.delete("/{ontology_id}/{record_id:path}")
async def delete_lesson(ontology_id: str, record_id: str):
    """Admin UI — remove one long-term memory record.

    ``ontology_id`` is part of the path for symmetry with the list call and
    for future authz scoping; the record id alone is what AgentCore needs.
    """
    try:
        memory_service.delete_record(memory_record_id=record_id)
    except ValueError as exc:
        # configured=False — surface as 503 so the operator notices.
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — translate to HTTP error
        logger.error("delete_memory_record failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="failed to delete record")
    return JSONResponse(content={'deleted': record_id})


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "lessons-api"}
