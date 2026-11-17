"""Per-invocation binding of the caller's grant profile for the SQL firewall.

The firewall (:mod:`agents.shared.sql_firewall`) needs the caller's resolved
restrictions, but the sites that execute SQL are Strands ``@tool`` functions and
module-level helpers whose signatures are fixed by the framework — the profile
cannot be threaded down as a parameter. This module holds it in a
:class:`~contextvars.ContextVar` instead, bound once per agent invocation at the
entrypoint and read at each execution site.

A ``ContextVar`` rather than a module global: an AgentCore container serves
concurrent invocations, and a plain global would let one caller's restrictions
leak into another caller's query. ``ContextVar`` reads are isolated per task.

**The profile is resolved here, never accepted from the payload.** The
restrictions are the security decision; taking them from the request would let a
caller declare their own. :func:`bind_caller` resolves them from the grants table
using the JWT-derived ``user_id`` the entrypoint already established as trusted.

**Unbound means structural-only, and says so.** With nothing bound,
:func:`current_profile` returns ``None`` and the firewall applies only its
always-on structural checks. That is the documented sparse-opt-in posture (see
``docs/plans/2026-08-23-namespaces-authz-firewall-design.md`` §3.3): action- and
namespace-level authorization is enforced upstream by ``AuthzMiddleware``, and a
deployment with no grants declaring restrictions has no data-level constraint to
apply. It is not a fallback for a *failed* resolve — :func:`bind_caller`
propagates a grants-table read failure, so an unreadable table fails the
invocation rather than quietly downgrading it to structural-only.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar, Token
from typing import Any, Dict, List, Optional

try:  # Container layouts differ: flat (`shared.*`) vs repo-rooted (`agents.shared.*`).
    from agents.shared.grant_profile import resolve_profile
    from agents.shared.sql_firewall import enforce_sql
except ImportError:  # pragma: no cover - exercised only inside the agent images
    from shared.grant_profile import resolve_profile  # type: ignore
    from shared.sql_firewall import enforce_sql  # type: ignore

logger = logging.getLogger(__name__)

# Default None (not {}) so "nothing was bound" is distinguishable from "bound a
# profile that happens to declare no restrictions". Only the former is worth a
# log line at the execution site.
_PROFILE: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    'sql_firewall_profile', default=None
)
_NAMESPACE: ContextVar[str] = ContextVar('sql_firewall_namespace', default='')


def bind_caller(
    *,
    user_id: str,
    email: str = '',
    groups: Optional[List[str]] = None,
    namespace: str = '',
) -> Dict[str, Any]:
    """Resolve the caller's grant profile and bind it for this invocation.

    Args:
        user_id: The JWT ``sub`` the entrypoint established as trusted. Never a
            payload-supplied identity — the restrictions keyed off it are the
            security decision.
        email: The JWT ``email`` claim, so grants written against an address
            resolve as well as grants written against the subject.
        groups: ``cognito:groups`` memberships, for group-addressed grants.
        namespace: The namespace id whose data restrictions apply. Empty
            resolves roles only, leaving no table/column constraint to enforce.

    Returns:
        The bound profile dict, in the shape the firewall reads
        (``tableAllowlist`` / ``columnDenylist`` / ``allowedMetrics``, each
        present only when some grant declared it).

    Raises:
        Exception: Propagates any grants-table read failure from
            :func:`~agents.shared.grant_profile.resolve_profile`. Deliberately
            not caught: a profile we could not read is not a profile with no
            restrictions, and treating it as one would run the caller's SQL
            unrestricted.
    """
    resolved = resolve_profile(
        user_id=user_id, email=email, groups=groups, namespace=namespace
    )
    profile: Dict[str, Any] = {}
    resolved.inject_into(profile=profile)
    _PROFILE.set(profile)
    _NAMESPACE.set(namespace)
    logger.info(
        'firewall_context_bound user_id=%s namespace=%s restricted=%s',
        user_id,
        namespace or '-',
        any(
            key in profile
            for key in ('tableAllowlist', 'columnDenylist', 'allowedMetrics')
        ),
    )
    return profile


def namespace_for_ontology(*, ontology_id: str) -> str:
    """Resolve the namespace an ontology is bound to, reading it server-side.

    Deliberately not taken from the invocation payload. Data restrictions are
    merged per namespace, so a caller who could name the namespace could name one
    where their grants are unrestricted and escape the restrictions on the
    namespace they are actually querying. The binding is read from the ontology's
    own config row, which only the REST API writes.

    Duplicates the "highest version wins" convention each agent implements for
    its own config reads. Kept separate on purpose: this copy is the one an
    authorization decision depends on, and it must not change because an
    unrelated read path was refactored.

    Args:
        ontology_id: The semantic layer / ontology id being queried.

    Returns:
        The bound namespace id, or an empty string when the ontology has no
        binding or the metadata table is not configured — in which case no
        table/column restriction is merged and the firewall's structural checks
        are the whole gate.
    """
    if not ontology_id:
        return ''
    table_name = os.environ.get('ONTOLOGY_METADATA_TABLE', '')
    if not table_name:
        logger.warning(
            'firewall_context_no_metadata_table — cannot resolve the namespace for '
            'ontology %s, so no data restrictions will be merged',
            ontology_id,
        )
        return ''
    import boto3
    from boto3.dynamodb.conditions import Key

    table = boto3.resource(
        'dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1')
    ).Table(table_name)
    items = table.query(KeyConditionExpression=Key('id').eq(ontology_id)).get(
        'Items', []
    )
    if not items:
        logger.warning(
            'firewall_context_unknown_ontology id=%s — no config row', ontology_id
        )
        return ''

    def _version_num(item: Dict[str, Any]) -> int:
        """Return an item's numeric version, tolerating a malformed value.

        Args:
            item: A metadata config row.

        Returns:
            The integer suffix of ``version`` ('v11' -> 11), or 0.
        """
        try:
            return int(str(item.get('version', 'v0')).lstrip('v'))
        except ValueError:
            return 0

    return str(max(items, key=_version_num).get('namespaceId') or '')


def _claims_from_context(*, context: Any) -> Dict[str, Any]:
    """Decode the forwarded Bearer token's claims without re-verifying them.

    AgentCore validated the token before the runtime was invoked, so this only
    needs to read it. Mirrors each agent's ``_user_id_from_context``, kept here
    so the three agents share one decoder rather than three near-copies.

    Args:
        context: The AgentCore invocation context, which may carry
            ``request_headers``.

    Returns:
        The decoded claim set, or an empty dict when there is no Bearer token or
        it is malformed. Only ``email`` and ``cognito:groups`` are read from it —
        the subject comes from the caller, which has already decided its own
        identity precedence.
    """
    import base64
    import json as _json

    headers = (getattr(context, 'request_headers', None) or {}) if context else {}
    auth = headers.get('Authorization') or headers.get('authorization') or ''
    if not auth.startswith('Bearer '):
        return {}
    try:
        segment = auth[len('Bearer ') :].split('.')[1]
        # Re-pad the base64url segment so urlsafe_b64decode accepts it.
        segment += '=' * (-len(segment) % 4)
        decoded = _json.loads(base64.urlsafe_b64decode(segment))
    except Exception:  # noqa: BLE001 — a malformed token must not break the turn
        logger.warning('firewall_context_claims_undecodable')
        return {}
    return decoded if isinstance(decoded, dict) else {}


def bind_from_context(
    *, context: Any, user_id: str, namespace: str = ''
) -> Dict[str, Any]:
    """Bind the caller's profile from an AgentCore invocation context.

    The one-line form the agent entrypoints call. ``user_id`` is passed in
    rather than decoded here because each agent has already established which
    subject it trusts (JWT over payload), and that decision must not be
    silently re-made with different precedence.

    Args:
        context: The AgentCore invocation context carrying the Bearer token.
        user_id: The subject the caller established as trusted.
        namespace: The namespace id whose data restrictions apply.

    Returns:
        The bound profile dict.

    Raises:
        Exception: Propagates a grants-table read failure — see
            :func:`bind_caller`.
    """
    claims = _claims_from_context(context=context)
    raw_groups = claims.get('cognito:groups')
    # The HTTP API JWT authorizer stringifies multi-valued claims, so the same
    # claim can arrive as a list (direct token decode) or as "[Admin Users]".
    if isinstance(raw_groups, str):
        groups = raw_groups.strip('[]').split()
    elif isinstance(raw_groups, (list, tuple)):
        groups = [str(g) for g in raw_groups]
    else:
        groups = []
    return bind_caller(
        user_id=user_id,
        email=str(claims.get('email') or ''),
        groups=groups,
        namespace=namespace,
    )


def bind_profile(*, profile: Dict[str, Any], namespace: str = '') -> Token:
    """Bind an already-resolved profile, returning a reset token.

    For callers that resolved the profile themselves — the REST API does, in
    ``AuthzMiddleware`` — so the work is not repeated, and for tests.

    Args:
        profile: The firewall-shaped profile dict to bind.
        namespace: The namespace id the query runs in.

    Returns:
        The ContextVar token for the profile binding, for :func:`reset`.
    """
    _NAMESPACE.set(namespace)
    return _PROFILE.set(profile)


def reset(*, token: Token) -> None:
    """Restore the profile binding a :func:`bind_profile` call replaced.

    Args:
        token: The token that call returned.

    Returns:
        None.
    """
    _PROFILE.reset(token)


def clear_caller() -> None:
    """Drop the bound profile.

    Called when an invocation ends, so a container reused for an unauthenticated
    or system path does not inherit the previous caller's restrictions.

    Returns:
        None.
    """
    _PROFILE.set(None)
    _NAMESPACE.set('')


def current_profile() -> Optional[Dict[str, Any]]:
    """Return the profile bound for this invocation.

    Returns:
        The bound profile, or None when nothing was bound (structural checks
        only — see the module docstring).
    """
    return _PROFILE.get()


def current_namespace() -> str:
    """Return the namespace bound for this invocation.

    Returns:
        The bound namespace id, or an empty string.
    """
    return _NAMESPACE.get()


def enforce_for_caller(*, sql: str, dialect: str = 'athena') -> str:
    """Run the firewall over ``sql`` using whatever profile is bound.

    The single call every wired execution site makes, so the profile lookup and
    the audit log line are identical everywhere rather than re-derived per site.

    Args:
        sql: The statement about to be executed.
        dialect: sqlglot read-dialect. Sites executing SQL already transpiled
            for another engine must pass that engine's dialect, or a valid
            statement is rejected as unparseable.

    Returns:
        The SQL cleared for execution.

    Raises:
        UnsafeSQLError: If the statement fails structural validation or the
            bound profile denies its tables/columns.
    """
    profile = current_profile()
    if profile is None:
        logger.info(
            'firewall_context_unbound — structural checks only for this statement'
        )
    return enforce_sql(
        sql=sql, profile=profile, namespace=current_namespace(), dialect=dialect
    )


def check_metric_allowed(*, metric_id: str) -> None:
    """Reject ``metric_id`` when the bound profile does not permit it.

    ``allowedMetrics`` is enforced here rather than in the firewall because a
    metric is authorized by identity, not by the shape of the SQL it compiles
    to. Absent means unrestricted; an explicitly empty list means no metric is
    permitted — collapsing the two would make a deny-all grant fail open.

    Args:
        metric_id: The governed metric the caller asked to execute.

    Returns:
        None.

    Raises:
        PermissionError: If the profile declares ``allowedMetrics`` and
            ``metric_id`` is not in it.
    """
    profile = current_profile()
    if profile is None:
        return
    allowed = profile.get('allowedMetrics')
    if allowed is None:
        return
    if metric_id not in set(allowed):
        logger.warning(
            'metric_denied metric_id=%s namespace=%s', metric_id, current_namespace()
        )
        raise PermissionError(f"Access denied to metric '{metric_id}'")


__all__ = [
    'bind_caller',
    'bind_from_context',
    'bind_profile',
    'check_metric_allowed',
    'clear_caller',
    'current_namespace',
    'current_profile',
    'enforce_for_caller',
    'namespace_for_ontology',
    'reset',
]
