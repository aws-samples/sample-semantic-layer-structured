"""Evaluation-results service — store & list OnDemand evaluation-pipeline runs
per semantic layer, backing the admin "Evaluations" tab.

When a new semantic-layer version reaches ``completed``, the agents emit an
``evaluation.requested`` EventBridge event (see ``emit_evaluation_requested``
in the metadata/ontology agents). A downstream eval-runner evaluates the
matching query agent against the layer's maintained ground-truth dataset and
writes a result envelope here. The admin tab reads these envelopes.

Each run is a JSON object in the artifacts bucket, keyed by layer + run id:

    s3://{ARTIFACTS_BUCKET}/evaluations/{ontology_id}/{run_id}.json

A run envelope captures the per-question metrics (accuracy / latency /
input+output tokens) plus a roll-up summary — the same metrics the eval
notebooks produce, so the tab and the notebooks tell the same story.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3

logger = logging.getLogger(__name__)


def _safe_mean(values: List[float]) -> Optional[float]:
    """Mean of a numeric list, or None when empty (avoids div-by-zero)."""
    nums = [v for v in values if isinstance(v, (int, float))]
    return (sum(nums) / len(nums)) if nums else None


class EvaluationService:
    """Persist/read per-layer OnDemand evaluation runs in S3 (artifacts bucket)."""

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

    def _prefix(self, ontology_id: str) -> str:
        """S3 prefix holding all runs for one layer."""
        return f"evaluations/{ontology_id}/"

    def _key(self, ontology_id: str, run_id: str) -> str:
        """S3 key for one evaluation run."""
        return f"{self._prefix(ontology_id)}{run_id}.json"

    def summarize(self, *, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Roll per-question rows up into headline metrics.

        Each row may carry ``passed`` (bool), ``accuracy`` (0-1), ``latency_s``,
        and token counts (``agent_in_tokens`` / ``agent_out_tokens`` /
        ``agent_total_tokens``). Missing fields are tolerated.
        """
        rows = results or []
        passed = [bool(r.get("passed")) for r in rows]
        return {
            "rows": len(rows),
            "passRate": (sum(passed) / len(passed)) if passed else None,
            "avgAccuracy": _safe_mean([r.get("accuracy") for r in rows]),
            "avgLatencyS": _safe_mean([r.get("latency_s") for r in rows]),
            "totalInputTokens": sum(int(r.get("agent_in_tokens") or 0) for r in rows),
            "totalOutputTokens": sum(int(r.get("agent_out_tokens") or 0) for r in rows),
            "totalTokens": sum(int(r.get("agent_total_tokens") or 0) for r in rows),
        }

    def put_run(self, *, ontology_id: str, run: Dict[str, Any]) -> Dict[str, Any]:
        """Store one evaluation run; returns the stored envelope (with summary).

        The ``run`` dict should carry at least ``results`` (the per-question
        rows). ``runId``, ``createdAt``, and ``summary`` are filled in if absent.
        """
        if not self._bucket:
            raise RuntimeError("ARTIFACTS_BUCKET env var is empty")
        now = datetime.now(timezone.utc).isoformat()
        run_id = run.get("runId") or now.replace(":", "").replace("-", "").replace(".", "")
        results = run.get("results") or []
        envelope = {
            "ontologyId": ontology_id,
            "runId": run_id,
            "createdAt": run.get("createdAt") or now,
            "version": run.get("version") or "",
            "layerType": run.get("layerType") or "",
            "agentId": run.get("agentId") or "",
            "datasetRecordCount": run.get("datasetRecordCount"),
            "status": run.get("status") or "completed",
            "summary": run.get("summary") or self.summarize(results=results),
            "results": results,
        }
        self._get_s3().put_object(
            Bucket=self._bucket, Key=self._key(ontology_id, run_id),
            Body=json.dumps(envelope, indent=2, default=str).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info(
            "Stored evaluation run %s for %s (%d rows)", run_id, ontology_id, len(results)
        )
        return envelope

    def list_runs(self, *, ontology_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Return run *summaries* (no per-question rows) for one layer, newest first."""
        if not self._bucket:
            raise RuntimeError("ARTIFACTS_BUCKET env var is empty")
        s3 = self._get_s3()
        keys: List[str] = []
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix=self._prefix(ontology_id)
        ):
            keys.extend(o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".json"))
        summaries: List[Dict[str, Any]] = []
        for key in keys:
            try:
                obj = s3.get_object(Bucket=self._bucket, Key=key)
                env = json.loads(obj["Body"].read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 — skip an unreadable run, don't fail the list
                logger.warning("skipping unreadable eval run %s: %s", key, exc)
                continue
            # Drop the heavy per-row results from the list view.
            summaries.append({k: v for k, v in env.items() if k != "results"})
        summaries.sort(key=lambda e: e.get("createdAt") or "", reverse=True)
        return summaries[:limit]

    def get_run(self, *, ontology_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        """Return one full run envelope (incl. per-question rows), or None if absent."""
        if not self._bucket:
            raise RuntimeError("ARTIFACTS_BUCKET env var is empty")
        s3 = self._get_s3()
        try:
            obj = s3.get_object(Bucket=self._bucket, Key=self._key(ontology_id, run_id))
        except s3.exceptions.NoSuchKey:
            return None
        except Exception as exc:  # noqa: BLE001
            if "NoSuchKey" in str(exc) or "Not Found" in str(exc):
                return None
            raise
        return json.loads(obj["Body"].read().decode("utf-8"))

    def delete_run(self, *, ontology_id: str, run_id: str) -> None:
        """Remove one stored run (idempotent)."""
        if not self._bucket:
            raise RuntimeError("ARTIFACTS_BUCKET env var is empty")
        self._get_s3().delete_object(Bucket=self._bucket, Key=self._key(ontology_id, run_id))
        logger.info("Deleted evaluation run %s for %s", run_id, ontology_id)
