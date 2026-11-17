"""Unit tests for the Cedar policy set — the access matrix, per role.

These tests are the executable form of the authorization matrix. Each asserts a
role × action outcome, so a policy edit that widens access fails here rather
than in production.
"""
import os
import sys
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api')
)

from authz.cedar_schema import (  # noqa: E402
    PLATFORM_RESOURCE_ID,
    Action,
    NamespaceStatus,
    Role,
)
from authz.policy_evaluator import (  # noqa: E402
    Decision,
    evaluate,
    load_all_policies,
    load_policies,
)

NS = 'ns-1'
OTHER_NS = 'ns-2'

# Every read action, used to assert the read/write split per role.
READ_ACTIONS = [
    Action.VIEW_NAMESPACE,
    Action.QUERY,
    Action.READ_METRIC,
    Action.READ_RULE,
    Action.VIEW_ONTOLOGY,
    Action.TRAVERSE_GRAPH,
    Action.SEARCH_DOCUMENTS,
    Action.VIEW_EVALUATION,
    Action.VIEW_MONITORING,
]

# Actions a data steward must NOT hold: the separation-of-duties gates and every
# administrative action.
STEWARD_WITHHELD = [
    Action.APPROVE_RULE,
    Action.PUBLISH_RULE,
    Action.PUBLISH_METRIC,
    Action.MANAGE_NAMESPACE,
    Action.SET_NAMESPACE_STATUS,
    Action.DELETE_NAMESPACE,
    Action.GRANT_PERMISSIONS,
    Action.MANAGE_ROLES,
]


def decide(
    *,
    action: Action,
    resource_id: str = NS,
    global_roles: Optional[List[str]] = None,
    resource_roles: Optional[List[Dict[str, str]]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Decision:
    """Evaluate one request against the FULL policy set.

    Using every shipped policy (rather than only the caller's roles) makes each
    assertion a property of the policies themselves, so a rule that leaks access
    to an unrelated role is caught here.

    Args:
        action: The action under test.
        resource_id: Namespace id, or the PLATFORM sentinel.
        global_roles: Platform roles held by the principal.
        resource_roles: Namespace-scoped role entries.
        context: Cedar request context, e.g. ``{'namespaceStatus': 'ARCHIVED'}``.

    Returns:
        The :class:`Decision`.
    """
    return evaluate(
        action=action.value,
        user_id='user-1',
        resource_id=resource_id,
        global_roles=global_roles or [],
        resource_roles=resource_roles or [],
        context=context or {},
        policies=load_all_policies(),
    )


def owner_of(namespace: str) -> List[Dict[str, str]]:
    """Build a namespace-owner grant for one namespace.

    Args:
        namespace: The namespace the role is scoped to.

    Returns:
        A single-entry ``resource_roles`` list.
    """
    return [
        {'role': Role.NAMESPACE_OWNER.value, 'resourceUID': namespace}
    ]


# ── Policy loading ────────────────────────────────────────────────────


def test_default_policy_always_loaded() -> None:
    """The non-ACTIVE guard must not depend on the caller holding a role."""
    assert 'denyAll' in load_policies(roles=[])


def test_unknown_role_is_skipped_not_fatal() -> None:
    """A grant naming a role this build does not ship must not break the rest."""
    policies = load_policies(roles=['not-a-real-role', Role.PLATFORM_ADMIN.value])
    assert 'platform-admin' in policies


def test_every_shipped_policy_parses() -> None:
    """A syntax error in any .cedar file would otherwise surface as a blanket deny."""
    assert decide(
        action=Action.VIEW_NAMESPACE,
        global_roles=[Role.PLATFORM_VIEWER.value],
    ).allowed


# ── Unmapped routes ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    'roles',
    [
        [Role.PLATFORM_ADMIN.value],
        [Role.PLATFORM_VIEWER.value],
        [],
    ],
)
def test_deny_all_sentinel_is_denied_for_every_principal(roles: List[str]) -> None:
    """An unmapped route denies even for platform-admin.

    Regression guard: platform-admin's ``permit (principal, action, resource)``
    has an unconstrained action, so without the explicit ``forbid`` in
    default.cedar the sentinel was ALLOWED for the most privileged principals.
    """
    assert decide(action=Action.DENY_ALL, global_roles=roles).allowed is False


def test_deny_all_denied_for_namespace_owner() -> None:
    """The owner's wildcard permit must not swallow the sentinel either."""
    assert (
        decide(action=Action.DENY_ALL, resource_roles=owner_of(NS)).allowed is False
    )


# ── No roles ──────────────────────────────────────────────────────────


def test_principal_with_no_roles_may_only_list() -> None:
    """Discovery is open to any authenticated caller; nothing else is."""
    assert decide(action=Action.LIST, resource_id=PLATFORM_RESOURCE_ID).allowed
    for action in READ_ACTIONS:
        assert decide(action=action).allowed is False, action


def test_principal_with_no_roles_cannot_write() -> None:
    """Deny-by-default covers every authoring action."""
    for action in (Action.MANAGE_RULE, Action.MANAGE_METRIC, Action.MANAGE_ONTOLOGY):
        assert decide(action=action).allowed is False, action


# ── platform-admin ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    'action',
    [
        Action.MANAGE_NAMESPACE,
        Action.DELETE_NAMESPACE,
        Action.SET_NAMESPACE_STATUS,
        Action.APPROVE_RULE,
        Action.PUBLISH_RULE,
        Action.PUBLISH_METRIC,
        Action.GRANT_PERMISSIONS,
        Action.MANAGE_ROLES,
        Action.MANAGE_ONTOLOGY,
        Action.QUERY,
    ],
)
def test_platform_admin_holds_every_action(action: Action) -> None:
    """platform-admin is the escape hatch and must not need per-namespace grants."""
    assert decide(action=action, global_roles=[Role.PLATFORM_ADMIN.value]).allowed


def test_platform_admin_spans_namespaces() -> None:
    """The admin's permit is not scoped to a resource id."""
    assert decide(
        action=Action.MANAGE_RULE,
        resource_id=OTHER_NS,
        global_roles=[Role.PLATFORM_ADMIN.value],
    ).allowed


# ── platform-viewer ───────────────────────────────────────────────────


@pytest.mark.parametrize('action', READ_ACTIONS)
def test_platform_viewer_reads_everywhere(action: Action) -> None:
    """The migration-safe default role keeps existing users reading and querying."""
    assert decide(action=action, global_roles=[Role.PLATFORM_VIEWER.value]).allowed


@pytest.mark.parametrize(
    'action',
    [
        Action.MANAGE_RULE,
        Action.MANAGE_METRIC,
        Action.MANAGE_ONTOLOGY,
        Action.APPROVE_RULE,
        Action.PUBLISH_RULE,
        Action.PUBLISH_METRIC,
        Action.MANAGE_NAMESPACE,
        Action.DELETE_NAMESPACE,
        Action.GRANT_PERMISSIONS,
    ],
)
def test_platform_viewer_holds_no_writes(action: Action) -> None:
    """Mapping every existing Cognito user to viewer must grant zero writes.

    This is the property that makes switching ENABLE_AUTHZ on non-breaking for
    reads while actually closing the write holes in docs/comparison-coa.md §3.2.
    """
    assert (
        decide(action=action, global_roles=[Role.PLATFORM_VIEWER.value]).allowed
        is False
    )


# ── namespace-owner / namespace-maintainer ────────────────────────────


@pytest.mark.parametrize(
    'action',
    [
        Action.MANAGE_RULE,
        Action.APPROVE_RULE,
        Action.PUBLISH_RULE,
        Action.PUBLISH_METRIC,
        Action.GRANT_PERMISSIONS,
        Action.VIEW_NAMESPACE,
    ],
)
def test_namespace_owner_holds_actions_in_own_namespace(action: Action) -> None:
    """An owner is unrestricted inside the namespace they own."""
    assert decide(action=action, resource_roles=owner_of(NS)).allowed


@pytest.mark.parametrize(
    'action', [Action.MANAGE_RULE, Action.APPROVE_RULE, Action.VIEW_NAMESPACE]
)
def test_namespace_owner_is_scoped_to_its_namespace(action: Action) -> None:
    """The whole point of namespaces: an owner of ns-1 has nothing in ns-2."""
    assert (
        decide(action=action, resource_id=OTHER_NS, resource_roles=owner_of(NS)).allowed
        is False
    )


def test_namespace_owner_cannot_act_on_the_platform() -> None:
    """A namespace role must not grant platform-scoped privileges."""
    assert (
        decide(
            action=Action.MANAGE_NAMESPACE,
            resource_id=PLATFORM_RESOURCE_ID,
            resource_roles=owner_of(NS),
        ).allowed
        is False
    )


def test_namespace_maintainer_matches_owner_inside_the_namespace() -> None:
    """Maintainer is the delegated equivalent of owner within one namespace."""
    roles = [{'role': Role.NAMESPACE_MAINTAINER.value, 'resourceUID': NS}]
    assert decide(action=Action.APPROVE_RULE, resource_roles=roles).allowed
    assert (
        decide(
            action=Action.APPROVE_RULE, resource_id=OTHER_NS, resource_roles=roles
        ).allowed
        is False
    )


# ── namespace-data-steward (separation of duties) ─────────────────────


@pytest.mark.parametrize(
    'action',
    [
        Action.MANAGE_RULE,
        Action.MANAGE_METRIC,
        Action.MANAGE_ONTOLOGY,
        Action.MANAGE_DOCUMENTS,
        Action.MANAGE_GROUND_TRUTH,
        Action.MANAGE_EVALUATION,
        Action.READ_RULE,
        Action.QUERY,
    ],
)
def test_data_steward_may_author(action: Action) -> None:
    """A steward authors content inside their namespace."""
    roles = [{'role': Role.NAMESPACE_DATA_STEWARD.value, 'resourceUID': NS}]
    assert decide(action=action, resource_roles=roles).allowed


@pytest.mark.parametrize('action', STEWARD_WITHHELD)
def test_data_steward_cannot_approve_publish_or_administer(action: Action) -> None:
    """The author of a governed rule must not be able to approve their own work."""
    roles = [{'role': Role.NAMESPACE_DATA_STEWARD.value, 'resourceUID': NS}]
    assert decide(action=action, resource_roles=roles).allowed is False


# ── namespace-data-analyst ────────────────────────────────────────────


@pytest.mark.parametrize('action', READ_ACTIONS)
def test_data_analyst_reads_its_namespace(action: Action) -> None:
    """The least-privileged namespace role: reads and queries only."""
    roles = [{'role': Role.NAMESPACE_DATA_ANALYST.value, 'resourceUID': NS}]
    assert decide(action=action, resource_roles=roles).allowed


@pytest.mark.parametrize(
    'action', [Action.MANAGE_RULE, Action.MANAGE_METRIC, Action.APPROVE_RULE]
)
def test_data_analyst_holds_no_writes(action: Action) -> None:
    """An analyst cannot author anything."""
    roles = [{'role': Role.NAMESPACE_DATA_ANALYST.value, 'resourceUID': NS}]
    assert decide(action=action, resource_roles=roles).allowed is False


def test_data_analyst_cannot_read_another_namespace() -> None:
    """Namespace scoping applies to reads, not only to writes."""
    roles = [{'role': Role.NAMESPACE_DATA_ANALYST.value, 'resourceUID': NS}]
    assert (
        decide(
            action=Action.READ_METRIC, resource_id=OTHER_NS, resource_roles=roles
        ).allowed
        is False
    )


# ── Non-ACTIVE namespace guard ────────────────────────────────────────


@pytest.mark.parametrize(
    'status',
    [
        NamespaceStatus.ARCHIVED,
        NamespaceStatus.DELETING,
        NamespaceStatus.DELETE_FAILED,
        NamespaceStatus.DELETED,
    ],
)
def test_mutations_forbidden_outside_active(status: NamespaceStatus) -> None:
    """The forbid overrides platform-admin — a frozen namespace stays frozen."""
    assert (
        decide(
            action=Action.MANAGE_RULE,
            global_roles=[Role.PLATFORM_ADMIN.value],
            context={'namespaceStatus': status.value},
        ).allowed
        is False
    )


@pytest.mark.parametrize('action', READ_ACTIONS)
def test_reads_still_allowed_outside_active(action: Action) -> None:
    """An archived namespace is retained to be read; blocking reads would defeat that."""
    assert decide(
        action=action,
        global_roles=[Role.PLATFORM_VIEWER.value],
        context={'namespaceStatus': NamespaceStatus.ARCHIVED.value},
    ).allowed


@pytest.mark.parametrize(
    'action', [Action.SET_NAMESPACE_STATUS, Action.DELETE_NAMESPACE]
)
def test_lifecycle_actions_survive_a_non_active_status(action: Action) -> None:
    """Otherwise an archived namespace could never be reactivated or torn down."""
    assert decide(
        action=action,
        global_roles=[Role.PLATFORM_ADMIN.value],
        context={'namespaceStatus': NamespaceStatus.ARCHIVED.value},
    ).allowed


def test_active_status_does_not_block_mutations() -> None:
    """The guard is conditional on the status, not on the attribute being present."""
    assert decide(
        action=Action.MANAGE_RULE,
        resource_roles=owner_of(NS),
        context={'namespaceStatus': NamespaceStatus.ACTIVE.value},
    ).allowed


def test_absent_status_does_not_block_platform_actions() -> None:
    """`context has namespaceStatus` keeps PLATFORM-scoped creates unaffected."""
    assert decide(
        action=Action.MANAGE_NAMESPACE,
        resource_id=PLATFORM_RESOURCE_ID,
        global_roles=[Role.PLATFORM_ADMIN.value],
    ).allowed


# ── Fail-closed behaviour ─────────────────────────────────────────────


def test_malformed_policy_text_denies() -> None:
    """A policy the engine rejects must produce a Deny, not an exception."""
    decision = evaluate(
        action=Action.QUERY.value,
        user_id='user-1',
        resource_id=NS,
        policies='this is not cedar',
    )
    assert decision.allowed is False


def test_unknown_action_denies() -> None:
    """An action no policy names is denied."""
    decision = evaluate(
        action='notARealAction',
        user_id='user-1',
        resource_id=NS,
        global_roles=[Role.PLATFORM_VIEWER.value],
        policies=load_all_policies(),
    )
    assert decision.allowed is False
