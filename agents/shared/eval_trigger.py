"""Shared helper to request an OnDemand evaluation when a semantic-layer
version completes.

Both build agents (metadata enrichment → SemanticRAG, ontology → VKG) call
``emit_evaluation_requested`` right after a version reaches ``completed``. It
emits an EventBridge ``evaluation.requested`` event carrying the layer id,
version, and type. A downstream eval-runner (subscribed to this event) runs the
matching query agent against the layer's maintained ground-truth dataset and
POSTs the per-question metrics to ``/evaluations/{id}`` (see
``services/evaluation_service.py``), which the admin "Evaluations" tab reads.

This is intentionally a thin event emit, not the evaluation itself: it mirrors
the existing ``ontology.published`` pattern, keeps the agent's critical path
fast, and decouples the (slow, multi-minute) eval run from the build.

The query agent to use is chosen by layer type, expressed here as ``query_kind``
so the runner doesn't have to re-derive it:
    SemanticRAG -> "metadata_query"
    VKG         -> "ontology_query"
"""

from __future__ import annotations

import json
import logging
import os

import boto3

logger = logging.getLogger(__name__)

# Map semantic-layer type → which query agent the eval-runner should drive.
_QUERY_KIND_BY_TYPE = {
    "SemanticRAG": "metadata_query",
    "VKG": "ontology_query",
}


def emit_evaluation_requested(
    *,
    ontology_id: str,
    version: str,
    layer_type: str,
    boto_session: "boto3.Session | None" = None,
) -> bool:
    """Emit an ``evaluation.requested`` EventBridge event for a completed layer.

    Args:
        ontology_id: The semantic-layer / config id that just completed.
        version: The version that completed (e.g. ``v1``, ``v2``).
        layer_type: ``SemanticRAG`` or ``VKG`` — selects the query agent.
        boto_session: Optional pre-built session (the agents inject their
            credential-scoped session; falls back to a default session).

    Returns:
        True if the event was emitted, False on any failure (never raises — a
        build must not be rolled back because the event bus is unhappy).
    """
    query_kind = _QUERY_KIND_BY_TYPE.get(layer_type, "metadata_query")
    bus_name = os.environ.get("EVAL_EVENT_BUS_NAME", "")  # "" → account default bus
    try:
        session = boto_session or boto3.Session()
        client = session.client(
            "events", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
        entry = {
            "Source": "semantic-layer.evaluation",
            "DetailType": "evaluation.requested",
            "Detail": json.dumps(
                {
                    "ontology_id": ontology_id,
                    "version": version,
                    "layer_type": layer_type,
                    "query_kind": query_kind,
                }
            ),
        }
        if bus_name:
            entry["EventBusName"] = bus_name
        client.put_events(Entries=[entry])
        logger.info(
            "[EventBridge] Emitted evaluation.requested for %s (version=%s, "
            "type=%s, query=%s)",
            ontology_id,
            version,
            layer_type,
            query_kind,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort, never break the build
        logger.error(
            "[EventBridge] Failed to emit evaluation.requested for %s: %s",
            ontology_id,
            exc,
        )
        return False
