"""Groundtruth-dataset service — store & retrieve per-semantic-layer evaluation
datasets in the AgentCore-Evaluations ground-truth format.

The admin "Ground truth dataset" tab uploads a JSON array of records; each
record must carry the four columns AgentCore ground-truth evaluation expects
(matching ``data/eval/groundtruth_dataset.json`` and the eval notebooks):

    Natural_Language_Question, Expected_Answer, Expected_SQL_Query, Expected_SQL_Result

Datasets are small relative to S3 limits but can be larger than a DynamoDB
item, and they're document-shaped (a list of rows), so we persist them as a
single JSON object in the artifacts bucket keyed by ontology id:

    s3://{ARTIFACTS_BUCKET}/groundtruth/{ontology_id}/dataset.json
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3

logger = logging.getLogger(__name__)

# The four columns AgentCore ground-truth evaluation requires per record. Kept
# in sync with the eval notebooks' REQUIRED_COLS.
REQUIRED_COLUMNS = (
    "Natural_Language_Question",
    "Expected_Answer",
    "Expected_SQL_Query",
    "Expected_SQL_Result",
)

# Defensive cap so a pathological upload can't exhaust Lambda memory. A
# groundtruth dataset is a curated eval set, not bulk data.
MAX_RECORDS = 1000


class GroundtruthValidationError(ValueError):
    """Raised when an uploaded dataset doesn't match the required schema."""


class GroundtruthService:
    """Persist/read per-ontology groundtruth datasets in S3 (artifacts bucket)."""

    def __init__(self, *, bucket: Optional[str] = None, region: Optional[str] = None,
                 s3_client: Any = None) -> None:
        """Configure the service.

        Args:
            bucket: Override the artifacts bucket (defaults to ARTIFACTS_BUCKET env).
            region: AWS region (defaults to AWS_REGION env, then us-east-1).
            s3_client: Pre-built boto3 S3 client (test seam).
        """
        self._bucket = bucket or os.environ.get("ARTIFACTS_BUCKET", "")
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._s3_client = s3_client

    def _get_s3(self):
        """Lazily build (and cache) the S3 client so import never needs creds."""
        if self._s3_client is None:
            self._s3_client = boto3.client("s3", region_name=self._region)
        return self._s3_client

    def _key(self, ontology_id: str) -> str:
        """S3 key for one ontology's groundtruth dataset."""
        return f"groundtruth/{ontology_id}/dataset.json"

    def validate(self, *, records: Any) -> List[Dict[str, Any]]:
        """Validate an uploaded dataset payload, returning the cleaned record list.

        Accepts either a bare JSON array of records, or an object with a
        top-level ``records``/``dataset`` array (tolerant of common shapes).
        Raises GroundtruthValidationError on any schema violation.
        """
        if isinstance(records, dict):
            records = records.get("records") or records.get("dataset") or records.get("rows")
        if not isinstance(records, list):
            raise GroundtruthValidationError(
                "dataset must be a JSON array of records (or {records: [...]})"
            )
        if not records:
            raise GroundtruthValidationError("dataset is empty")
        if len(records) > MAX_RECORDS:
            raise GroundtruthValidationError(
                f"dataset has {len(records)} records; max is {MAX_RECORDS}"
            )
        for i, row in enumerate(records):
            if not isinstance(row, dict):
                raise GroundtruthValidationError(f"record {i} is not an object")
            missing = [c for c in REQUIRED_COLUMNS if c not in row]
            if missing:
                raise GroundtruthValidationError(
                    f"record {i} missing required column(s): {', '.join(missing)}"
                )
        return records

    def put(self, *, ontology_id: str, records: Any) -> Dict[str, Any]:
        """Validate then store a dataset for one ontology. Returns metadata."""
        if not self._bucket:
            raise RuntimeError("ARTIFACTS_BUCKET env var is empty")
        cleaned = self.validate(records=records)
        now = datetime.now(timezone.utc).isoformat()
        body = json.dumps(
            {
                "ontologyId": ontology_id,
                "uploadedAt": now,
                "recordCount": len(cleaned),
                "records": cleaned,
            },
            indent=2,
        ).encode("utf-8")
        self._get_s3().put_object(
            Bucket=self._bucket, Key=self._key(ontology_id), Body=body,
            ContentType="application/json",
        )
        logger.info(
            "Stored groundtruth dataset for %s (%d records)", ontology_id, len(cleaned)
        )
        return {"ontologyId": ontology_id, "recordCount": len(cleaned), "uploadedAt": now}

    def get(self, *, ontology_id: str) -> Optional[Dict[str, Any]]:
        """Return the stored dataset envelope for one ontology, or None if absent."""
        if not self._bucket:
            raise RuntimeError("ARTIFACTS_BUCKET env var is empty")
        s3 = self._get_s3()
        try:
            obj = s3.get_object(Bucket=self._bucket, Key=self._key(ontology_id))
        except s3.exceptions.NoSuchKey:
            return None
        except Exception as exc:  # noqa: BLE001 — treat any read miss as "no dataset yet"
            # A 404-style ClientError also lands here on some S3 configs.
            if "NoSuchKey" in str(exc) or "Not Found" in str(exc):
                return None
            raise
        return json.loads(obj["Body"].read().decode("utf-8"))

    def delete(self, *, ontology_id: str) -> None:
        """Remove a stored dataset (idempotent)."""
        if not self._bucket:
            raise RuntimeError("ARTIFACTS_BUCKET env var is empty")
        self._get_s3().delete_object(Bucket=self._bucket, Key=self._key(ontology_id))
        logger.info("Deleted groundtruth dataset for %s", ontology_id)
