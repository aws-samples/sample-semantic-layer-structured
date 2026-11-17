"""Namespaces as a first-class primitive: lifecycle, name uniqueness, grants.

Before this module a "namespace" was a bare string that rules and metrics keyed
themselves by — anyone could invent one, nothing recorded who owned it, and
nothing enforced its lifecycle. This service makes it a real resource:

* **Registered.** A namespace exists only if it has a metadata row, so
  ``/rules/namespaces/<anything>/rules`` can no longer conjure one into being.
* **Uniquely named.** The name is claimed with a conditional write, so two
  concurrent creates cannot both win.
* **Lifecycled.** ACTIVE / ARCHIVED / DELETING / DELETE_FAILED / DELETED. The
  Cedar ``default`` policy forbids mutation outside ACTIVE, so archiving a
  namespace freezes it for everyone including platform-admin.
* **Owned.** Creation writes a ``namespace-owner`` grant for the creator, which
  is what makes the namespace-scoped Cedar policies resolve to anything.
* **Physically isolated.** Each namespace gets its own Athena workgroup with
  ``EnforceWorkGroupConfiguration``, so query results land under a
  namespace-specific S3 prefix rather than in one shared output location.

Two DynamoDB tables:

``NAMESPACES_TABLE`` (pk, sk)
    ``NS#<id> / METADATA``       — the namespace record.
    ``NS_NAME#<lower(name)> / RESERVATION`` — the name claim.

``GRANTS_TABLE`` (pk, sk + ``PrincipalIndex`` GSI on ``principalKey``)
    ``RES#<resourceId> / <principalKey>#<role>`` — one grant. The base table
    answers "who has access to this namespace"; the GSI answers "what can this
    principal do", which is the query
    :mod:`agents.shared.grant_profile` runs on every request.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from agents.shared.grant_profile import GLOBAL_RESOURCE_ID, sanitize_principal_key

from authz.cedar_schema import NamespaceStatus, Role
from authz.role_resolver import clear_cache as clear_role_cache

logger = logging.getLogger(__name__)

# A namespace name is used in an Athena workgroup name and in S3 prefixes, so it
# is restricted to characters that are safe in both without escaping.
_NAME_PATTERN = re.compile(r'^[a-z0-9][a-z0-9._-]{1,62}$')

_METADATA_SK = 'METADATA'
_RESERVATION_SK = 'RESERVATION'
_NS_PK_PREFIX = 'NS#'
_NAME_PK_PREFIX = 'NS_NAME#'
_GRANT_PK_PREFIX = 'RES#'

# How long an in-flight name claim survives if the create crashes between
# claiming the name and writing the namespace row. Without the TTL an abandoned
# create would burn the name permanently; with it, the name frees itself.
_RESERVATION_TTL_SECONDS = 300

# Statuses in which the namespace still exists as a resource. DELETED rows are
# retained as tombstones so a re-create under the same name is visibly a new
# namespace rather than a resurrection of the old one's grants.
_LIVE_STATUSES = frozenset(
    {
        NamespaceStatus.ACTIVE.value,
        NamespaceStatus.ARCHIVED.value,
        NamespaceStatus.DELETING.value,
        NamespaceStatus.DELETE_FAILED.value,
    }
)

# Status transitions the API permits. DELETING / DELETE_FAILED are set by the
# delete path, never by a caller, so they are not transition targets here.
_ALLOWED_TRANSITIONS: Dict[str, frozenset] = {
    NamespaceStatus.ACTIVE.value: frozenset({NamespaceStatus.ARCHIVED.value}),
    NamespaceStatus.ARCHIVED.value: frozenset({NamespaceStatus.ACTIVE.value}),
    NamespaceStatus.DELETE_FAILED.value: frozenset({NamespaceStatus.ARCHIVED.value}),
}


class NamespaceError(RuntimeError):
    """Base class for namespace service failures."""


class NamespaceNameTaken(NamespaceError):
    """Raised when the requested namespace name is already claimed."""


class NamespaceNotFound(NamespaceError):
    """Raised when no namespace matches the given id or name."""


class InvalidNamespaceName(NamespaceError):
    """Raised when a proposed name would be unsafe in a workgroup or S3 prefix."""


class InvalidStatusTransition(NamespaceError):
    """Raised when a requested lifecycle transition is not permitted."""


class Namespace(BaseModel):
    """A namespace record.

    Attributes:
        id: Immutable identifier (a UUID). Everything else may change.
        name: Human-readable unique name, used in the Athena workgroup name.
        description: Free-text purpose.
        status: Lifecycle status; see :class:`~authz.cedar_schema.NamespaceStatus`.
        workgroup: The Athena workgroup backing this namespace, or ``''`` when
            workgroup provisioning has not run.
        resultLocation: The S3 prefix the workgroup writes results to.
        createdBy: JWT ``sub`` of the creator.
        createdAt: ISO-8601 UTC creation timestamp.
        updatedAt: ISO-8601 UTC timestamp of the last change.
    """

    id: str
    name: str
    description: str = ''
    status: str = NamespaceStatus.ACTIVE.value
    workgroup: str = ''
    resultLocation: str = ''
    createdBy: str = ''
    createdAt: str = ''
    updatedAt: str = ''


class NamespaceCreate(BaseModel):
    """Request body for namespace creation.

    Attributes:
        name: The requested unique name.
        description: Optional free-text purpose.
    """

    name: str
    description: str = ''


class NamespaceUpdate(BaseModel):
    """Request body for a namespace metadata update.

    Only the description is mutable: the id is the durable handle, and the name
    is embedded in an Athena workgroup name that cannot be renamed in place.

    Attributes:
        description: The new description.
    """

    description: str = ''


class StatusUpdate(BaseModel):
    """Request body for a lifecycle transition.

    Attributes:
        status: The target status.
    """

    status: str


class GrantCreate(BaseModel):
    """Request body for granting a role.

    Attributes:
        principalType: ``User`` or ``Group``.
        principalId: A JWT ``sub``, an email address, or a Cognito group name.
        role: A role identifier from :class:`~authz.cedar_schema.Role`.
        tableAllowlist: Optional table allowlist for the SQL firewall. ``None``
            declares no constraint; ``[]`` denies every table.
        columnDenylist: Optional ``{table: [column, …]}`` denylist.
        allowedMetrics: Optional metric allowlist.
    """

    principalType: str = 'User'
    principalId: str
    role: str
    tableAllowlist: Optional[List[str]] = None
    columnDenylist: Optional[Dict[str, List[str]]] = None
    allowedMetrics: Optional[List[str]] = None


class Grant(BaseModel):
    """A stored grant, as returned by the API.

    Attributes:
        principalKey: The ``<type>::<id>`` key the grant is stored under.
        resourceId: The namespace id, or ``GLOBAL``.
        role: The granted role identifier.
        grantedBy: JWT ``sub`` of the granter.
        grantedAt: ISO-8601 UTC timestamp.
        tableAllowlist: The declared table allowlist, if any.
        columnDenylist: The declared column denylist, if any.
        allowedMetrics: The declared metric allowlist, if any.
    """

    principalKey: str
    resourceId: str
    role: str
    grantedBy: str = ''
    grantedAt: str = ''
    tableAllowlist: Optional[List[str]] = None
    columnDenylist: Optional[Dict[str, List[str]]] = None
    allowedMetrics: Optional[List[str]] = Field(default=None)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Returns:
        The timestamp, e.g. ``2026-08-23T21:15:00+00:00``.
    """
    return datetime.now(timezone.utc).isoformat()


def build_principal_key(*, principal_type: str, principal_id: str) -> str:
    """Build the grants-table principal key for one identity.

    Uses the same encoder as the reader
    (:func:`agents.shared.grant_profile.sanitize_principal_key`) — a writer that
    encodes differently produces grants the resolver can never find, which
    presents as "the grant had no effect" with no error anywhere.

    Args:
        principal_type: ``User`` or ``Group``.
        principal_id: A ``sub``, email address, or group name.

    Returns:
        The ``<type>::<encoded id>`` key.

    Raises:
        ValueError: If the type is not User/Group or the id is empty.
    """
    normalized = principal_type.strip().capitalize()
    if normalized not in ('User', 'Group'):
        raise ValueError(f'principalType must be User or Group, got {principal_type!r}')
    if not principal_id.strip():
        raise ValueError('principalId must not be empty')
    return f'{normalized}::{sanitize_principal_key(value=principal_id)}'


def validate_namespace_name(*, name: str) -> str:
    """Validate and normalise a proposed namespace name.

    Args:
        name: The requested name.

    Returns:
        The lower-cased name.

    Raises:
        InvalidNamespaceName: If the name would be unsafe in an Athena workgroup
            name or an S3 prefix. Rejecting here rather than escaping later keeps
            one canonical spelling of the name across DynamoDB, Athena and S3.
    """
    candidate = name.strip().lower()
    if not _NAME_PATTERN.match(candidate):
        raise InvalidNamespaceName(
            'namespace name must be 2-63 characters of a-z, 0-9, dot, dash or '
            f'underscore and start with a letter or digit (got {name!r})'
        )
    return candidate


class NamespaceService:
    """DynamoDB-backed namespace registry, lifecycle and grant administration.

    Attributes:
        namespaces: The namespaces DynamoDB ``Table`` resource.
        grants: The grants DynamoDB ``Table`` resource.
        athena: A boto3 Athena client, or None when workgroup provisioning is
            not configured.
        results_bucket: S3 bucket for per-namespace Athena results.
        resource_prefix: Prefix for the generated Athena workgroup name.
    """

    def __init__(
        self,
        *,
        namespaces_table: Any = None,
        grants_table: Any = None,
        athena_client: Any = None,
        results_bucket: Optional[str] = None,
        resource_prefix: Optional[str] = None,
    ) -> None:
        """Build the service, resolving anything not injected from the environment.

        Args:
            namespaces_table: Pre-built DynamoDB ``Table`` for namespaces.
            grants_table: Pre-built DynamoDB ``Table`` for grants.
            athena_client: Pre-built boto3 Athena client.
            results_bucket: Override for ``ATHENA_RESULTS_BUCKET``.
            resource_prefix: Override for ``RESOURCE_PREFIX``.

        Raises:
            NamespaceError: If a required table name is not configured. Running
                without a namespaces table would silently reduce namespaces to
                the unregistered strings they were before.
        """
        region = os.environ.get('AWS_REGION', 'us-east-1')
        if namespaces_table is None:
            table_name = os.environ.get('NAMESPACES_TABLE', '')
            if not table_name:
                raise NamespaceError('NAMESPACES_TABLE is not configured')
            namespaces_table = boto3.resource(
                'dynamodb', region_name=region
            ).Table(table_name)
        if grants_table is None:
            grants_name = os.environ.get('GRANTS_TABLE', '')
            if not grants_name:
                raise NamespaceError('GRANTS_TABLE is not configured')
            grants_table = boto3.resource('dynamodb', region_name=region).Table(
                grants_name
            )
        self.namespaces = namespaces_table
        self.grants = grants_table
        self.athena = athena_client
        self.results_bucket = (
            results_bucket
            if results_bucket is not None
            else os.environ.get('ATHENA_RESULTS_BUCKET', '')
        )
        self.resource_prefix = (
            resource_prefix
            if resource_prefix is not None
            else os.environ.get('RESOURCE_PREFIX', 'semantic-layer')
        )

    # ── Reads ─────────────────────────────────────────────────────────

    def get(self, *, namespace_id: str) -> Optional[Namespace]:
        """Fetch a namespace by id.

        Args:
            namespace_id: The namespace id.

        Returns:
            The namespace, or None when no record exists.
        """
        response = self.namespaces.get_item(
            Key={'pk': f'{_NS_PK_PREFIX}{namespace_id}', 'sk': _METADATA_SK}
        )
        item = response.get('Item')
        return self._to_model(item=item) if item else None

    def get_by_name(self, *, name: str) -> Optional[Namespace]:
        """Fetch a namespace by name.

        Args:
            name: The namespace name, any case.

        Returns:
            The namespace, or None when the name is unclaimed or its claim is
            still an in-flight reservation.
        """
        response = self.namespaces.get_item(
            Key={
                'pk': f'{_NAME_PK_PREFIX}{name.strip().lower()}',
                'sk': _RESERVATION_SK,
            }
        )
        item = response.get('Item')
        if not item:
            return None
        return self.get(namespace_id=str(item.get('namespaceId') or ''))

    def resolve(self, *, identifier: str) -> Optional[Namespace]:
        """Resolve a namespace by id or by name.

        The API accepts either because ``/rules/namespaces/{ns}/…`` and
        ``/metrics/namespaces/{ns}/…`` predate namespace ids and callers already
        pass names there. Ids are tried first so a name that happens to look like
        an id cannot shadow one.

        Args:
            identifier: A namespace id or name.

        Returns:
            The namespace, or None when nothing matches.
        """
        if not identifier:
            return None
        found = self.get(namespace_id=identifier)
        if found is not None:
            return found
        return self.get_by_name(name=identifier)

    def status_of(self, *, identifier: str) -> Optional[str]:
        """Return a namespace's lifecycle status.

        This is the resolver the authorization middleware calls, so its
        ``None`` return is what produces a 403 for an unknown namespace.

        Args:
            identifier: A namespace id or name.

        Returns:
            The status string, or None when the namespace does not exist.
        """
        found = self.resolve(identifier=identifier)
        return found.status if found is not None else None

    def list(self) -> List[Namespace]:
        """List every namespace that still exists.

        Uses a paginated Scan: the namespace count is operator-scale (tens), and
        a Scan avoids adding a GSI whose only purpose is to enumerate a small
        table. DELETED tombstones are excluded.

        Returns:
            Every live namespace, sorted by name.
        """
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {
            # Attr, not Key: a Scan's FilterExpression is evaluated after the read,
            # so it is an attribute condition even when the attribute is a key.
            'FilterExpression': Attr('sk').eq(_METADATA_SK),
        }
        while True:
            response = self.namespaces.scan(**kwargs)
            items.extend(response.get('Items', []))
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                break
            kwargs['ExclusiveStartKey'] = last_key
        found = [
            self._to_model(item=item)
            for item in items
            if str(item.get('status') or '') in _LIVE_STATUSES
        ]
        return sorted(found, key=lambda ns: ns.name)

    # ── Create ────────────────────────────────────────────────────────

    def create(
        self, *, request: NamespaceCreate, created_by: str, created_by_email: str = ''
    ) -> Namespace:
        """Register a namespace, provision its workgroup, and grant ownership.

        Ordered so that a failure never leaves a half-usable namespace:

        1. Claim the name with a conditional write, carrying a TTL. Two
           concurrent creates cannot both succeed.
        2. Provision the Athena workgroup.
        3. Write the namespace record (status ACTIVE).
        4. Grant the creator ``namespace-owner`` — without this the creator
           could not administer what they just made.
        5. Make the name claim permanent by removing its TTL.

        If any of 2-4 fails the name claim is released immediately and the
        original error propagates; the TTL is the backstop if that release also
        fails.

        Args:
            request: The create request.
            created_by: JWT ``sub`` of the creator.
            created_by_email: Email of the creator, granted alongside the sub so
                the owner grant resolves whichever identifier their token
                surfaces.

        Returns:
            The created namespace.

        Raises:
            InvalidNamespaceName: If the name is not usable.
            NamespaceNameTaken: If the name is already claimed.
            NamespaceError: If workgroup provisioning is unconfigured.
        """
        name = validate_namespace_name(name=request.name)
        namespace_id = str(uuid.uuid4())
        timestamp = _now_iso()

        self._claim_name(name=name, namespace_id=namespace_id)

        try:
            workgroup, result_location = self._provision_workgroup(
                namespace_id=namespace_id, name=name
            )
            record = Namespace(
                id=namespace_id,
                name=name,
                description=request.description,
                status=NamespaceStatus.ACTIVE.value,
                workgroup=workgroup,
                resultLocation=result_location,
                createdBy=created_by,
                createdAt=timestamp,
                updatedAt=timestamp,
            )
            self.namespaces.put_item(
                Item={
                    'pk': f'{_NS_PK_PREFIX}{namespace_id}',
                    'sk': _METADATA_SK,
                    **record.model_dump(),
                }
            )
            self.grant_role(
                namespace_id=namespace_id,
                request=GrantCreate(
                    principalType='User',
                    principalId=created_by,
                    role=Role.NAMESPACE_OWNER.value,
                ),
                granted_by=created_by,
            )
            if created_by_email:
                self.grant_role(
                    namespace_id=namespace_id,
                    request=GrantCreate(
                        principalType='User',
                        principalId=created_by_email,
                        role=Role.NAMESPACE_OWNER.value,
                    ),
                    granted_by=created_by,
                )
        except Exception:
            self._release_name(name=name)
            raise

        self._confirm_name(name=name)
        logger.info(
            'namespace_created id=%s name=%s workgroup=%s by=%s',
            namespace_id,
            name,
            record.workgroup,
            created_by,
        )
        return record

    def _claim_name(self, *, name: str, namespace_id: str) -> None:
        """Claim a namespace name with a conditional, TTL'd write.

        Args:
            name: The normalised name.
            namespace_id: The id the name will point at.

        Returns:
            None.

        Raises:
            NamespaceNameTaken: If the name is already claimed.
        """
        try:
            self.namespaces.put_item(
                Item={
                    'pk': f'{_NAME_PK_PREFIX}{name}',
                    'sk': _RESERVATION_SK,
                    'namespaceId': namespace_id,
                    'ttl': int(time.time()) + _RESERVATION_TTL_SECONDS,
                },
                ConditionExpression='attribute_not_exists(pk)',
            )
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') == (
                'ConditionalCheckFailedException'
            ):
                raise NamespaceNameTaken(
                    f'namespace name {name!r} is already in use'
                ) from exc
            raise

    def _confirm_name(self, *, name: str) -> None:
        """Make a name claim permanent by dropping its TTL.

        Args:
            name: The normalised name.

        Returns:
            None.
        """
        self.namespaces.update_item(
            Key={'pk': f'{_NAME_PK_PREFIX}{name}', 'sk': _RESERVATION_SK},
            UpdateExpression='REMOVE #ttl',
            ExpressionAttributeNames={'#ttl': 'ttl'},
        )

    def _release_name(self, *, name: str) -> None:
        """Release a name claim after a failed create.

        A failure here is logged and swallowed deliberately: the caller is
        already unwinding a more important error, and the reservation TTL frees
        the name regardless. Raising would replace the real cause with a
        cleanup error.

        Args:
            name: The normalised name.

        Returns:
            None.
        """
        try:
            self.namespaces.delete_item(
                Key={'pk': f'{_NAME_PK_PREFIX}{name}', 'sk': _RESERVATION_SK}
            )
        except Exception as exc:
            logger.warning(
                'namespace_name_release_failed name=%s error=%s — the %ss '
                'reservation TTL will free it',
                name,
                exc,
                _RESERVATION_TTL_SECONDS,
            )

    def _provision_workgroup(self, *, namespace_id: str, name: str) -> tuple[str, str]:
        """Create (or adopt) the namespace's Athena workgroup.

        ``EnforceWorkGroupConfiguration`` is set so a caller cannot override the
        output location: that is what makes the per-namespace S3 prefix an
        isolation boundary rather than a default.

        Args:
            namespace_id: The namespace id, used in the result prefix.
            name: The namespace name, used in the workgroup name.

        Returns:
            ``(workgroup_name, result_location)``.

        Raises:
            NamespaceError: If ``ATHENA_RESULTS_BUCKET`` is not configured — a
                workgroup with no enforced output location provides no
                isolation, so creating one would be worse than failing.
        """
        if not self.results_bucket:
            raise NamespaceError(
                'ATHENA_RESULTS_BUCKET is not configured; cannot provision an '
                'isolated workgroup for the namespace'
            )
        workgroup = f'{self.resource_prefix}-ns-{name}'
        result_location = (
            f's3://{self.results_bucket}/namespaces/{namespace_id}/'
        )
        client = self.athena
        if client is None:
            client = boto3.client(
                'athena', region_name=os.environ.get('AWS_REGION', 'us-east-1')
            )
            self.athena = client

        # Adopt an existing workgroup rather than failing: create is retried by
        # callers and by the bootstrap custom resource, and a workgroup that
        # already carries the right enforced configuration is the desired state.
        try:
            client.get_work_group(WorkGroup=workgroup)
            logger.info('namespace_workgroup_exists workgroup=%s', workgroup)
            return workgroup, result_location
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code')
            if code not in ('InvalidRequestException', 'ResourceNotFoundException'):
                raise

        client.create_work_group(
            Name=workgroup,
            Description=f'Semantic Layer namespace {name} ({namespace_id})',
            Configuration={
                'ResultConfiguration': {'OutputLocation': result_location},
                # Callers must not be able to redirect results out of the
                # namespace's prefix.
                'EnforceWorkGroupConfiguration': True,
                'PublishCloudWatchMetricsEnabled': True,
            },
            Tags=[
                {'Key': 'SemanticLayerNamespaceId', 'Value': namespace_id},
                {'Key': 'SemanticLayerNamespaceName', 'Value': name},
            ],
        )
        return workgroup, result_location

    # ── Update / lifecycle ────────────────────────────────────────────

    def update(
        self, *, namespace_id: str, request: NamespaceUpdate
    ) -> Namespace:
        """Update a namespace's description.

        Args:
            namespace_id: The namespace id.
            request: The update request.

        Returns:
            The updated namespace.

        Raises:
            NamespaceNotFound: If the namespace does not exist.
        """
        existing = self.get(namespace_id=namespace_id)
        if existing is None:
            raise NamespaceNotFound(f'namespace {namespace_id!r} not found')
        updated = existing.model_copy(
            update={'description': request.description, 'updatedAt': _now_iso()}
        )
        self.namespaces.put_item(
            Item={
                'pk': f'{_NS_PK_PREFIX}{namespace_id}',
                'sk': _METADATA_SK,
                **updated.model_dump(),
            }
        )
        return updated

    def set_status(self, *, namespace_id: str, status: str) -> Namespace:
        """Transition a namespace's lifecycle status.

        Args:
            namespace_id: The namespace id.
            status: The target status.

        Returns:
            The updated namespace.

        Raises:
            NamespaceNotFound: If the namespace does not exist.
            InvalidStatusTransition: If the transition is not permitted. An
                unchecked transition would let a caller move a namespace out of
                DELETING, resuming writes against a namespace mid-teardown.
        """
        existing = self.get(namespace_id=namespace_id)
        if existing is None:
            raise NamespaceNotFound(f'namespace {namespace_id!r} not found')
        allowed = _ALLOWED_TRANSITIONS.get(existing.status, frozenset())
        if status not in allowed:
            raise InvalidStatusTransition(
                f'cannot transition namespace {namespace_id!r} from '
                f'{existing.status} to {status}; allowed: '
                f'{sorted(allowed) or "none"}'
            )
        updated = existing.model_copy(
            update={'status': status, 'updatedAt': _now_iso()}
        )
        self.namespaces.put_item(
            Item={
                'pk': f'{_NS_PK_PREFIX}{namespace_id}',
                'sk': _METADATA_SK,
                **updated.model_dump(),
            }
        )
        logger.info(
            'namespace_status_changed id=%s from=%s to=%s',
            namespace_id,
            existing.status,
            status,
        )
        return updated

    def delete(self, *, namespace_id: str) -> Namespace:
        """Tear a namespace down: revoke its grants and tombstone the record.

        The namespace is marked DELETING before anything is removed, so the
        Cedar non-ACTIVE guard blocks writes for the duration. Grants are revoked
        before the tombstone is written — leaving grants behind would make a
        later namespace with a recycled id inherit them.

        Content (rules, metrics, ontologies) is deliberately NOT deleted: it is
        keyed by namespace and becomes unreachable once the namespace is gone,
        and destroying governed content as a side effect of a namespace delete is
        not a decision this endpoint should make silently.

        Args:
            namespace_id: The namespace id.

        Returns:
            The tombstoned namespace record.

        Raises:
            NamespaceNotFound: If the namespace does not exist.
        """
        existing = self.get(namespace_id=namespace_id)
        if existing is None:
            raise NamespaceNotFound(f'namespace {namespace_id!r} not found')

        self._write_status(record=existing, status=NamespaceStatus.DELETING.value)
        try:
            for grant in self.list_grants(namespace_id=namespace_id):
                self.revoke_role(
                    namespace_id=namespace_id,
                    principal_key=grant.principalKey,
                    role=grant.role,
                )
            self._release_name(name=existing.name)
            tombstone = self._write_status(
                record=existing, status=NamespaceStatus.DELETED.value
            )
        except Exception:
            self._write_status(
                record=existing, status=NamespaceStatus.DELETE_FAILED.value
            )
            raise

        logger.info('namespace_deleted id=%s name=%s', namespace_id, existing.name)
        return tombstone

    def _write_status(self, *, record: Namespace, status: str) -> Namespace:
        """Persist a status change without transition validation.

        Used by the delete path, whose transitions (ACTIVE -> DELETING ->
        DELETED / DELETE_FAILED) are driven by the service rather than by a
        caller.

        Args:
            record: The current namespace record.
            status: The status to write.

        Returns:
            The updated namespace.
        """
        updated = record.model_copy(
            update={'status': status, 'updatedAt': _now_iso()}
        )
        self.namespaces.put_item(
            Item={
                'pk': f'{_NS_PK_PREFIX}{record.id}',
                'sk': _METADATA_SK,
                **updated.model_dump(),
            }
        )
        return updated

    # ── Grants ────────────────────────────────────────────────────────

    def grant_role(
        self,
        *,
        namespace_id: str,
        request: GrantCreate,
        granted_by: str,
    ) -> Grant:
        """Grant a role on a namespace (or platform-wide) to a principal.

        Args:
            namespace_id: The namespace id, or ``GLOBAL`` for a platform role.
            request: The grant request.
            granted_by: JWT ``sub`` of the granter, recorded for audit.

        Returns:
            The stored grant.

        Raises:
            ValueError: If the principal or role is invalid. An unrecognised
                role is rejected rather than stored: the policy loader skips
                roles it does not ship, so a typo would silently grant nothing.
        """
        if request.role not in {role.value for role in Role}:
            raise ValueError(
                f'unknown role {request.role!r}; expected one of '
                f'{sorted(role.value for role in Role)}'
            )
        principal_key = build_principal_key(
            principal_type=request.principalType, principal_id=request.principalId
        )
        item: Dict[str, Any] = {
            'pk': f'{_GRANT_PK_PREFIX}{namespace_id}',
            'sk': f'{principal_key}#{request.role}',
            'principalKey': principal_key,
            'resourceId': namespace_id,
            'role': request.role,
            'grantedBy': granted_by,
            'grantedAt': _now_iso(),
        }
        # Restrictions are written only when declared: an absent key means "no
        # constraint", while an empty list means "deny everything". Writing None
        # would collapse that distinction on read.
        if request.tableAllowlist is not None:
            item['tableAllowlist'] = request.tableAllowlist
        if request.columnDenylist is not None:
            item['columnDenylist'] = request.columnDenylist
        if request.allowedMetrics is not None:
            item['allowedMetrics'] = request.allowedMetrics

        self.grants.put_item(Item=item)
        # The role cache would otherwise serve the pre-grant answer for up to
        # its TTL, making a fresh grant look like it did not take.
        clear_role_cache()
        logger.info(
            'grant_created resource=%s principal=%s role=%s by=%s',
            namespace_id,
            principal_key,
            request.role,
            granted_by,
        )
        return Grant(**{k: v for k, v in item.items() if k not in ('pk', 'sk')})

    def revoke_role(
        self, *, namespace_id: str, principal_key: str, role: str
    ) -> None:
        """Revoke one grant.

        Idempotent: revoking a grant that does not exist is not an error, so a
        retried revoke cannot leave a caller believing access is still in place.

        Args:
            namespace_id: The namespace id, or ``GLOBAL``.
            principal_key: The stored ``<type>::<id>`` key.
            role: The role identifier to revoke.

        Returns:
            None.
        """
        self.grants.delete_item(
            Key={
                'pk': f'{_GRANT_PK_PREFIX}{namespace_id}',
                'sk': f'{principal_key}#{role}',
            }
        )
        clear_role_cache()
        logger.info(
            'grant_revoked resource=%s principal=%s role=%s',
            namespace_id,
            principal_key,
            role,
        )

    def list_grants(self, *, namespace_id: str) -> List[Grant]:
        """List every grant on a namespace, following all pages.

        Args:
            namespace_id: The namespace id, or ``GLOBAL``.

        Returns:
            Every grant on the resource.
        """
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {
            'KeyConditionExpression': Key('pk').eq(
                f'{_GRANT_PK_PREFIX}{namespace_id}'
            )
        }
        while True:
            response = self.grants.query(**kwargs)
            items.extend(response.get('Items', []))
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                break
            kwargs['ExclusiveStartKey'] = last_key
        return [
            Grant(**{k: v for k, v in item.items() if k not in ('pk', 'sk')})
            for item in items
        ]

    def grant_platform_role(
        self, *, principal_type: str, principal_id: str, role: str, granted_by: str
    ) -> Grant:
        """Grant a platform-wide role.

        Args:
            principal_type: ``User`` or ``Group``.
            principal_id: A ``sub``, email address, or group name.
            role: A platform role identifier.
            granted_by: JWT ``sub`` of the granter.

        Returns:
            The stored grant.
        """
        return self.grant_role(
            namespace_id=GLOBAL_RESOURCE_ID,
            request=GrantCreate(
                principalType=principal_type, principalId=principal_id, role=role
            ),
            granted_by=granted_by,
        )

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _to_model(*, item: Dict[str, Any]) -> Namespace:
        """Convert a DynamoDB item into a :class:`Namespace`.

        Args:
            item: The raw DynamoDB item.

        Returns:
            The namespace model, ignoring the key attributes.
        """
        return Namespace(
            **{key: value for key, value in item.items() if key not in ('pk', 'sk')}
        )
