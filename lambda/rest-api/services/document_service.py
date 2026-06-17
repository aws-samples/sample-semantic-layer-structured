"""Document upload + status tracking (item #3).

Backs ``POST /documents`` (upload), ``GET /documents/{docId}`` (status),
``GET /documents/{docId}/chunks`` (list chunks), ``DELETE`` (cascade).

Status records live alongside ontology metadata in ``ONTOLOGY_METADATA_TABLE``
under SK prefix ``DOCJOB#`` so we don't need a new table for the day-one slice.
The pipeline (Step Functions) updates the per-stage status booleans.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

# Per-upload size cap (50 MB by default — design doc default).
MAX_UPLOAD_BYTES = int(os.environ.get('DOC_PIPELINE_MAX_UPLOAD_BYTES', str(50 * 1024 * 1024)))

# Per-ontology document cap.
MAX_DOCS_PER_ONTOLOGY = int(os.environ.get('DOC_PIPELINE_MAX_DOCS_PER_ONTOLOGY', '100'))

_SUPPORTED_EXTS = ('.txt', '.md', '.markdown', '.pdf', '.docx')


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DocumentValidationError(ValueError):
    """Raised on size cap, count cap, or unsupported file type."""


class DocumentService:
    """Upload + status persistence."""

    def __init__(
        self,
        *,
        table_name: Optional[str] = None,
        bucket: Optional[str] = None,
        region: Optional[str] = None,
        state_machine_arn: Optional[str] = None,
        ddb_resource: Any = None,
        s3_client: Any = None,
        sfn_client: Any = None,
    ) -> None:
        self._table_name = table_name or os.environ.get(
            'ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata'
        )
        self._bucket = bucket or os.environ.get('ARTIFACTS_BUCKET', '')
        self._region = region or os.environ.get('AWS_REGION', 'us-east-1')
        # Doc-pipeline state machine ARN — when set, upload_document fires a
        # state-machine execution after persisting the doc. Empty string
        # short-circuits (e.g. local dev where the SF stack isn't deployed).
        self._state_machine_arn = state_machine_arn or os.environ.get(
            'DOC_PIPELINE_STATE_MACHINE_ARN', ''
        )
        self._ddb_resource = ddb_resource
        self._s3_client = s3_client
        self._sfn_client = sfn_client
        self._table = None

    def _get_table(self):
        if self._table is None:
            resource = self._ddb_resource or boto3.resource(
                'dynamodb', region_name=self._region
            )
            self._table = resource.Table(self._table_name)
        return self._table

    def _get_s3(self):
        if self._s3_client is None:
            self._s3_client = boto3.client('s3', region_name=self._region)
        return self._s3_client

    def _get_sfn(self):
        if self._sfn_client is None:
            self._sfn_client = boto3.client(
                'stepfunctions', region_name=self._region
            )
        return self._sfn_client

    # ---------------------------------------------------------------
    # Validation
    # ---------------------------------------------------------------

    def _validate_extension(self, *, filename: str) -> None:
        """Reject unsupported file types upfront (HTTP 415 in the API)."""
        lower = filename.lower()
        if not any(lower.endswith(ext) for ext in _SUPPORTED_EXTS):
            raise DocumentValidationError(
                f"unsupported file type: {filename!r}; supported: {_SUPPORTED_EXTS}"
            )

    def _validate_size(self, *, byte_length: int) -> None:
        """Reject oversize uploads (HTTP 413)."""
        if byte_length > MAX_UPLOAD_BYTES:
            raise DocumentValidationError(
                f"upload exceeds {MAX_UPLOAD_BYTES} byte cap"
            )

    def _validate_count(self, *, ontology_id: str) -> None:
        """Reject when the per-ontology doc cap is reached (HTTP 409)."""
        existing = self.list_documents(ontology_id=ontology_id)
        if len(existing) >= MAX_DOCS_PER_ONTOLOGY:
            raise DocumentValidationError(
                f"ontology already has {len(existing)} docs (cap {MAX_DOCS_PER_ONTOLOGY})"
            )

    # ---------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------

    def upload_document(
        self,
        *,
        ontology_id: str,
        filename: str,
        body: bytes,
    ) -> Dict[str, Any]:
        """Persist the document to S3 and stamp an initial status row."""
        self._validate_extension(filename=filename)
        self._validate_size(byte_length=len(body))
        self._validate_count(ontology_id=ontology_id)

        if not self._bucket:
            raise RuntimeError("ARTIFACTS_BUCKET env var is empty")

        doc_id = str(uuid.uuid4())
        # S3 layout: ontology-prefixed so cleanup/cascade-delete is easy.
        s3_key = f"supplementary-docs/{ontology_id}/raw/{doc_id}/{filename}"
        self._get_s3().put_object(Bucket=self._bucket, Key=s3_key, Body=body)

        item = {
            'id': ontology_id,
            'version': f"DOCJOB#{doc_id}",
            'docId': doc_id,
            'ontologyId': ontology_id,
            'filename': filename,
            'sizeBytes': len(body),
            's3Bucket': self._bucket,
            's3Key': s3_key,
            'createdAt': _now_iso(),
            'updatedAt': _now_iso(),
            'stages': {
                'chunked': False,
                'ner': False,
                'embedded': False,
                'linked': False,
                'indexed': False,
            },
            'errors': {},
        }
        self._get_table().put_item(Item=item)

        # Kick off the doc-pipeline state machine. Failures are logged and
        # the upload still succeeds (status row stays in pre-pipeline state)
        # so the steward can retry by re-uploading. The execution input is
        # the chunker's expected payload shape.
        if self._state_machine_arn:
            try:
                self._get_sfn().start_execution(
                    stateMachineArn=self._state_machine_arn,
                    name=f"doc-{doc_id}",
                    input=json.dumps({
                        'docId': doc_id,
                        'ontologyId': ontology_id,
                        's3Bucket': self._bucket,
                        's3Key': s3_key,
                        'filename': filename,
                    }),
                )
                item['executionStarted'] = True
            except Exception as exc:  # noqa: BLE001 — never block the upload
                logger.warning(
                    'doc-pipeline start_execution failed for %s: %s',
                    doc_id, exc,
                )
                item['executionStarted'] = False
                item['executionError'] = str(exc)
        return item

    def get_document(
        self, *, ontology_id: str, doc_id: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch the per-doc status row."""
        resp = self._get_table().get_item(
            Key={'id': ontology_id, 'version': f"DOCJOB#{doc_id}"}
        )
        return resp.get('Item')

    def list_documents(self, *, ontology_id: str) -> List[Dict[str, Any]]:
        """List docs for one ontology."""
        resp = self._get_table().query(
            KeyConditionExpression=Key('id').eq(ontology_id)
            & Key('version').begins_with('DOCJOB#'),
        )
        return resp.get('Items', [])

    def update_stage(
        self,
        *,
        ontology_id: str,
        doc_id: str,
        stage: str,
        success: bool,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Step Functions calls this after each stage."""
        if stage not in ('chunked', 'ner', 'embedded', 'linked', 'indexed'):
            raise ValueError(f"unknown stage: {stage}")
        update_expr = (
            'SET stages.#s = :v, updatedAt = :u'
        )
        eav = {':v': success, ':u': _now_iso()}
        if not success and error is not None:
            update_expr += ', errors.#s = :e'
            eav[':e'] = error
        resp = self._get_table().update_item(
            Key={'id': ontology_id, 'version': f"DOCJOB#{doc_id}"},
            UpdateExpression=update_expr,
            ExpressionAttributeNames={'#s': stage},
            ExpressionAttributeValues=eav,
            ReturnValues='ALL_NEW',
        )
        return resp.get('Attributes', {})

    def delete_document(
        self, *, ontology_id: str, doc_id: str
    ) -> None:
        """Remove the status row and the raw S3 object. Chunks/embeddings
        live elsewhere and are cleaned up by the pipeline owner."""
        item = self.get_document(ontology_id=ontology_id, doc_id=doc_id)
        if not item:
            return
        try:
            self._get_s3().delete_object(
                Bucket=item['s3Bucket'], Key=item['s3Key']
            )
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.warning(
                "failed to delete S3 object during cascade: %s", exc
            )
        self._get_table().delete_item(
            Key={'id': ontology_id, 'version': f"DOCJOB#{doc_id}"}
        )
