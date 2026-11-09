"""Cedar schema constants for the Semantic Layer authorization model.

Mirrors the Context Ontology Accelerator's ``coa_authorization.cedar_schema``
so the two projects' access matrices stay comparable, with SL's own action set.

Only ONE resource entity type participates in decisions: ``SL::Namespace``. The
namespace is the governance boundary, and the *object* being acted on is carried
by the action (``manageMetric``, ``approveRule``, …) rather than by a distinct
entity type. COA's policies reduce to the same thing — every one of its seed
policies matches ``resource`` unconstrained and conditions on ``resource.id`` or
``resource.namespace``. Keeping a single entity type means a new sub-resource
(say, a lesson) needs a new action, not a new entity plus seven policy edits.
"""

from __future__ import annotations

from enum import StrEnum

SL_SCHEMA = 'SL'
"""Cedar namespace prefix qualifying every entity and action reference."""

PLATFORM_RESOURCE_ID = 'PLATFORM'
"""Sentinel resource id for platform-scoped routes that name no namespace.

Used by ``/ontology/list``, ``/datasource/glue/databases``, ``POST /namespaces``
and similar. No ``namespaceStatus`` is attached to a PLATFORM decision, so the
non-ACTIVE mutation guard in ``default.cedar`` cannot misfire on a route that
has no namespace yet.
"""


class EntityType(StrEnum):
    """Cedar entity types in the SL authorization model.

    Members:
        USER: The authenticated principal.
        NAMESPACE: The governance boundary every decision is evaluated against.
    """

    USER = 'User'
    NAMESPACE = 'Namespace'


class Action(StrEnum):
    """Cedar actions controlling access to SL surfaces.

    Values are bare action names; ``policy_evaluator`` qualifies them with
    :data:`SL_SCHEMA`.

    ``approveRule``, ``publishRule`` and ``publishMetric`` are deliberately
    separate from the authoring action ``manageRule`` / ``manageMetric``. That
    separation of duties is the specific hole ``docs/comparison-coa.md`` §3.2
    names: the governed-rules approval gate was open to every authenticated
    user. A data steward can now author a rule without being able to approve
    their own.
    """

    # Namespace lifecycle
    VIEW_NAMESPACE = 'viewNamespace'
    MANAGE_NAMESPACE = 'manageNamespace'
    SET_NAMESPACE_STATUS = 'setNamespaceStatus'
    DELETE_NAMESPACE = 'deleteNamespace'

    # Discovery
    LIST = 'list'

    # Reads
    QUERY = 'query'
    READ_METRIC = 'readMetric'
    READ_RULE = 'readRule'
    VIEW_ONTOLOGY = 'viewOntology'
    TRAVERSE_GRAPH = 'traverseGraph'
    SEARCH_DOCUMENTS = 'searchDocuments'
    VIEW_EVALUATION = 'viewEvaluation'
    VIEW_MONITORING = 'viewMonitoring'

    # Authoring writes
    MANAGE_ONTOLOGY = 'manageOntology'
    MANAGE_METRIC = 'manageMetric'
    MANAGE_RULE = 'manageRule'
    MANAGE_DOCUMENTS = 'manageDocuments'
    MANAGE_GROUND_TRUTH = 'manageGroundTruth'
    MANAGE_EVALUATION = 'manageEvaluation'
    MANAGE_FEEDBACK = 'manageFeedback'
    MANAGE_LESSONS = 'manageLessons'
    MANAGE_DATA_SOURCE = 'manageDataSource'

    # Governed-publication gates (separation of duties)
    APPROVE_RULE = 'approveRule'
    PUBLISH_RULE = 'publishRule'
    PUBLISH_METRIC = 'publishMetric'

    # Administrative
    GRANT_PERMISSIONS = 'grantPermissions'
    MANAGE_ROLES = 'manageRoles'

    # Sentinel for an unmapped route. No policy permits it, so mapping a route
    # to DENY_ALL — or failing to map it at all — denies.
    DENY_ALL = 'denyAll'


class Role(StrEnum):
    """Role identifiers as stored in the grants table.

    Kebab-case, matching COA, so a grant row written for one project reads the
    same in the other.
    """

    PLATFORM_ADMIN = 'platform-admin'
    PLATFORM_VIEWER = 'platform-viewer'
    NAMESPACE_OWNER = 'namespace-owner'
    NAMESPACE_MAINTAINER = 'namespace-maintainer'
    NAMESPACE_DATA_STEWARD = 'namespace-data-steward'
    NAMESPACE_DATA_ANALYST = 'namespace-data-analyst'


class NamespaceStatus(StrEnum):
    """Namespace lifecycle states.

    ``DELETING`` and ``DELETE_FAILED`` exist so the non-ACTIVE mutation guard
    and the UI need no change when an asynchronous deletion pipeline lands; the
    synchronous implementation only transitions between the other three.
    """

    ACTIVE = 'ACTIVE'
    ARCHIVED = 'ARCHIVED'
    DELETING = 'DELETING'
    DELETED = 'DELETED'
    DELETE_FAILED = 'DELETE_FAILED'


# Every role's policy file, resolved relative to ``authz/policies/``. The
# evaluator always loads ``default`` in addition to the principal's roles, so
# the non-ACTIVE mutation guard participates in every decision.
DEFAULT_POLICY_NAME = 'default'

ROLE_POLICY_FILES: dict[str, str] = {
    Role.PLATFORM_ADMIN: 'platform_admin',
    Role.PLATFORM_VIEWER: 'platform_viewer',
    Role.NAMESPACE_OWNER: 'namespace_owner',
    Role.NAMESPACE_MAINTAINER: 'namespace_maintainer',
    Role.NAMESPACE_DATA_STEWARD: 'namespace_data_steward',
    Role.NAMESPACE_DATA_ANALYST: 'namespace_data_analyst',
}
