"""FastAPI router for governed-rule authoring, review and publication.

Routes are namespace-scoped (``/namespaces/{ns}/rules``) exactly like the
metrics router, so the admin UI surfaces them per ontology. ``build_router``
takes a factory so tests inject a MagicMock while the live app wires a real
RuleService against Neptune.

Two things are enforced HERE rather than in the UI, because a disabled button
is not a control: lifecycle transitions only happen through the ``:approve`` /
``:publish`` routes, and a blocked publish returns 422 with the gate's reason.
"""
from typing import Callable, Dict, List

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agents.shared.rule_graph import RuleReadError
from agents.shared.rule_models import Rule
from agents.shared.rule_publisher import PublishBlocked
from services.rule_service import RuleService


class RuleListResponse(BaseModel):
    """Envelope for the rule list.

    An envelope rather than a bare array because an unreadable rule must be
    REPORTED, not omitted: a rule silently missing from the list is the failure
    mode that made the threshold-IRI incident look like data loss.
    """

    rules: List[Rule] = Field(..., description="Every rule that could be read")
    unreadable: List[Dict[str, str]] = Field(
        default_factory=list,
        description=(
            "One {rule_id, error} entry per rule that is stored but cannot be "
            "deserialized. Non-empty means the graph is corrupt, not that the "
            "request was bad."
        ),
    )


def rule_read_error_handler(
    request: Request, exc: Exception,
) -> JSONResponse:
    """Answer 500 with the rule named, for a rule that is stored but unreadable.

    500, not 422: the payload was fine and the caller cannot fix this. Naming the
    rule in the body is the whole point of the strict reader — the original
    incident surfaced as an ``AttributeError`` that named nothing.

    :param request: The failing request (unused; required by the Starlette hook).
    :param exc: The ``RuleReadError`` raised while reading.
    :returns: A 500 response carrying the message and the rule id.
    """
    rule_id = getattr(exc, "rule_id", None)
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "rule_id": rule_id},
    )


def register_rule_error_handlers(app: FastAPI) -> None:
    """Install the rules router's app-level exception handlers.

    Registered once on the app rather than caught in each route: seven routes
    resolve a rule and can raise ``RuleReadError``, and a per-route ``except``
    would be silently forgotten by the eighth.

    :param app: The FastAPI application the rules router is mounted on.
    :returns: None.
    """
    app.add_exception_handler(RuleReadError, rule_read_error_handler)


class ApprovalBody(BaseModel):
    """Request body for the two human-approval routes."""

    approver: str = Field(..., description="Identity of the approving human")


class ImportBody(BaseModel):
    """Request body for the mined-artefact import route."""

    key: str = Field(
        ...,
        description=(
            "S3 key of the mined-rules artefact, as reported by the doc "
            "pipeline's minedRulesKey output"
        ),
    )


def build_router(svc_factory: Callable[[], RuleService]) -> APIRouter:
    """Build the rules router.

    :param svc_factory: Zero-arg callable returning a ``RuleService``.
    :returns: A configured ``APIRouter``.
    """
    r = APIRouter(tags=["rules"])

    def svc() -> RuleService:  # nosemgrep: useless-inner-function — FastAPI DI factory, invoked by Depends()
        return svc_factory()

    @r.post("/namespaces/{ns}/rules", status_code=201, response_model=Rule)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def create(ns: str, body: Rule, s: RuleService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        if body.namespace != ns:
            raise HTTPException(400, "namespace mismatch between path and body")
        try:
            return s.create(rule=body)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @r.post("/namespaces/{ns}/rules:import", status_code=201)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def import_mined(ns: str, body: ImportBody, s: RuleService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        """Import a doc-pipeline artefact as DRAFT rules, nothing approved."""
        try:
            rules = s.import_mined(ontology_id=ns, key=body.key)
        except KeyError:
            # The artefact key does not exist — a stale minedRulesKey, or a
            # document whose pipeline run mined nothing.
            raise HTTPException(404, "mined-rules artefact not found")
        except ValueError as e:
            # 422 with the reason: a malformed artefact is the producer's bug,
            # but the admin still needs to know WHICH one is unusable.
            raise HTTPException(422, str(e))
        return {
            "rules": [rule.model_dump(mode="json") for rule in rules],
            "count": len(rules),
        }

    @r.get("/namespaces/{ns}/rules", response_model=RuleListResponse)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def list_(ns: str, s: RuleService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        # Partitioned, not strict: one corrupt rule used to 500 the whole
        # namespace and blank the Governed Rules tab.
        rules, unreadable = s.list_partitioned(namespace=ns)
        return {"rules": rules, "unreadable": unreadable}

    @r.get("/namespaces/{ns}/rules/{rule_id}", response_model=Rule)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def get_(ns: str, rule_id: str, s: RuleService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        out = s.get(namespace=ns, rule_id=rule_id)
        if out is None:
            raise HTTPException(404, "rule not found")
        return out

    @r.put("/namespaces/{ns}/rules/{rule_id}", response_model=Rule)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def update(ns: str, rule_id: str, body: Rule, s: RuleService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        if body.namespace != ns or body.rule_id != rule_id:
            raise HTTPException(400, "path/body identifier mismatch")
        try:
            return s.update(rule=body)
        except KeyError:
            raise HTTPException(404, "rule not found")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @r.post(  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
        "/namespaces/{ns}/rules/{rule_id}/thresholds/{threshold_id}:approve",
        response_model=Rule,
    )
    def approve_threshold(  # nosemgrep: useless-inner-function — registered as route handler by decorator
        ns: str,
        rule_id: str,
        threshold_id: str,
        body: ApprovalBody,
        s: RuleService = Depends(svc),
    ):
        try:
            return s.approve_threshold(
                namespace=ns, rule_id=rule_id, threshold_id=threshold_id,
                approver=body.approver,
            )
        except KeyError:
            raise HTTPException(404, "rule or threshold not found")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @r.post("/namespaces/{ns}/rules/{rule_id}:approve", response_model=Rule)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def approve(ns: str, rule_id: str, body: ApprovalBody, s: RuleService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        try:
            return s.approve(namespace=ns, rule_id=rule_id, approver=body.approver)
        except KeyError:
            raise HTTPException(404, "rule not found")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @r.post("/namespaces/{ns}/rules/{rule_id}:validate")  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def validate(ns: str, rule_id: str, s: RuleService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        try:
            return s.validate(namespace=ns, rule_id=rule_id)
        except KeyError:
            raise HTTPException(404, "rule not found")

    @r.post("/namespaces/{ns}/rules/{rule_id}:publish", response_model=Rule)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def publish(ns: str, rule_id: str, s: RuleService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        try:
            return s.publish_rule(namespace=ns, rule_id=rule_id)
        except KeyError:
            raise HTTPException(404, "rule not found")
        except PublishBlocked as e:
            # 422, not 500: the request was well-formed, the RULE was not ready.
            # The reason is passed through so the reviewer can act on it.
            raise HTTPException(422, str(e))

    @r.delete("/namespaces/{ns}/rules/{rule_id}", status_code=204)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def delete(ns: str, rule_id: str, s: RuleService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        s.delete(namespace=ns, rule_id=rule_id)

    return r
