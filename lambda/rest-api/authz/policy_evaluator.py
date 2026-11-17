"""Cedar policy decision point.

Wraps ``cedarpy.is_authorized``. Policies are the ``.cedar`` files bundled next
to this module under ``policies/``; unlike COA they are not loaded from
DynamoDB, because SL has no per-namespace custom roles — every role's policy is
fixed at build time, so shipping them in the image removes a DynamoDB read from
the hot path and removes the "namespace has no role templates" failure mode.

Fails CLOSED: any exception — a malformed policy, an entity that cedarpy
rejects, a missing policy file — returns Deny rather than propagating and being
caught as a 500 somewhere that might not deny.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from cedarpy import is_authorized

from authz.cedar_schema import (
    DEFAULT_POLICY_NAME,
    ROLE_POLICY_FILES,
    SL_SCHEMA,
    EntityType,
)

logger = logging.getLogger(__name__)

_POLICY_DIR = Path(__file__).parent / 'policies'


@dataclass(frozen=True)
class Decision:
    """The outcome of one authorization evaluation.

    Attributes:
        allowed: True when Cedar returned Allow.
        reason: Diagnostic detail for the audit log. Never surfaced verbatim to
            the caller — the 403 body stays generic so a denial does not leak
            the policy set or another principal's roles.
    """

    allowed: bool
    reason: str = ''


# Unbounded cache: the key space is the fixed set of shipped policy files, so
# there is nothing to evict and no growth risk.
@lru_cache(maxsize=None)
def _read_policy(name: str) -> str:
    """Read one ``.cedar`` file, cached for the process lifetime.

    Args:
        name: Policy file stem, e.g. ``platform_admin``.

    Returns:
        The policy text.

    Raises:
        FileNotFoundError: If the policy file is missing. The caller converts
            this into a Deny.
    """
    return (_POLICY_DIR / f'{name}.cedar').read_text(encoding='utf-8')


def load_policies(*, roles: Iterable[str]) -> str:
    """Concatenate the default policy with the policy of each named role.

    ``default`` is always included so the non-ACTIVE mutation guard
    participates in every decision — that guard is the only ``forbid`` in the
    policy set and must not depend on which roles the caller holds.

    Unknown role names are skipped (with a warning) rather than raising: a grant
    row naming a role this build does not ship must not lock out the roles that
    are valid. Skipping is safe because an unknown role contributes no
    ``permit``.

    Args:
        roles: The principal's resolved role identifiers.

    Returns:
        The concatenated Cedar policy text.
    """
    names: List[str] = [DEFAULT_POLICY_NAME]
    for role in roles:
        policy_file = ROLE_POLICY_FILES.get(role)
        if policy_file is None:
            logger.warning('authz_unknown_role role=%s — no policy loaded', role)
            continue
        if policy_file not in names:
            names.append(policy_file)
    return '\n'.join(_read_policy(name) for name in names)


def load_all_policies() -> str:
    """Return every shipped policy concatenated.

    Used by the policy unit tests, which assert role behaviour against the full
    set rather than a per-request subset.

    Returns:
        The concatenated Cedar policy text for all roles plus the default.
    """
    return load_policies(roles=list(ROLE_POLICY_FILES.keys()))


def _build_user_entity(
    *,
    user_id: str,
    groups: List[str],
    global_roles: List[str],
    resource_roles: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Build the ``SL::User`` Cedar entity for the request principal.

    Args:
        user_id: The JWT ``sub``.
        groups: ``cognito:groups`` values, carried for policy authors who want
            to condition on a raw group rather than a mapped role.
        global_roles: Platform-scoped role identifiers.
        resource_roles: ``[{"role": …, "resourceUID": …}]`` entries.

    Returns:
        The entity dict cedarpy expects.
    """
    return {
        'uid': {'type': f'{SL_SCHEMA}::{EntityType.USER}', 'id': user_id},
        'attrs': {
            'userId': user_id,
            'groups': groups,
            'globalRoles': global_roles,
            'resourceRoles': resource_roles,
        },
        'parents': [],
    }


def _build_resource_entity(
    *, resource_id: str, attrs: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Build the ``SL::Namespace`` Cedar entity the decision is evaluated against.

    ``namespace`` is set alongside ``id`` (to the same value) so a policy copied
    verbatim from COA — which conditions child-resource rules on
    ``resource.namespace`` — evaluates correctly here without editing.

    Args:
        resource_id: The namespace id, or the ``PLATFORM`` sentinel.
        attrs: Extra resource attributes to merge.

    Returns:
        The entity dict cedarpy expects.
    """
    entity_attrs: Dict[str, Any] = {'id': resource_id, 'namespace': resource_id}
    if attrs:
        entity_attrs.update(attrs)
    return {
        'uid': {'type': f'{SL_SCHEMA}::{EntityType.NAMESPACE}', 'id': resource_id},
        'attrs': entity_attrs,
        'parents': [],
    }


def evaluate(
    *,
    action: str,
    user_id: str,
    resource_id: str,
    groups: Optional[List[str]] = None,
    global_roles: Optional[List[str]] = None,
    resource_roles: Optional[List[Dict[str, str]]] = None,
    resource_attrs: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    policies: Optional[str] = None,
) -> Decision:
    """Evaluate one authorization request against the Cedar policy set.

    Args:
        action: Bare action name from :class:`~authz.cedar_schema.Action`.
        user_id: The JWT ``sub`` of the requesting principal.
        resource_id: The namespace id, or ``PLATFORM`` for platform-scoped
            routes.
        groups: ``cognito:groups`` values.
        global_roles: Resolved platform roles.
        resource_roles: Resolved namespace-scoped roles.
        resource_attrs: Extra attributes for the resource entity.
        context: Request-time facts passed to Cedar as the request ``context``
            record — notably ``namespaceStatus``, which drives the non-ACTIVE
            mutation guard.
        policies: Pre-loaded policy text. Defaults to the policies for
            ``global_roles`` + the roles named in ``resource_roles``.

    Returns:
        A :class:`Decision`. Any evaluation failure returns
        ``Decision(allowed=False)``.
    """
    group_list = list(groups or [])
    global_role_list = list(global_roles or [])
    resource_role_list = list(resource_roles or [])

    try:
        if policies is None:
            role_names = global_role_list + [
                str(entry.get('role') or '') for entry in resource_role_list
            ]
            policies = load_policies(roles=[r for r in role_names if r])

        user = _build_user_entity(
            user_id=user_id,
            groups=group_list,
            global_roles=global_role_list,
            resource_roles=resource_role_list,
        )
        resource = _build_resource_entity(
            resource_id=resource_id, attrs=resource_attrs
        )

        request = {
            'principal': f'{SL_SCHEMA}::{EntityType.USER}::"{user_id}"',
            'action': f'{SL_SCHEMA}::Action::"{action}"',
            'resource': (
                f'{SL_SCHEMA}::{EntityType.NAMESPACE}::"{resource_id}"'
            ),
            'context': context or {},
        }

        result = is_authorized(request, policies, [user, resource])
    except Exception as exc:
        # Security-critical path: log the detail server-side, deny the request.
        logger.warning(
            'authz_evaluate_error action=%s user_id=%s resource_id=%s error=%s: %s',
            action,
            user_id,
            resource_id,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return Decision(allowed=False, reason='policy evaluation failed')

    if result.allowed:
        return Decision(allowed=True)
    return Decision(allowed=False, reason=f'no policy permits {action}')
