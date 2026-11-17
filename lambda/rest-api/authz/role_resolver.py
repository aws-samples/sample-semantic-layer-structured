"""Resolve a request principal's roles for the policy decision point.

Two sources feed the resolved role set:

1. **Cognito group mapping** (``AUTHZ_GROUP_ROLE_MAP``) — bootstrap so that
   turning authorization on does not lock every existing user out on the first
   request. The deployed user pool already carries ``Admin``/``Admins`` and
   ``Users``/``QueryUsers`` groups that nothing read before this module; they
   now map to ``platform-admin`` and ``platform-viewer`` respectively.
2. **Grants table** — the durable, per-namespace source of truth, read through
   :func:`agents.shared.grant_profile.resolve_profile` so the REST API and the
   agent runtimes resolve grants with exactly one implementation.

Group mapping is bootstrap only: a group can grant a PLATFORM role, never a
namespace-scoped one, because there is no namespace in a group name. Real
namespace roles must be granted, which keeps the audit trail in DynamoDB.

Resolution is cached for a short TTL because a single API call can trigger
several decisions (middleware, then the firewall inside a handler). The cache is
bounded and keyed on the full identity so one user's roles can never be served
to another.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from agents.shared.grant_profile import ResolvedProfile, resolve_profile

from authz.cedar_schema import Role

logger = logging.getLogger(__name__)

# Default Cognito-group -> global-role map. Both spellings of each group exist in
# the deployed stacks (``auth/index.ts`` creates Admin/Users, ``lf-groups/
# index.ts`` creates Admins/QueryUsers), so all four are mapped.
#
# Deliberately conservative on the read side: an existing member of Users /
# QueryUsers keeps read + query access when authorization is switched on, but
# gains no write. Writes require an explicit grant.
_DEFAULT_GROUP_ROLE_MAP: Dict[str, str] = {
    'Admin': Role.PLATFORM_ADMIN.value,
    'Admins': Role.PLATFORM_ADMIN.value,
    'Users': Role.PLATFORM_VIEWER.value,
    'QueryUsers': Role.PLATFORM_VIEWER.value,
}

_GROUP_ROLE_MAP_ENV = 'AUTHZ_GROUP_ROLE_MAP'
_CACHE_TTL_ENV = 'AUTHZ_CACHE_TTL_SECONDS'
_DEFAULT_CACHE_TTL_SECONDS = 30.0
_CACHE_MAX_ENTRIES = 512


class RoleResolutionError(RuntimeError):
    """Raised when the grants table could not be read.

    Separate from a plain denial: the caller turns this into a 503 rather than a
    403, because "we do not know your roles" is an availability failure, not an
    authorization outcome. Either way the request does not proceed.
    """


@dataclass(frozen=True)
class ResolvedRoles:
    """The roles and data restrictions in force for one (principal, namespace).

    Attributes:
        user_id: The JWT ``sub``.
        email: The ``email`` claim, or ``''``.
        groups: ``cognito:groups`` values from the JWT.
        global_roles: Platform-scoped roles, from group mapping and from grants
            whose ``resourceId`` is ``GLOBAL``.
        resource_roles: ``[{"role": …, "resourceUID": …}]`` namespace-scoped
            grants.
        table_allowlist: Merged table allowlist, or ``None`` when no grant
            declares one. ``None`` and ``[]`` mean different things — see
            :mod:`agents.shared.sql_firewall`.
        column_denylist: Merged ``{table: [column, …]}`` denylist, or ``None``.
        allowed_metrics: Merged metric allowlist, or ``None``.
    """

    user_id: str
    email: str = ''
    groups: List[str] = field(default_factory=list)
    global_roles: List[str] = field(default_factory=list)
    resource_roles: List[Dict[str, str]] = field(default_factory=list)
    table_allowlist: Optional[List[str]] = None
    column_denylist: Optional[Dict[str, List[str]]] = None
    allowed_metrics: Optional[List[str]] = None

    def role_names(self) -> List[str]:
        """List every role identifier the principal holds, deduplicated.

        Returns:
            Global roles followed by the roles named in ``resource_roles``, in
            first-seen order. Used to select which policy files to load.
        """
        names: List[str] = []
        for name in self.global_roles:
            if name and name not in names:
                names.append(name)
        for entry in self.resource_roles:
            name = str(entry.get('role') or '')
            if name and name not in names:
                names.append(name)
        return names

    def to_grant_profile(self) -> Dict[str, Any]:
        """Render the data restrictions as an SQL-firewall grant profile.

        Absent restrictions are omitted rather than set to ``None`` so the
        firewall's "nothing declared -> unrestricted" branch stays distinct from
        an explicit empty allowlist (which denies everything).

        Returns:
            A dict suitable for ``sql_firewall.enforce_sql(profile=…)``.
        """
        profile: Dict[str, Any] = {'userId': self.user_id}
        if self.table_allowlist is not None:
            profile['tableAllowlist'] = self.table_allowlist
        if self.column_denylist is not None:
            profile['columnDenylist'] = self.column_denylist
        if self.allowed_metrics is not None:
            profile['allowedMetrics'] = self.allowed_metrics
        return profile


def group_role_map() -> Dict[str, str]:
    """Return the active Cognito-group -> global-role map.

    Read from the environment on every call (not cached) so an operator can
    change the mapping with a Lambda configuration update instead of a redeploy.

    Returns:
        The parsed map, or the built-in default when the variable is unset.

    Raises:
        RoleResolutionError: If the variable is set but is not a JSON object of
            string -> string. Falling back to the default here would silently
            grant the default roles after an operator intended to restrict them.
    """
    raw = os.environ.get(_GROUP_ROLE_MAP_ENV, '').strip()
    if not raw:
        return dict(_DEFAULT_GROUP_ROLE_MAP)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RoleResolutionError(
            f'{_GROUP_ROLE_MAP_ENV} is not valid JSON: {exc}'
        ) from exc
    if not isinstance(parsed, dict):
        raise RoleResolutionError(f'{_GROUP_ROLE_MAP_ENV} must be a JSON object')
    mapping: Dict[str, str] = {}
    for group, role in parsed.items():
        if not isinstance(group, str) or not isinstance(role, str):
            raise RoleResolutionError(
                f'{_GROUP_ROLE_MAP_ENV} entries must be string -> string'
            )
        mapping[group] = role
    return mapping


def roles_from_groups(*, groups: List[str]) -> List[str]:
    """Map Cognito group names to platform role identifiers.

    Args:
        groups: ``cognito:groups`` values for the principal.

    Returns:
        The distinct mapped roles, in first-seen order. Unmapped groups
        contribute nothing (they may exist for reasons unrelated to authz, such
        as the Lake Formation groups).
    """
    mapping = group_role_map()
    roles: List[str] = []
    for group in groups:
        role = mapping.get(group)
        if role and role not in roles:
            roles.append(role)
    return roles


def _cache_ttl_seconds() -> float:
    """Read the resolution cache TTL from the environment.

    Returns:
        The TTL in seconds; ``0`` disables caching entirely.

    Raises:
        RoleResolutionError: If the variable is set to something non-numeric —
            a typo must not silently re-enable a stale-role window.
    """
    raw = os.environ.get(_CACHE_TTL_ENV, '').strip()
    if not raw:
        return _DEFAULT_CACHE_TTL_SECONDS
    try:
        ttl = float(raw)
    except ValueError as exc:
        raise RoleResolutionError(
            f'{_CACHE_TTL_ENV} must be a number, got {raw!r}'
        ) from exc
    if ttl < 0:
        raise RoleResolutionError(f'{_CACHE_TTL_ENV} must not be negative')
    return ttl


# Bounded, TTL'd resolution cache: key -> (expiry_monotonic, ResolvedRoles).
# An OrderedDict gives insertion-order eviction without a dependency.
_cache: 'OrderedDict[Tuple[str, str, Tuple[str, ...], str], Tuple[float, ResolvedRoles]]' = (
    OrderedDict()
)


def clear_cache() -> None:
    """Drop every cached resolution.

    Called by the namespace service after a grant is created or revoked so the
    change takes effect immediately rather than after the TTL, and by tests.

    Returns:
        None.
    """
    _cache.clear()


def _cache_get(
    *, key: Tuple[str, str, Tuple[str, ...], str], now: float
) -> Optional[ResolvedRoles]:
    """Look up a live cache entry.

    Args:
        key: The identity cache key.
        now: Current ``time.monotonic()`` reading.

    Returns:
        The cached roles, or ``None`` when absent or expired.
    """
    entry = _cache.get(key)
    if entry is None:
        return None
    expiry, roles = entry
    if expiry <= now:
        del _cache[key]
        return None
    return roles


def _cache_put(
    *,
    key: Tuple[str, str, Tuple[str, ...], str],
    roles: ResolvedRoles,
    now: float,
    ttl: float,
) -> None:
    """Store a resolution, evicting the oldest entry when the cache is full.

    Args:
        key: The identity cache key.
        roles: The resolution to cache.
        now: Current ``time.monotonic()`` reading.
        ttl: Lifetime in seconds.

    Returns:
        None.
    """
    _cache[key] = (now + ttl, roles)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)


def resolve_roles(
    *,
    user_id: str,
    email: str = '',
    groups: Optional[List[str]] = None,
    namespace: str = '',
    use_cache: bool = True,
) -> ResolvedRoles:
    """Resolve the roles and data restrictions in force for one request.

    Args:
        user_id: The JWT ``sub``. Required — an empty value means the request
            never passed the JWT authorizer and must not be resolved.
        email: The ``email`` claim, used as a second principal key so a grant
            can be written for a person before they first sign in.
        groups: ``cognito:groups`` values.
        namespace: The namespace the request targets, or ``''`` for a
            platform-scoped route. Data restrictions are merged only from grants
            scoped to this namespace.
        use_cache: Set False to force a fresh grants-table read.

    Returns:
        The resolved :class:`ResolvedRoles`.

    Raises:
        ValueError: If ``user_id`` is empty.
        RoleResolutionError: If the grants table could not be read, or the
            group map / TTL configuration is malformed.
    """
    if not user_id:
        raise ValueError('resolve_roles requires a non-empty user_id')

    group_list = list(groups or [])
    key = (user_id, email, tuple(group_list), namespace)
    ttl = _cache_ttl_seconds()
    now = time.monotonic()

    if use_cache and ttl > 0:
        cached = _cache_get(key=key, now=now)
        if cached is not None:
            return cached

    try:
        profile: ResolvedProfile = resolve_profile(
            user_id=user_id, email=email, groups=group_list, namespace=namespace
        )
    except Exception as exc:
        # Never degrade to "no grants found": that would look identical to a
        # principal with no privileges, silently breaking every legitimate write.
        logger.error(
            'authz_grant_lookup_failed user_id=%s namespace=%s error=%s: %s',
            user_id,
            namespace,
            type(exc).__name__,
            exc,
        )
        raise RoleResolutionError('grants lookup failed') from exc

    # Group-derived roles are merged with grant-derived global roles. Groups can
    # only ever contribute PLATFORM roles — a group name carries no namespace.
    global_roles: List[str] = list(roles_from_groups(groups=group_list))
    for role in profile.global_roles:
        if role not in global_roles:
            global_roles.append(role)

    resolved = ResolvedRoles(
        user_id=user_id,
        email=email,
        groups=group_list,
        global_roles=global_roles,
        resource_roles=list(profile.resource_roles),
        table_allowlist=profile.table_allowlist,
        column_denylist=profile.column_denylist,
        allowed_metrics=profile.allowed_metrics,
    )

    if use_cache and ttl > 0:
        _cache_put(key=key, roles=resolved, now=now, ttl=ttl)
    return resolved
