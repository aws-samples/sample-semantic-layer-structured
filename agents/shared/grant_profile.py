"""Resolve a principal's roles and data-access restrictions from the grants table.

Shared by the REST API's authorization middleware and by the agent runtimes
that execute SQL, so both derive the same profile from the same grants. Ported
from the Context Ontology Accelerator's ``coa_serve/role_resolver.py``.

Two invariants carried over verbatim, both of which exist because violating
them fails *open*:

1. **Paginate fully.** A single DynamoDB ``Query`` returns one 1 MB page. If a
   restrictive grant lands on a later page while an unrestricted one survives,
   the merge below sees no restrictions at all and the SQL firewall stops
   enforcing them — a PII denylist silently disabled by a pagination boundary.
2. **Never degrade to a partial read.** A lookup failure re-raises. Restrictions
   computed from a subset of the grants are more permissive than the truth, so a
   transient DynamoDB error would widen data access instead of denying it.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import boto3

logger = logging.getLogger(__name__)

# The literal ``resourceId`` marking a platform-wide (non-namespace) grant.
GLOBAL_RESOURCE_ID = 'GLOBAL'

_PRINCIPAL_INDEX = 'PrincipalIndex'


def sanitize_principal_key(*, value: str) -> str:
    """URL-encode a value used as a DynamoDB principal-key component.

    Encodes characters that would corrupt the ``<type>::<id>`` key layout
    (``+``, ``#``, ``/``) while leaving ``@`` and ``.`` intact so stored keys
    stay human-readable. Identifiers containing ``@`` are treated as email
    addresses: they are stripped and lower-cased first, because every IdP emits
    email case inconsistently between the ``email`` claim and its user records,
    and a grant written under one case would otherwise be invisible to a reader
    query built from the other.

    Cognito ``sub`` UUIDs and group names pass through verbatim — ``sub`` is
    opaque and case-sensitive, and group names may intentionally be mixed case.

    This is the single source of truth for principal-key encoding; every writer
    and reader of the grants table must go through it.

    Args:
        value: The raw principal identifier (sub, email, or group name).

    Returns:
        The encoded key component.
    """
    if '@' in value:
        value = value.strip().lower()
    return urllib.parse.quote(value, safe='@.')


def build_principal_keys(
    *, user_id: str, email: str, groups: List[str]
) -> List[str]:
    """Build every ``PrincipalIndex`` lookup key for one caller.

    A grant may have been written under any identifier the caller presents:
    the JWT ``sub``, the ``email`` claim, or a group membership. All of them are
    queried, so a grant written under one form resolves regardless of which
    identifier the token primarily surfaces. Omitting the email lookup silently
    hides grants written by the UI against an email address.

    Args:
        user_id: The JWT ``sub``.
        email: The JWT ``email`` claim; empty string when absent.
        groups: The caller's group memberships from ``cognito:groups``.

    Returns:
        Deduplicated ``User::…`` and ``Group::…`` keys.
    """
    seen: Set[str] = set()
    keys: List[str] = []
    for identifier in (user_id, email):
        if not identifier:
            continue
        key = f'User::{sanitize_principal_key(value=identifier)}'
        if key not in seen:
            seen.add(key)
            keys.append(key)
    for group in groups:
        if not group:
            continue
        key = f'Group::{sanitize_principal_key(value=group)}'
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


@dataclass
class ResolvedProfile:
    """A principal's resolved roles and data-access restrictions."""

    user_id: str
    email: str = ''
    groups: List[str] = field(default_factory=list)
    global_roles: List[str] = field(default_factory=list)
    resource_roles: List[Dict[str, str]] = field(default_factory=list)
    table_allowlist: Optional[List[str]] = None
    column_denylist: Optional[Dict[str, List[str]]] = None
    allowed_metrics: Optional[List[str]] = None

    def inject_into(self, *, profile: Dict[str, Any]) -> None:
        """Copy the resolved fields into a mutable profile dict.

        ``None`` restrictions are omitted so the SQL firewall can distinguish
        "no constraint declared" from an explicitly empty (deny-all) list.

        Args:
            profile: The dict to populate in place.

        Returns:
            None.
        """
        profile['userId'] = self.user_id
        if self.email:
            profile['email'] = self.email
        if self.groups:
            profile['groups'] = self.groups
        if self.global_roles:
            profile['globalRoles'] = self.global_roles
        if self.resource_roles:
            profile['resourceRoles'] = self.resource_roles
        if self.table_allowlist is not None:
            profile['tableAllowlist'] = self.table_allowlist
        if self.column_denylist:
            profile['columnDenylist'] = self.column_denylist
        if self.allowed_metrics is not None:
            profile['allowedMetrics'] = self.allowed_metrics


def _grants_table_name() -> str:
    """Return the configured grants table name.

    Returns:
        The value of ``GRANTS_TABLE``, or an empty string when unset.
    """
    return os.environ.get('GRANTS_TABLE', '')


def _query_all_grants(*, table: Any, principal_key: str) -> List[Dict[str, Any]]:
    """Return every grant item for ``principal_key``, following all pages.

    Args:
        table: A boto3 DynamoDB ``Table`` resource for the grants table.
        principal_key: The ``PrincipalIndex`` partition key to query.

    Returns:
        All matching items across every page.

    Raises:
        Exception: Any DynamoDB error is propagated — a partial read would
            produce a more permissive profile than the truth.
    """
    from boto3.dynamodb.conditions import Key

    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {
        'IndexName': _PRINCIPAL_INDEX,
        'KeyConditionExpression': Key('principalKey').eq(principal_key),
    }
    while True:
        response = table.query(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            return items
        kwargs['ExclusiveStartKey'] = last_key


def resolve_profile(
    *,
    user_id: str,
    email: str = '',
    groups: Optional[List[str]] = None,
    namespace: str = '',
    table: Any = None,
) -> ResolvedProfile:
    """Resolve roles and, for ``namespace``, the effective data restrictions.

    Queries the ``PrincipalIndex`` GSI for ``User::<sub>``, ``User::<email>``
    and every ``Group::<g>`` key. Grants whose ``resourceId`` is ``GLOBAL``
    become platform roles; the rest become resource roles. Data restrictions are
    merged only across grants on the target namespace (see
    :func:`_merge_data_restrictions`).

    When ``GRANTS_TABLE`` is unset the profile carries only the identity fields
    — the deployment has no grants table, so there are no grants to find. The
    action-level gate does not depend on this function's output for
    Cognito-group-derived platform roles, so an unconfigured table cannot
    escalate anyone.

    Args:
        user_id: The JWT ``sub``.
        email: The JWT ``email`` claim.
        groups: Group memberships from ``cognito:groups``.
        namespace: Namespace id whose data restrictions should be merged. Empty
            resolves roles only.
        table: Optional pre-built DynamoDB ``Table`` resource, for tests.

    Returns:
        The resolved profile.

    Raises:
        Exception: Propagates any grant-lookup failure.
    """
    group_list = list(groups or [])
    result = ResolvedProfile(user_id=user_id, email=email, groups=group_list)

    if table is None:
        table_name = _grants_table_name()
        if not table_name:
            logger.warning(
                'grant_profile_no_table user_id=%s — resolving identity only',
                user_id,
            )
            return result
        table = boto3.resource(
            'dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1')
        ).Table(table_name)

    principal_keys = build_principal_keys(
        user_id=user_id, email=email, groups=group_list
    )

    all_items: List[Dict[str, Any]] = []
    for principal_key in principal_keys:
        try:
            all_items.extend(
                _query_all_grants(table=table, principal_key=principal_key)
            )
        except Exception:
            logger.exception(
                'grant_profile_query_failed principal_key=%s', principal_key
            )
            raise

    for item in all_items:
        role = str(item.get('role') or '')
        resource_id = str(item.get('resourceId') or '')
        if not role:
            continue
        if resource_id == GLOBAL_RESOURCE_ID:
            if role not in result.global_roles:
                result.global_roles.append(role)
        elif resource_id:
            entry = {'role': role, 'resourceUID': resource_id}
            if entry not in result.resource_roles:
                result.resource_roles.append(entry)

    if namespace:
        _merge_data_restrictions(
            result=result, items=all_items, namespace=namespace
        )

    return result


@dataclass
class _Restrictions:
    """Effective data restrictions for one principal in one namespace."""

    table_allowlist: Optional[List[str]] = None
    column_denylist: Optional[Dict[str, List[str]]] = None
    allowed_metrics: Optional[List[str]] = None


def _merge_data_restrictions(
    *, result: ResolvedProfile, items: List[Dict[str, Any]], namespace: str
) -> None:
    """Compute ``namespace``'s effective data restrictions onto ``result``.

    Scoped to grants on this namespace. A ``GLOBAL`` grant — the shape
    ``platform-admin`` / ``platform-viewer`` are written with — does not
    participate: platform privileges flow through ``global_roles`` and drive the
    action-level Cedar layer. Only the data-level merge is namespace-scoped, and
    no role bypasses it.

    Args:
        result: The profile being populated.
        items: Every grant item resolved for the principal.
        namespace: The target namespace id.

    Returns:
        None.
    """
    namespace_grants = [
        item for item in items if str(item.get('resourceId') or '') == namespace
    ]
    if not namespace_grants:
        return

    effective = _restrictive_merge(grants=namespace_grants)
    result.table_allowlist = effective.table_allowlist
    result.column_denylist = effective.column_denylist
    result.allowed_metrics = effective.allowed_metrics


def _restrictive_merge(*, grants: List[Dict[str, Any]]) -> _Restrictions:
    """Deny-wins merge: restrictions accumulate across a namespace's grants.

    - ``columnDenylist`` — UNION. A column is denied if ANY grant denies it.
    - ``tableAllowlist`` / ``allowedMetrics`` — union of the grants that
      *declare* one. A grant declaring none contributes no constraint rather
      than removing every constraint, so access is unrestricted only when NO
      grant declares a list.

    Permissions compose additively — holding two roles grants the union of their
    actions, which is what Cedar does and is correct. Restrictions are
    constraints and compose restrictively. Applying permission semantics to
    constraints is what let one unrestricted grant erase a PII denylist attached
    to another in COA: adding ``namespace-owner`` to an analyst silently stopped
    their ``columnDenylist`` being enforced. Every mature system draws the line
    the same way — an explicit Deny beats any Allow in IAM, ``forbid`` beats
    ``permit`` in Cedar.

    Union rather than intersection for the allowlists is deliberate:
    intersecting a grant allowing ``[a]`` with one allowing ``[b]`` would deny
    both, when the principal was plainly granted each. An explicitly EMPTY
    declared allowlist is honoured as "no tables permitted" — that is an
    administrative choice, unlike absence.

    Args:
        grants: Grant items scoped to a single namespace.

    Returns:
        The merged restrictions.
    """
    denied: Dict[str, Set[str]] = {}
    for grant in grants:
        denylist = grant.get('columnDenylist')
        if not isinstance(denylist, dict):
            continue
        for table, cols in denylist.items():
            if isinstance(cols, (list, tuple, set, frozenset)):
                denied.setdefault(str(table), set()).update(str(c) for c in cols)

    return _Restrictions(
        table_allowlist=_union_of_declared(grants=grants, field_name='tableAllowlist'),
        column_denylist={
            table: sorted(cols) for table, cols in sorted(denied.items()) if cols
        }
        or None,
        allowed_metrics=_union_of_declared(grants=grants, field_name='allowedMetrics'),
    )


def _union_of_declared(
    *, grants: List[Dict[str, Any]], field_name: str
) -> Optional[List[str]]:
    """Union ``field_name`` across the grants that declare it.

    Args:
        grants: Grant items scoped to a single namespace.
        field_name: The grant attribute to union.

    Returns:
        The sorted union, or None when no grant declares the field.
    """
    declared = False
    merged: Set[str] = set()
    for grant in grants:
        value = grant.get(field_name)
        if isinstance(value, (list, tuple, set, frozenset)):
            declared = True
            merged.update(str(v) for v in value)
    return sorted(merged) if declared else None
