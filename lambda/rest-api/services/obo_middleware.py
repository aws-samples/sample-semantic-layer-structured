"""OBO middleware — FastAPI dependencies for principal extraction + OBO exchange.

Used by user-initiated routes (chat, query submit) to:
  1. Extract the verified Cognito principal from the API Gateway
     authorizer claims attached to ``request.scope['aws.event']``
     (Mangum forwards the Lambda event under that key).
  2. Exchange the JWT for an OBO token via ``IdentityService``.
  3. Fail-closed when ENABLE_OBO_PASSTHROUGH=true and the exchange errors.

Service routes (background scans, eval) opt out by simply not depending
on ``require_obo``. When ``ENABLE_OBO_PASSTHROUGH`` is false, the
dependency returns ``None`` and downstream code keeps using service
identity — matching Phase 0/1 of the rollout in the design doc.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

from services.identity_service import (
    IdentityService,
    ObOExchangeError,
    ObOToken,
)

logger = logging.getLogger(__name__)


# Single shared service so the per-(user, ontology) cache is process-wide.
_identity_service: Optional[IdentityService] = None


def get_identity_service() -> IdentityService:
    """Singleton accessor used by the dependency. Tests can monkey-patch the
    module global to inject a mocked service."""
    global _identity_service
    if _identity_service is None:
        _identity_service = IdentityService()
    return _identity_service


def _extract_lambda_event(request: Request) -> Dict[str, Any]:
    """Return the original Lambda event dict that Mangum stashes on the
    request scope. When running uvicorn locally the event is absent — we
    return an empty dict so the dependency degrades to service identity."""
    scope = request.scope
    # Mangum 0.17+ uses 'aws.event'.
    return scope.get('aws.event') or {}


def _extract_principal(event: Dict[str, Any]) -> Dict[str, str]:
    """Pull the verified principal out of the API Gateway JWT authorizer.

    Returns ``{userId, email, jwt}``; missing fields default to empty strings
    so callers can decide what to do with partial credentials.
    """
    request_context = (
        event.get('requestContext') if isinstance(event, dict) else None
    ) or {}
    authorizer = request_context.get('authorizer') or {}
    # API Gateway HTTP API JWT authorizer puts claims under 'jwt.claims'.
    jwt_block = authorizer.get('jwt') or {}
    claims = jwt_block.get('claims') or authorizer.get('claims') or {}
    headers = (event.get('headers') or {}) if isinstance(event, dict) else {}
    auth_header = headers.get('authorization') or headers.get('Authorization') or ''
    bearer = ''
    if auth_header.lower().startswith('bearer '):
        bearer = auth_header.split(' ', 1)[1]
    return {
        'userId': str(claims.get('sub') or ''),
        'email': str(claims.get('email') or ''),
        'jwt': bearer,
    }


def get_principal(request: Request) -> Dict[str, str]:
    """FastAPI dependency — verified principal info for the current request.

    Returns a dict with ``userId``, ``email``, ``jwt`` keys. When the request
    didn't go through the JWT authorizer (local dev, service-token paths) the
    fields are empty strings.
    """
    event = _extract_lambda_event(request)
    return _extract_principal(event)


def require_obo(
    request: Request,
    ontology_id: str = '',
) -> Optional[ObOToken]:
    """FastAPI dependency that returns an OBO token (or None when the
    flag is off). Raises HTTPException(401) on any exchange failure when
    OBO is enabled, per the fail-closed rule.

    Routes pass the ontology id either via path (eg. ``/query/{id}``) or
    body (caller resolves and supplies it through ``Query()`` /
    ``Body()`` and passes through this helper). Tests can swap
    ``get_identity_service`` to inject a mock.
    """
    svc = get_identity_service()
    if not svc.enabled:
        return None
    principal = get_principal(request)
    if not principal['userId'] or not principal['jwt']:
        # OBO is on but we have no JWT to exchange — fail closed.
        raise HTTPException(
            status_code=401,
            detail='OBO passthrough enabled but no JWT in request',
        )
    audience = os.environ.get(
        'OBO_TARGET_AUDIENCE',
        f"semantic-layer-{os.environ.get('AWS_REGION', 'us-east-1')}",
    )
    try:
        return svc.exchange(
            jwt=principal['jwt'],
            user_id=principal['userId'],
            ontology_id=ontology_id or 'default',
            target_audience=audience,
        )
    except ObOExchangeError as exc:
        logger.warning('OBO exchange failed: %s', exc)
        raise HTTPException(
            status_code=401,
            detail=f'OBO exchange failed: {exc}',
        ) from exc
