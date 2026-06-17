"""Documents API (item #3 — supplementary doc upload + status)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from services.document_service import (
    DocumentService,
    DocumentValidationError,
    MAX_UPLOAD_BYTES,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Documents API")
document_service = DocumentService()


_VALIDATION_TO_STATUS = {
    'unsupported': 415,
    'size': 413,
    'count': 409,
}


def _classify_validation_error(message: str) -> int:
    if 'unsupported file type' in message:
        return 415
    if 'cap' in message and 'byte' in message:
        return 413
    if 'cap' in message:
        return 409
    return 400


@app.post("/{ontology_id}/upload")
async def upload(
    ontology_id: str,
    file: UploadFile = File(...),
):
    """Multipart upload entrypoint. Returns ``{docId, jobId}`` on success."""
    body = await file.read()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"upload exceeds {MAX_UPLOAD_BYTES} byte cap",
        )
    try:
        item = document_service.upload_document(
            ontology_id=ontology_id,
            filename=file.filename or 'unknown',
            body=body,
        )
    except DocumentValidationError as exc:
        raise HTTPException(
            status_code=_classify_validation_error(str(exc)),
            detail=str(exc),
        )
    return JSONResponse(content={'docId': item['docId'], 'jobId': item['docId']})


@app.get("/{ontology_id}")
async def list_docs(ontology_id: str):
    """List docs for one ontology."""
    items = document_service.list_documents(ontology_id=ontology_id)
    return JSONResponse(content={'documents': items})


@app.get("/{ontology_id}/{doc_id}")
async def get_doc(ontology_id: str, doc_id: str):
    """Per-doc status row (used by the upload UI for polling)."""
    item = document_service.get_document(
        ontology_id=ontology_id, doc_id=doc_id
    )
    if not item:
        raise HTTPException(status_code=404, detail='document not found')
    return JSONResponse(content=item)


@app.delete("/{ontology_id}/{doc_id}")
async def delete_doc(ontology_id: str, doc_id: str):
    """Cascade-delete a document (S3 raw + DDB status row)."""
    document_service.delete_document(
        ontology_id=ontology_id, doc_id=doc_id
    )
    return JSONResponse(content={'status': 'deleted', 'docId': doc_id})


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "documents-api"}
