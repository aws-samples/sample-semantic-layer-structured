"""Evaluations API — list / fetch / store OnDemand evaluation-pipeline runs per
semantic layer, backing the admin "Evaluations" tab.

The eval-runner (triggered by an ``evaluation.requested`` EventBridge event when
a layer version completes) POSTs a result envelope here; the admin tab GETs the
run summaries and drills into one run's per-question metrics.

Route shape mirrors the other per-ontology sub-apps (``/feedback/<id>`` etc.).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)

app = FastAPI(title="Evaluations API")
evaluation_service = EvaluationService()


@app.get("/{ontology_id}")
async def list_runs(ontology_id: str, limit: int = Query(default=50, ge=1, le=200)):
    """List evaluation-run summaries for one layer, newest first.

    Returns ``{runs: [...]}`` (each item is a run envelope WITHOUT the heavy
    per-question rows). Empty list when nothing has run yet.
    """
    try:
        runs = evaluation_service.list_runs(ontology_id=ontology_id, limit=limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return JSONResponse(content={"runs": runs})


@app.get("/{ontology_id}/{run_id}")
async def get_run(ontology_id: str, run_id: str):
    """Return one full evaluation run (incl. per-question metric rows)."""
    try:
        run = evaluation_service.get_run(ontology_id=ontology_id, run_id=run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if run is None:
        raise HTTPException(status_code=404, detail="evaluation run not found")
    return JSONResponse(content=run)


@app.post("/{ontology_id}")
async def put_run(ontology_id: str, request: Request):
    """Store an evaluation run for one layer (called by the eval-runner).

    Body is a run dict carrying at least ``results`` (per-question rows). The
    service fills in runId / createdAt / summary if absent. Returns the stored
    envelope.
    """
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {exc}")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    try:
        envelope = evaluation_service.put_run(ontology_id=ontology_id, run=body)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return JSONResponse(content=envelope)


@app.delete("/{ontology_id}/{run_id}")
async def delete_run(ontology_id: str, run_id: str):
    """Delete one stored evaluation run (idempotent)."""
    try:
        evaluation_service.delete_run(ontology_id=ontology_id, run_id=run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("delete_run failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="failed to delete run")
    return JSONResponse(content={"deleted": run_id})
