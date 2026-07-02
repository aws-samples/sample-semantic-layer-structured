"""Monitoring API — per-layer production-signal breakdown for the admin tab.

Surfaces, for one semantic layer's LIVE query traffic, how queries resolved
across the four resolution layers (metric / semantic / advisory / agentic) plus
the share of user turns that used correction language, correlated with the
lessons AgentCore Memory has extracted. All read-only; the aggregation lives in
``services/monitoring_service.MonitoringService``.

Route shape mirrors the other per-ontology sub-apps (``/evaluations/<id>`` etc.):
the layer id is the path parameter and auth is enforced upstream by the API
Gateway JWT authorizer (this Lambda is the single ``/{proxy+}`` integration).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from services.monitoring_service import MonitoringService

logger = logging.getLogger(__name__)

app = FastAPI(title="Monitoring API")
monitoring_service = MonitoringService()


@app.get("/{ontology_id}")
async def get_breakdown(ontology_id: str):
    """Return the resolution + correction-language breakdown for one layer.

    Always returns a 200 with a fully-shaped envelope (zeros when nothing has
    been queried yet, or when the chat-sessions table / memory aren't wired) so
    the tab renders an honest empty state rather than an error.
    """
    try:
        breakdown = monitoring_service.aggregate(ontology_id=ontology_id)
    except Exception as exc:  # noqa: BLE001 — translate to HTTP error
        logger.error(
            "monitoring aggregate failed for %s: %s", ontology_id, exc, exc_info=True
        )
        raise HTTPException(status_code=500, detail="failed to aggregate monitoring data")
    return JSONResponse(content=breakdown)

# NOTE: deliberately no "/health" route here. The "/{ontology_id}" path param
# above is greedy, so a "/health" route declared after it is unreachable (it
# would resolve to get_breakdown(ontology_id="health") and Scan for a layer
# named "health"). The app-level health check lives at main.py "/health".
