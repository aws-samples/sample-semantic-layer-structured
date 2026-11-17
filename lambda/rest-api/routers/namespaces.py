"""FastAPI router for namespace lifecycle and grant administration.

Mounted at the root (``/namespaces``) rather than under a prefix, because the
namespace is the top-level governance boundary — rules, metrics and ontologies
hang off it, not the reverse.

Every route here is gated by :class:`~authz.middleware.AuthzMiddleware` before
the handler runs, so these functions do no authorization of their own. The one
exception is ``GET /namespaces``: the middleware authorizes the *listing*, and
this router filters the *result* down to the namespaces the caller may see, so
discovery does not become a directory of everything.

``build_router`` takes a factory to match ``routers/metrics.py`` — tests inject a
service built over a fake table while the live app passes a real one.
"""
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from authz.cedar_schema import Action, NamespaceStatus, Role
from authz.middleware import (
    authz_enabled,
    extract_claims,
    get_authz_context,
    parse_groups,
)
from authz.policy_evaluator import evaluate, load_policies
from authz.role_resolver import (
    ResolvedRoles,
    RoleResolutionError,
    resolve_roles,
)
from services.namespace_service import (
    Grant,
    GrantCreate,
    InvalidNamespaceName,
    InvalidStatusTransition,
    Namespace,
    NamespaceCreate,
    NamespaceNameTaken,
    NamespaceNotFound,
    NamespaceService,
    NamespaceUpdate,
    StatusUpdate,
)
from services.obo_middleware import get_principal


class CallerAuthz(BaseModel):
    """The calling principal's resolved authorization state.

    Attributes:
        enforced: Whether ``ENABLE_AUTHZ`` is on. The role fields are populated
            either way, but when false a client must not act on them: nothing is
            being denied, so an empty role set does not mean "no access".
        userId: The JWT ``sub``, or ``''``.
        email: The ``email`` claim, or ``''``.
        groups: ``cognito:groups`` values.
        globalRoles: Platform-scoped roles, from group mapping and GLOBAL grants.
        resourceRoles: ``[{"role": …, "resourceUID": …}]`` namespace-scoped roles.
    """

    enforced: bool
    userId: str = ''
    email: str = ''
    groups: List[str] = []
    globalRoles: List[str] = []
    resourceRoles: List[Dict[str, str]] = []


def _resolve_caller(*, request: Request) -> Dict[str, Any]:
    """Resolve the calling principal's roles for ``GET /namespaces/me``.

    Prefers the context the middleware already published, so an enforced request
    costs no extra grants read. When enforcement is off the middleware returns
    before resolving anything, so this falls back to resolving from the verified
    JWT claims directly — otherwise the endpoint would report "no roles" on
    exactly the deployment where an operator most needs to check whether arming
    enforcement is about to lock them out.

    Args:
        request: The incoming request.

    Returns:
        A mapping with ``userId``, ``email``, ``groups``, ``globalRoles`` and
        ``resourceRoles``; empty when the request carried no verified principal.

    Raises:
        HTTPException: 503 if the grants table could not be read. Reporting an
            empty role set instead would be indistinguishable from a principal
            who genuinely holds nothing.
    """
    context = get_authz_context(request)
    if context:
        return context

    claims = extract_claims(scope=request.scope)
    user_id = str(claims.get('sub') or '')
    if not user_id:
        # No verified principal: local uvicorn, or a route reached without the
        # JWT authorizer. Nothing to resolve, and nothing to report.
        return {}

    email = str(claims.get('email') or '')
    groups = parse_groups(raw=claims.get('cognito:groups'))
    try:
        # namespace='' — the platform scope, matching what the middleware passes
        # for a PLATFORM-scoped route. Namespace-scoped grants still come back in
        # `resource_roles`; only data restrictions are namespace-filtered.
        roles: ResolvedRoles = resolve_roles(
            user_id=user_id, email=email, groups=groups, namespace=''
        )
    except RoleResolutionError as exc:
        raise HTTPException(503, 'Authorization unavailable') from exc

    return {
        'userId': user_id,
        'email': email,
        'groups': groups,
        'globalRoles': roles.global_roles,
        'resourceRoles': roles.resource_roles,
    }


def _may_view(*, context: Dict[str, Any], namespace: Namespace) -> bool:
    """Decide whether the caller may see one namespace in a listing.

    Re-evaluates ``viewNamespace`` per namespace using the roles the middleware
    already resolved, so no extra grants read happens per row. Callers with no
    role see an empty list rather than a 403 — the list action itself was
    authorized; its contents are simply empty.

    Args:
        context: The published ``scope['authz']`` mapping.
        namespace: The namespace being considered.

    Returns:
        True when the caller holds ``viewNamespace`` on the namespace.
    """
    global_roles: List[str] = list(context.get('globalRoles') or [])
    resource_roles: List[Dict[str, str]] = list(context.get('resourceRoles') or [])
    role_names = sorted(
        {*global_roles, *(str(entry.get('role') or '') for entry in resource_roles)}
    )
    decision = evaluate(
        action=Action.VIEW_NAMESPACE.value,
        user_id=str(context.get('userId') or ''),
        resource_id=namespace.id,
        groups=list(context.get('groups') or []),
        global_roles=global_roles,
        resource_roles=resource_roles,
        context={'namespaceStatus': namespace.status},
        policies=load_policies(roles=role_names),
    )
    return decision.allowed


def build_router(svc_factory: Callable[[], NamespaceService]) -> APIRouter:
    """Build the namespaces router.

    Args:
        svc_factory: Zero-argument callable returning a
            :class:`~services.namespace_service.NamespaceService`.

    Returns:
        The configured ``APIRouter``.
    """
    r = APIRouter(tags=["namespaces"])

    def svc() -> NamespaceService:  # nosemgrep: useless-inner-function — FastAPI DI factory, invoked by Depends()
        return svc_factory()

    @r.post("/namespaces", status_code=201, response_model=Namespace)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def create(  # nosemgrep: useless-inner-function — registered as route handler by decorator
        request: Request,
        body: NamespaceCreate,
        s: NamespaceService = Depends(svc),
    ):
        principal = get_principal(request)
        if not principal['userId']:
            # Creating a namespace with no owner would produce a namespace only
            # a platform-admin could ever administer.
            raise HTTPException(401, "no verified principal on the request")
        try:
            return s.create(
                request=body,
                created_by=principal['userId'],
                created_by_email=principal['email'],
            )
        except InvalidNamespaceName as e:
            raise HTTPException(422, str(e))
        except NamespaceNameTaken as e:
            raise HTTPException(409, str(e))

    @r.get("/namespaces", response_model=List[Namespace])  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def list_(request: Request, s: NamespaceService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        found = s.list()
        context = get_authz_context(request)
        if not context:
            # Enforcement is off; there is no resolved identity to filter by.
            return found
        return [ns for ns in found if _may_view(context=context, namespace=ns)]

    # MUST precede `/namespaces/{ns}`: FastAPI matches in declaration order, so
    # declared after it, `me` would be captured as a namespace identifier and
    # this route would be unreachable.
    @r.get("/namespaces/me", response_model=CallerAuthz)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def me(request: Request):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        """Report the calling principal's resolved roles.

        Exists for the frontend. A browser can decode its own ID token and see
        ``cognito:groups``, but group membership only ever confers a PLATFORM
        role — every namespace-scoped role comes from a grant row the browser
        cannot see. A UI that hid admin surfaces on the token alone would
        therefore hide them from exactly the namespace owners and maintainers who
        hold their access by grant.

        Reuses the context the middleware published when enforcement is on, so an
        enforced request costs no additional grants read; with enforcement off it
        resolves the caller directly (see :func:`_resolve_caller`).

        Args:
            request: The incoming request, carrying the published authz context.

        Returns:
            The caller's roles, plus whether enforcement is switched on at all.
            The roles are reported in both states, but a UI must not treat an
            empty set as "this user may do nothing" while ``enforced`` is false,
            because in that state nothing is denied.
        """
        context = _resolve_caller(request=request)
        return CallerAuthz(
            enforced=authz_enabled(),
            userId=str(context.get('userId') or ''),
            email=str(context.get('email') or ''),
            groups=list(context.get('groups') or []),
            globalRoles=list(context.get('globalRoles') or []),
            resourceRoles=list(context.get('resourceRoles') or []),
        )

    @r.get("/namespaces/{ns}", response_model=Namespace)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def get_(ns: str, s: NamespaceService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        out = s.resolve(identifier=ns)
        if out is None:
            raise HTTPException(404, "namespace not found")
        return out

    @r.patch("/namespaces/{ns}", response_model=Namespace)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def update(  # nosemgrep: useless-inner-function — registered as route handler by decorator
        ns: str, body: NamespaceUpdate, s: NamespaceService = Depends(svc)
    ):
        try:
            return s.update(namespace_id=_require_id(svc=s, identifier=ns), request=body)
        except NamespaceNotFound as e:
            raise HTTPException(404, str(e))

    @r.post("/namespaces/{ns}/status", response_model=Namespace)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def set_status(  # nosemgrep: useless-inner-function — registered as route handler by decorator
        ns: str, body: StatusUpdate, s: NamespaceService = Depends(svc)
    ):
        if body.status not in {status.value for status in NamespaceStatus}:
            raise HTTPException(422, f"unknown status {body.status!r}")
        try:
            return s.set_status(
                namespace_id=_require_id(svc=s, identifier=ns), status=body.status
            )
        except NamespaceNotFound as e:
            raise HTTPException(404, str(e))
        except InvalidStatusTransition as e:
            raise HTTPException(409, str(e))

    @r.delete("/namespaces/{ns}", response_model=Namespace)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def delete(ns: str, s: NamespaceService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        try:
            return s.delete(namespace_id=_require_id(svc=s, identifier=ns))
        except NamespaceNotFound as e:
            raise HTTPException(404, str(e))

    @r.get("/namespaces/{ns}/grants", response_model=List[Grant])  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def list_grants(ns: str, s: NamespaceService = Depends(svc)):  # nosemgrep: useless-inner-function — registered as route handler by decorator
        return s.list_grants(namespace_id=_require_id(svc=s, identifier=ns))

    @r.post("/namespaces/{ns}/grants", status_code=201, response_model=Grant)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def grant(  # nosemgrep: useless-inner-function — registered as route handler by decorator
        request: Request,
        ns: str,
        body: GrantCreate,
        s: NamespaceService = Depends(svc),
    ):
        if body.role not in {role.value for role in Role}:
            raise HTTPException(422, f"unknown role {body.role!r}")
        principal = get_principal(request)
        try:
            return s.grant_role(
                namespace_id=_require_id(svc=s, identifier=ns),
                request=body,
                granted_by=principal['userId'],
            )
        except NamespaceNotFound as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @r.delete("/namespaces/{ns}/grants/{principal_key}/{role}", status_code=204)  # nosemgrep: useless-inner-function — FastAPI route handler/DI factory bound via decorator/Depends
    def revoke(  # nosemgrep: useless-inner-function — registered as route handler by decorator
        ns: str, principal_key: str, role: str, s: NamespaceService = Depends(svc)
    ):
        try:
            s.revoke_role(
                namespace_id=_require_id(svc=s, identifier=ns),
                principal_key=principal_key,
                role=role,
            )
        except NamespaceNotFound as e:
            raise HTTPException(404, str(e))

    return r


def _require_id(*, svc: NamespaceService, identifier: str) -> str:
    """Resolve an id-or-name path parameter to a namespace id.

    Grants and lifecycle rows are keyed by id, so writing them under a name
    would produce records the resolver never finds.

    Args:
        svc: The namespace service.
        identifier: A namespace id or name from the request path.

    Returns:
        The namespace id.

    Raises:
        HTTPException: 404 when no namespace matches.
    """
    found: Optional[Namespace] = svc.resolve(identifier=identifier)
    if found is None:
        raise HTTPException(404, f"namespace {identifier!r} not found")
    return found.id
