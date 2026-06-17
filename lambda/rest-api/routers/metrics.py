"""FastAPI router for governed-metric authoring + lifecycle.

Routes are namespace-scoped (``/namespaces/{ns}/metrics``) so the admin
UI can surface them per ontology. ``build_router`` takes a factory so
test code can inject a MagicMock service while the live app wires a
real one against DDB + OpenSearch.
"""
from typing import Callable, List

from fastapi import APIRouter, Depends, HTTPException

from agents.shared.metric_models import Metric
from services.metric_service import MetricService


def build_router(svc_factory: Callable[[], MetricService]) -> APIRouter:
    """Build the metrics router. The factory keeps testability and live
    wiring symmetric — tests pass a lambda returning a MagicMock; the
    live app passes a closure that constructs a real MetricService."""
    r = APIRouter(tags=["metrics"])

    def svc() -> MetricService:  # nosemgrep: useless-inner-function — FastAPI DI factory, invoked by Depends()
        return svc_factory()

    @r.post("/namespaces/{ns}/metrics", status_code=201, response_model=Metric)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def create(ns: str, body: Metric, s: MetricService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        if body.namespace != ns:
            raise HTTPException(400, "namespace mismatch between path and body")
        try:
            return s.create(body)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @r.get("/namespaces/{ns}/metrics", response_model=List[Metric])  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def list_(ns: str, s: MetricService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        return s.list(namespace=ns)

    @r.get("/namespaces/{ns}/metrics/{metric_id}", response_model=Metric)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def get_(ns: str, metric_id: str, s: MetricService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        out = s.get(namespace=ns, metric_id=metric_id)
        if out is None:
            raise HTTPException(404, "metric not found")
        return out

    @r.put("/namespaces/{ns}/metrics/{metric_id}", response_model=Metric)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def update(ns: str, metric_id: str, body: Metric, s: MetricService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        if body.namespace != ns or body.metric_id != metric_id:
            raise HTTPException(400, "path/body identifier mismatch")
        try:
            return s.update(body)
        except KeyError:
            raise HTTPException(404, "metric not found")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @r.post("/namespaces/{ns}/metrics/{metric_id}:publish", response_model=Metric)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def publish(ns: str, metric_id: str, s: MetricService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        try:
            return s.publish(namespace=ns, metric_id=metric_id)
        except KeyError:
            raise HTTPException(404, "metric not found")

    @r.delete("/namespaces/{ns}/metrics/{metric_id}", status_code=204)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def delete(ns: str, metric_id: str, s: MetricService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        s.delete(namespace=ns, metric_id=metric_id)

    return r
