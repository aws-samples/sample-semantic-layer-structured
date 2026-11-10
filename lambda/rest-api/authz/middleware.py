"""ASGI policy enforcement point for the REST API.

Where COA puts its PDP in an API Gateway REQUEST authorizer, SL puts it here.
The reason is structural, not stylistic: SL's HTTP API exposes a single
``/{proxy+}`` ``ANY`` route (deliberately — per-route integrations blow past
Lambda's 20 KB resource-policy limit), so a REQUEST authorizer would see one
route and could not distinguish ``GET /metrics/...`` from
``POST /rules/...:approve``.

Installing the check as **root-app ASGI middleware** recovers the property that
matters: ``app.mount()`` composes at the ASGI level, so middleware on the root
app wraps every mounted sub-app and every included router. There is no way to
add a route that bypasses it. Coverage is structural, exactly as it is in COA —
and a route this build does not know about maps to ``denyAll``, which
``default.cedar`` forbids.

Pure ASGI rather than ``BaseHTTPMiddleware``: the latter buffers the response
body, which would break the chat SSE stream.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from authz.action_map import (
    NamespaceSource,
    RouteMatch,
    is_bypass_path,
    match_route,
    resolve_namespace,
)
from authz.cedar_schema import PLATFORM_RESOURCE_ID, NamespaceStatus
from authz.policy_evaluator import evaluate
from authz.role_resolver import (
    ResolvedRoles,
    RoleResolutionError,
    resolve_roles,
)

logger = logging.getLogger(__name__)

ENABLE_AUTHZ_ENV = 'ENABLE_AUTHZ'
SCOPE_KEY = 'authz'
"""ASGI scope key under which the allowed decision's context is published."""

# Resolver signatures. Both are injected so this module performs no I/O and can
# be unit-tested against the policy set alone.
OntologyNamespaceResolver = Callable[[str], Optional[str]]

NamespaceResolver = Callable[[str], Optional[Tuple[str, str]]]
"""Maps a namespace id *or name* to ``(canonical id, status)``, or None.

Returning the canonical id — not just the status — is load-bearing. Legacy routes
(``/rules/namespaces/{ns}/rules``, ``/metrics/namespaces/{ns}/metrics``) predate
namespace ids and callers pass names there, while grants record ``resourceUID``
as the namespace **id**. Evaluating the raw path segment would compare a name
against an id, so every namespace-scoped grant would silently fail to match and
only platform roles would ever work.
"""


def authz_enabled() -> bool:
    """Report whether enforcement is switched on.

    Read per request so the flag can be flipped with a Lambda configuration
    update. Defaults to disabled, so deploying this code changes no behaviour
    until the flag and the seed grants are both in place.

    Returns:
        True when ``ENABLE_AUTHZ`` is a truthy string.
    """
    return os.environ.get(ENABLE_AUTHZ_ENV, '').strip().lower() in (
        'true',
        '1',
        'yes',
    )


def parse_groups(*, raw: Any) -> List[str]:
    """Normalise the ``cognito:groups`` claim into a list of group names.

    The API Gateway HTTP API JWT authorizer stringifies claim values, so a
    multi-valued group claim arrives as ``"[Admin Users]"`` rather than a JSON
    array. Handling only the list form would silently drop every group of every
    multi-group user — which reads as "this admin has no roles".

    Args:
        raw: The claim value: a list, a bracketed space-separated string, a
            comma-separated string, a bare name, or None.

    Returns:
        The group names, in order, with empties removed.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith('[') and text.endswith(']'):
        text = text[1:-1]
    separator = ',' if ',' in text else None
    parts = text.split(separator) if separator else text.split()
    return [part.strip() for part in parts if part.strip()]


def extract_claims(*, scope: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the verified JWT claims out of the ASGI scope.

    Mangum stashes the original Lambda event under ``aws.event``; the HTTP API
    JWT authorizer puts verified claims at
    ``requestContext.authorizer.jwt.claims``. Nothing here parses or trusts a
    raw token — the authorizer has already validated the signature, so claims
    present in the event are authoritative and claims absent from it mean the
    request did not pass the authorizer.

    Args:
        scope: The ASGI connection scope.

    Returns:
        The claims mapping, empty when the request bypassed the authorizer
        (local uvicorn, direct Lambda invoke).
    """
    event = scope.get('aws.event') or {}
    if not isinstance(event, dict):
        return {}
    request_context = event.get('requestContext') or {}
    authorizer = request_context.get('authorizer') or {}
    jwt_block = authorizer.get('jwt') or {}
    claims = jwt_block.get('claims') or authorizer.get('claims') or {}
    return claims if isinstance(claims, dict) else {}


async def _send_error(
    *,
    send: Callable[[Dict[str, Any]], Awaitable[None]],
    status: int,
    detail: str,
) -> None:
    """Emit a JSON error response and end the exchange.

    Args:
        send: The ASGI send callable.
        status: HTTP status code.
        detail: Message body. Deliberately generic — a denial must not disclose
            which policy denied it or which roles the caller would have needed.

    Returns:
        None.
    """
    body = json.dumps({'detail': detail}).encode('utf-8')
    await send(
        {
            'type': 'http.response.start',
            'status': status,
            'headers': [
                (b'content-type', b'application/json'),
                (b'content-length', str(len(body)).encode('ascii')),
            ],
        }
    )
    await send({'type': 'http.response.body', 'body': body})


class AuthzMiddleware:
    """Deny-by-default authorization for every HTTP request to the REST API.

    Attributes:
        app: The wrapped ASGI application.
        ontology_namespace_resolver: Maps an ontology id to its owning
            namespace, or None when unbound.
        namespace_resolver: Maps a namespace id/name to ``(id, status)``, or
            None when unknown.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        ontology_namespace_resolver: OntologyNamespaceResolver,
        namespace_resolver: NamespaceResolver,
    ) -> None:
        """Wrap an ASGI app with the policy enforcement point.

        Args:
            app: The ASGI application to wrap (positional, as ASGI requires).
            ontology_namespace_resolver: Ontology-id -> namespace lookup.
            namespace_resolver: Namespace id-or-name -> ``(id, status)`` lookup.
        """
        self.app = app
        self.ontology_namespace_resolver = ontology_namespace_resolver
        self.namespace_resolver = namespace_resolver

    async def __call__(
        self,
        scope: Dict[str, Any],
        receive: Callable[[], Awaitable[Dict[str, Any]]],
        send: Callable[[Dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Authorize the request, then delegate to the wrapped app.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive callable.
            send: The ASGI send callable.

        Returns:
            None.
        """
        # Lifespan and websocket scopes carry no principal and no route; pass
        # them through untouched rather than denying and breaking startup.
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

        method = str(scope.get('method') or 'GET')
        path = str(scope.get('path') or '/')

        # CORS preflight carries no Authorization header by specification, so it
        # can never be authorized. The CORS middleware answers it.
        if method.upper() == 'OPTIONS' or is_bypass_path(path=path):
            await self.app(scope, receive, send)
            return

        if not authz_enabled():
            await self.app(scope, receive, send)
            return

        claims = extract_claims(scope=scope)
        user_id = str(claims.get('sub') or '')
        if not user_id:
            logger.warning(
                'authz_denied reason=no_principal method=%s path=%s', method, path
            )
            await _send_error(
                send=send, status=401, detail='Unauthenticated'
            )
            return

        email = str(claims.get('email') or '')
        groups = parse_groups(raw=claims.get('cognito:groups'))
        route: RouteMatch = match_route(method=method, path=path)

        # Namespace binding. An ONTOLOGY-scoped route whose ontology is not bound
        # to a namespace is denied rather than evaluated against PLATFORM — see
        # resolve_namespace().
        ontology_namespace: Optional[str] = None
        if route.namespace_source is NamespaceSource.ONTOLOGY:
            try:
                ontology_namespace = self.ontology_namespace_resolver(
                    route.ontology_id
                )
            except Exception as exc:
                logger.error(
                    'authz_namespace_binding_error ontology_id=%s error=%s: %s',
                    route.ontology_id,
                    type(exc).__name__,
                    exc,
                )
                await _send_error(
                    send=send, status=503, detail='Authorization unavailable'
                )
                return
        try:
            namespace = resolve_namespace(
                match=route, ontology_namespace=ontology_namespace
            )
        except ValueError as exc:
            logger.warning(
                'authz_denied reason=unbound_ontology method=%s path=%s: %s',
                method,
                path,
                exc,
            )
            await _send_error(send=send, status=403, detail='Forbidden')
            return

        # Canonicalisation + lifecycle context, in that order: the namespace must
        # be its id before roles are resolved, because grants and data
        # restrictions are both keyed by id. The status is attached only for a
        # real namespace, so the non-ACTIVE mutation guard cannot misfire on a
        # PLATFORM-scoped route or on namespace creation.
        context: Dict[str, Any] = {}
        if namespace != PLATFORM_RESOURCE_ID:
            try:
                resolved = self.namespace_resolver(namespace)
            except Exception as exc:
                logger.error(
                    'authz_namespace_status_error namespace=%s error=%s: %s',
                    namespace,
                    type(exc).__name__,
                    exc,
                )
                await _send_error(
                    send=send, status=503, detail='Authorization unavailable'
                )
                return
            if resolved is None:
                # An unknown namespace is a 403, not a 404: answering 404 would
                # let an unauthorized caller enumerate which namespaces exist.
                logger.warning(
                    'authz_denied reason=unknown_namespace namespace=%s path=%s',
                    namespace,
                    path,
                )
                await _send_error(send=send, status=403, detail='Forbidden')
                return
            namespace, status = resolved
            context['namespaceStatus'] = status

        # Roles. A grants-table failure is an availability problem, not a
        # decision — 503, never a silent "no roles".
        try:
            roles: ResolvedRoles = resolve_roles(
                user_id=user_id,
                email=email,
                groups=groups,
                namespace=(
                    '' if namespace == PLATFORM_RESOURCE_ID else namespace
                ),
            )
        except RoleResolutionError as exc:
            logger.error('authz_role_resolution_failed path=%s: %s', path, exc)
            await _send_error(
                send=send, status=503, detail='Authorization unavailable'
            )
            return

        decision = evaluate(
            action=route.action,
            user_id=user_id,
            resource_id=namespace,
            groups=groups,
            global_roles=roles.global_roles,
            resource_roles=roles.resource_roles,
            context=context,
        )

        if not decision.allowed:
            logger.warning(
                'authz_denied action=%s user_id=%s namespace=%s method=%s '
                'path=%s reason=%s',
                route.action,
                user_id,
                namespace,
                method,
                path,
                decision.reason,
            )
            await _send_error(send=send, status=403, detail='Forbidden')
            return

        logger.info(
            'authz_allowed action=%s user_id=%s namespace=%s status=%s',
            route.action,
            user_id,
            namespace,
            context.get('namespaceStatus', '-'),
        )

        # Publish the decision so downstream code reuses this resolution rather
        # than re-reading grants: notably the SQL firewall, which needs the
        # caller's table allowlist / column denylist.
        scope[SCOPE_KEY] = {
            'userId': user_id,
            'email': email,
            'groups': groups,
            'action': route.action,
            'namespace': namespace,
            'namespaceStatus': context.get('namespaceStatus', ''),
            'globalRoles': roles.global_roles,
            'resourceRoles': roles.resource_roles,
            'grantProfile': roles.to_grant_profile(),
        }

        await self.app(scope, receive, send)


def get_authz_context(request: Any) -> Dict[str, Any]:
    """Read the published authorization context for the current request.

    Args:
        request: A Starlette/FastAPI ``Request``.

    Returns:
        The context dict the middleware published, or ``{}`` when enforcement is
        disabled or the path was a bypass path. Callers must treat an empty dict
        as "no restrictions declared", which is what the firewall does with an
        empty grant profile.
    """
    return dict(getattr(request, 'scope', {}).get(SCOPE_KEY) or {})


def get_grant_profile(request: Any) -> Dict[str, Any]:
    """Read just the SQL-firewall grant profile for the current request.

    Args:
        request: A Starlette/FastAPI ``Request``.

    Returns:
        The grant profile published by the middleware, or ``{}``.
    """
    return dict(get_authz_context(request).get('grantProfile') or {})


# Re-exported so callers do not need a second import to name a status.
__all__ = [
    'ENABLE_AUTHZ_ENV',
    'SCOPE_KEY',
    'AuthzMiddleware',
    'NamespaceStatus',
    'authz_enabled',
    'extract_claims',
    'get_authz_context',
    'get_grant_profile',
    'parse_groups',
]
