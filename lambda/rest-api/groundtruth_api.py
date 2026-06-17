"""Groundtruth-dataset API — upload / fetch / delete the per-semantic-layer
evaluation dataset backing the admin "Ground truth dataset" tab.

Route shape mirrors ``/feedback/<ontologyId>`` and ``/documents/<ontologyId>``:
the dataset is scoped to one semantic layer (ontology) so each layer can be
evaluated against its own curated ground truth.

The uploaded payload must be in the AgentCore ground-truth evaluation format —
a JSON array of records, each with: Natural_Language_Question, Expected_Answer,
Expected_SQL_Query, Expected_SQL_Result.
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from services.groundtruth_service import (
    GroundtruthService,
    GroundtruthValidationError,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Groundtruth API")
groundtruth_service = GroundtruthService()


@app.post("/{ontology_id}/upload")
async def upload_dataset(ontology_id: str, file: UploadFile = File(...)):
    """Upload a ground-truth dataset (JSON) for one semantic layer.

    Accepts a JSON array of records (or an object with a ``records`` array).
    Validates the AgentCore ground-truth schema before storing. Returns
    ``{ontologyId, recordCount, uploadedAt}``.
    """
    raw = await file.read()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"file is not valid JSON: {exc}")
    try:
        meta = groundtruth_service.put(ontology_id=ontology_id, records=parsed)
    except GroundtruthValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        # ARTIFACTS_BUCKET unset — misconfiguration, not the caller's fault.
        raise HTTPException(status_code=503, detail=str(exc))
    return JSONResponse(content=meta)


@app.get("/{ontology_id}")
async def get_dataset(ontology_id: str):
    """Return the stored dataset for one semantic layer.

    Returns ``{ontologyId, recordCount, uploadedAt, records: [...]}`` or
    ``{ontologyId, recordCount: 0, records: []}`` when none is stored yet so
    the UI can render an empty state without special-casing a 404.
    """
    try:
        envelope = groundtruth_service.get(ontology_id=ontology_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if envelope is None:
        return JSONResponse(
            content={"ontologyId": ontology_id, "recordCount": 0, "records": []}
        )
    return JSONResponse(content=envelope)


@app.delete("/{ontology_id}")
async def delete_dataset(ontology_id: str):
    """Delete the stored dataset for one semantic layer (idempotent)."""
    try:
        groundtruth_service.delete(ontology_id=ontology_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("delete_dataset failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="failed to delete dataset")
    return JSONResponse(content={"deleted": ontology_id})
