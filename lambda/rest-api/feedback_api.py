"""Feedback admin API — list & delete per-turn user feedback rows.

The matching write surface lives in ``query_api.py`` (``POST /query/feedback``)
so the bar under each assistant turn doesn't have to know about a separate
sub-app. Reads/deletes live here because the admin tab is a different
audience and the route shape (``/feedback/<ontologyId>``) mirrors
``/lessons/<ontologyId>``.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from services.feedback_service import FeedbackService

logger = logging.getLogger(__name__)

app = FastAPI(title="Feedback API")
feedback_service = FeedbackService()


@app.get("/{ontology_id}")
async def list_feedback(
    ontology_id: str,
    limit: int = Query(default=50, ge=1, le=200),
):
    """Admin UI — list all feedback rows for one ontology, newest first.

    Returns ``{'feedback': []}`` when ``FEEDBACK_TABLE`` isn't configured so
    the UI degrades gracefully on stale deployments.
    """
    items = feedback_service.list_for_ontology(
        ontology_id=ontology_id, limit=limit,
    )
    return JSONResponse(content={'feedback': items})


@app.delete("/{ontology_id}/{feedback_id}")
async def delete_feedback(ontology_id: str, feedback_id: str):
    """Admin UI — remove one feedback row.

    Maps the service's ``ValueError`` (not-found / not-configured) to 404 / 503
    and any other exception to 500 with a generic detail.
    """
    try:
        feedback_service.delete(
            ontology_id=ontology_id, feedback_id=feedback_id,
        )
    except ValueError as exc:
        msg = str(exc)
        if 'not configured' in msg:
            raise HTTPException(status_code=503, detail=msg)
        raise HTTPException(status_code=404, detail=msg)
    except Exception as exc:  # noqa: BLE001
        logger.error("delete_feedback failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="failed to delete feedback")
    return JSONResponse(content={'deleted': feedback_id})


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "feedback-api"}
