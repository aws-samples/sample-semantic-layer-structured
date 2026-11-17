"""Unit tests for NamespaceService — registry, lifecycle, and grants.

Run against moto rather than a hand-rolled fake table because the properties
under test are DynamoDB properties: the conditional write that makes name claims
race-safe, the GSI the grants resolver reads through, and paginated reads. A fake
that always succeeds would assert nothing about any of them.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api')
)

from agents.shared.grant_profile import (  # noqa: E402
    GLOBAL_RESOURCE_ID,
    resolve_profile,
)
from authz.cedar_schema import NamespaceStatus, Role  # noqa: E402
from services.namespace_service import (  # noqa: E402
    GrantCreate,
    InvalidNamespaceName,
    InvalidStatusTransition,
    NamespaceCreate,
    NamespaceError,
    NamespaceNameTaken,
    NamespaceNotFound,
    NamespaceService,
    NamespaceUpdate,
    build_principal_key,
    validate_namespace_name,
)

NAMESPACES_TABLE = 'semantic-layer-namespaces'
GRANTS_TABLE = 'semantic-layer-grants'
RESULTS_BUCKET = 'semantic-layer-athena-results'
OWNER = 'owner-sub-1'


def _create_tables() -> None:
    """Create the namespaces and grants tables, including the PrincipalIndex GSI.

    The GSI is created here rather than stubbed because
    :func:`agents.shared.grant_profile.resolve_profile` reads through it — a test
    that wrote grants without it would pass while the live read path returned
    nothing.

    Returns:
        None.
    """
    client = boto3.client('dynamodb', region_name='us-east-1')
    client.create_table(
        TableName=NAMESPACES_TABLE,
        AttributeDefinitions=[
            {'AttributeName': 'pk', 'AttributeType': 'S'},
            {'AttributeName': 'sk', 'AttributeType': 'S'},
        ],
        KeySchema=[
            {'AttributeName': 'pk', 'KeyType': 'HASH'},
            {'AttributeName': 'sk', 'KeyType': 'RANGE'},
        ],
        BillingMode='PAY_PER_REQUEST',
    )
    client.create_table(
        TableName=GRANTS_TABLE,
        AttributeDefinitions=[
            {'AttributeName': 'pk', 'AttributeType': 'S'},
            {'AttributeName': 'sk', 'AttributeType': 'S'},
            {'AttributeName': 'principalKey', 'AttributeType': 'S'},
        ],
        KeySchema=[
            {'AttributeName': 'pk', 'KeyType': 'HASH'},
            {'AttributeName': 'sk', 'KeyType': 'RANGE'},
        ],
        GlobalSecondaryIndexes=[
            {
                'IndexName': 'PrincipalIndex',
                'KeySchema': [
                    {'AttributeName': 'principalKey', 'KeyType': 'HASH'},
                    {'AttributeName': 'sk', 'KeyType': 'RANGE'},
                ],
                'Projection': {'ProjectionType': 'ALL'},
            }
        ],
        BillingMode='PAY_PER_REQUEST',
    )


class StubAthena:
    """Athena client stub that records workgroup creation.

    Attributes:
        created: Every ``create_work_group`` call's kwargs.
        existing: Workgroup names to report as already present.
    """

    def __init__(self, *, existing: List[str] | None = None) -> None:
        """Build the stub.

        Args:
            existing: Workgroup names ``get_work_group`` should find.
        """
        self.created: List[Dict[str, Any]] = []
        self.existing = list(existing or [])

    def get_work_group(self, **kwargs: Any) -> Dict[str, Any]:
        """Report whether a workgroup exists.

        Args:
            **kwargs: Must carry ``WorkGroup``.

        Returns:
            A minimal workgroup description.

        Raises:
            ClientError: ``ResourceNotFoundException`` when unknown.
        """
        name = kwargs['WorkGroup']
        if name in self.existing:
            return {'WorkGroup': {'Name': name}}
        raise ClientError(
            {'Error': {'Code': 'ResourceNotFoundException', 'Message': 'nope'}},
            'GetWorkGroup',
        )

    def create_work_group(self, **kwargs: Any) -> Dict[str, Any]:
        """Record a workgroup creation.

        Args:
            **kwargs: The ``create_work_group`` arguments.

        Returns:
            An empty response.
        """
        self.created.append(kwargs)
        self.existing.append(kwargs['Name'])
        return {}


@pytest.fixture
def athena() -> StubAthena:
    """Provide a fresh Athena stub.

    Returns:
        The stub.
    """
    return StubAthena()


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch, athena: StubAthena):
    """Provide a NamespaceService over moto-backed tables.

    Args:
        monkeypatch: pytest's environment patcher.
        athena: The Athena stub.

    Yields:
        The configured service.
    """
    with mock_aws():
        _create_tables()
        monkeypatch.setenv('AWS_REGION', 'us-east-1')
        monkeypatch.setenv('GRANTS_TABLE', GRANTS_TABLE)
        resource = boto3.resource('dynamodb', region_name='us-east-1')
        yield NamespaceService(
            namespaces_table=resource.Table(NAMESPACES_TABLE),
            grants_table=resource.Table(GRANTS_TABLE),
            athena_client=athena,
            results_bucket=RESULTS_BUCKET,
            resource_prefix='semantic-layer',
        )


# ── Name validation ───────────────────────────────────────────────────


@pytest.mark.parametrize('name', ['sales', 'sales-eu', 'sales_eu.v2', 'ns1'])
def test_valid_names_are_accepted(name: str) -> None:
    """Names usable in both an Athena workgroup name and an S3 prefix pass."""
    assert validate_namespace_name(name=name) == name


def test_names_are_lowercased() -> None:
    """One canonical spelling keeps DynamoDB, Athena and S3 in agreement."""
    assert validate_namespace_name(name='  Sales  ') == 'sales'


@pytest.mark.parametrize(
    'name',
    ['', 'a', 'has space', 'has/slash', '-leading', 'UPPER!', 'x' * 64],
)
def test_unsafe_names_are_rejected(name: str) -> None:
    """Rejecting at the door beats escaping at three downstream call sites."""
    with pytest.raises(InvalidNamespaceName):
        validate_namespace_name(name=name)


# ── Principal keys ────────────────────────────────────────────────────


def test_principal_key_matches_the_reader_encoding() -> None:
    """Writer and reader must encode identically or grants are unfindable."""
    assert (
        build_principal_key(principal_type='User', principal_id='A@Example.com')
        == 'User::a@example.com'
    )


def test_principal_type_is_normalised() -> None:
    """Case in the request body must not fork the key space."""
    assert (
        build_principal_key(principal_type='group', principal_id='Admin')
        == 'Group::Admin'
    )


@pytest.mark.parametrize(
    'principal_type,principal_id',
    [('Robot', 'x'), ('User', ''), ('User', '   ')],
)
def test_invalid_principals_are_rejected(
    principal_type: str, principal_id: str
) -> None:
    """A grant stored under a bogus key silently grants nothing."""
    with pytest.raises(ValueError):
        build_principal_key(
            principal_type=principal_type, principal_id=principal_id
        )


# ── Configuration ─────────────────────────────────────────────────────


def test_missing_table_configuration_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running without a registry would reduce namespaces to bare strings again."""
    monkeypatch.delenv('NAMESPACES_TABLE', raising=False)
    with pytest.raises(NamespaceError, match='NAMESPACES_TABLE'):
        NamespaceService()


def test_missing_results_bucket_fails_the_create(service) -> None:
    """A workgroup with no enforced output location provides no isolation."""
    service.results_bucket = ''
    with pytest.raises(NamespaceError, match='ATHENA_RESULTS_BUCKET'):
        service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)


# ── Create ────────────────────────────────────────────────────────────


def test_create_registers_the_namespace(service) -> None:
    """A created namespace is ACTIVE, named, and addressable by id."""
    created = service.create(
        request=NamespaceCreate(name='sales', description='EU sales'),
        created_by=OWNER,
    )
    assert created.name == 'sales'
    assert created.status == NamespaceStatus.ACTIVE.value
    assert created.description == 'EU sales'
    assert service.get(namespace_id=created.id) == created


def test_create_provisions_an_enforced_workgroup(service, athena) -> None:
    """EnforceWorkGroupConfiguration is what makes the S3 prefix a boundary."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    assert len(athena.created) == 1
    call = athena.created[0]
    assert call['Name'] == 'semantic-layer-ns-sales'
    assert call['Configuration']['EnforceWorkGroupConfiguration'] is True
    assert call['Configuration']['ResultConfiguration']['OutputLocation'] == (
        f's3://{RESULTS_BUCKET}/namespaces/{created.id}/'
    )
    assert created.workgroup == 'semantic-layer-ns-sales'


def test_create_adopts_an_existing_workgroup(service, athena) -> None:
    """Create is retried by callers and by the bootstrap; adoption is idempotent."""
    athena.existing.append('semantic-layer-ns-sales')
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    assert athena.created == []
    assert created.workgroup == 'semantic-layer-ns-sales'


def test_create_grants_the_creator_ownership(service) -> None:
    """Without this the creator could not administer what they just made."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    grants = service.list_grants(namespace_id=created.id)
    assert [g.role for g in grants] == [Role.NAMESPACE_OWNER.value]
    assert grants[0].principalKey == f'User::{OWNER}'


def test_create_also_grants_by_email(service) -> None:
    """Tokens surface either identifier, so the owner grant must cover both."""
    created = service.create(
        request=NamespaceCreate(name='sales'),
        created_by=OWNER,
        created_by_email='owner@example.com',
    )
    keys = {g.principalKey for g in service.list_grants(namespace_id=created.id)}
    assert keys == {f'User::{OWNER}', 'User::owner@example.com'}


def test_owner_grant_is_visible_through_the_live_resolver(service) -> None:
    """End-to-end: what create() writes is what the PDP's resolver reads.

    Exercises the real ``PrincipalIndex`` query, so a mismatch between the write
    shape and the GSI key schema fails here rather than as an inexplicable denial.
    """
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    profile = resolve_profile(user_id=OWNER, table=service.grants)
    assert profile.resource_roles == [
        {'role': Role.NAMESPACE_OWNER.value, 'resourceUID': created.id}
    ]
    assert profile.global_roles == []


def test_duplicate_name_is_rejected(service) -> None:
    """The conditional write is what makes two concurrent creates safe."""
    service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    with pytest.raises(NamespaceNameTaken):
        service.create(request=NamespaceCreate(name='Sales'), created_by=OWNER)


def test_failed_create_releases_the_name(service, monkeypatch) -> None:
    """An abandoned create must not burn the name permanently."""

    def explode(**kwargs: Any) -> None:
        """Fail workgroup provisioning.

        Args:
            **kwargs: Ignored.

        Returns:
            Never returns.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError('Athena unavailable')

    monkeypatch.setattr(service, '_provision_workgroup', explode)
    with pytest.raises(RuntimeError):
        service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    monkeypatch.undo()
    # The name is immediately reusable, without waiting for the reservation TTL.
    assert service.create(
        request=NamespaceCreate(name='sales'), created_by=OWNER
    ).name == 'sales'


def test_successful_create_drops_the_reservation_ttl(service) -> None:
    """A confirmed name must not expire out from under its namespace."""
    service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    item = service.namespaces.get_item(
        Key={'pk': 'NS_NAME#sales', 'sk': 'RESERVATION'}
    )['Item']
    assert 'ttl' not in item


# ── Reads ─────────────────────────────────────────────────────────────


def test_resolve_accepts_id_or_name(service) -> None:
    """Legacy routes pass names; grants are keyed by id. Both must resolve."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    assert service.resolve(identifier=created.id).id == created.id
    assert service.resolve(identifier='sales').id == created.id
    assert service.resolve(identifier='SALES').id == created.id


def test_resolve_returns_none_for_unknown(service) -> None:
    """An unknown identifier is None, which the middleware turns into a 403."""
    assert service.resolve(identifier='nope') is None
    assert service.resolve(identifier='') is None


def test_status_of_reports_the_lifecycle_state(service) -> None:
    """This is the resolver the PDP calls on every namespace-scoped request."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    assert service.status_of(identifier='sales') == NamespaceStatus.ACTIVE.value
    service.set_status(
        namespace_id=created.id, status=NamespaceStatus.ARCHIVED.value
    )
    assert service.status_of(identifier='sales') == NamespaceStatus.ARCHIVED.value
    assert service.status_of(identifier='missing') is None


def test_list_excludes_deleted_and_sorts_by_name(service) -> None:
    """A tombstone must not appear as a live namespace."""
    service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    service.create(request=NamespaceCreate(name='finance'), created_by=OWNER)
    gone = service.create(request=NamespaceCreate(name='legacy'), created_by=OWNER)
    service.delete(namespace_id=gone.id)
    assert [ns.name for ns in service.list()] == ['finance', 'sales']


def test_list_ignores_reservation_rows(service) -> None:
    """Name-claim rows share the table and must not be listed as namespaces."""
    service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    assert len(service.list()) == 1


# ── Update / lifecycle ────────────────────────────────────────────────


def test_update_changes_only_the_description(service) -> None:
    """The name is embedded in a workgroup name that cannot be renamed in place."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    updated = service.update(
        namespace_id=created.id, request=NamespaceUpdate(description='new')
    )
    assert updated.description == 'new'
    assert updated.name == 'sales'
    assert updated.workgroup == created.workgroup


def test_update_of_a_missing_namespace_raises(service) -> None:
    """Silently creating one on update would bypass name reservation entirely."""
    with pytest.raises(NamespaceNotFound):
        service.update(namespace_id='nope', request=NamespaceUpdate())


def test_archive_and_reactivate(service) -> None:
    """Archiving freezes writes via Cedar; reactivating must remain possible."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    archived = service.set_status(
        namespace_id=created.id, status=NamespaceStatus.ARCHIVED.value
    )
    assert archived.status == NamespaceStatus.ARCHIVED.value
    revived = service.set_status(
        namespace_id=created.id, status=NamespaceStatus.ACTIVE.value
    )
    assert revived.status == NamespaceStatus.ACTIVE.value


@pytest.mark.parametrize(
    'target',
    [
        NamespaceStatus.ACTIVE.value,
        NamespaceStatus.DELETING.value,
        NamespaceStatus.DELETED.value,
    ],
)
def test_invalid_transitions_are_rejected(service, target: str) -> None:
    """A caller must not be able to move a namespace out of a teardown state.

    Resuming writes against a namespace mid-teardown is exactly what the
    lifecycle exists to prevent.
    """
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    with pytest.raises(InvalidStatusTransition):
        service.set_status(namespace_id=created.id, status=target)


def test_set_status_of_a_missing_namespace_raises(service) -> None:
    """Lifecycle changes need a namespace to change."""
    with pytest.raises(NamespaceNotFound):
        service.set_status(
            namespace_id='nope', status=NamespaceStatus.ARCHIVED.value
        )


# ── Delete ────────────────────────────────────────────────────────────


def test_delete_tombstones_and_revokes(service) -> None:
    """Leftover grants would be inherited by anything reusing the id."""
    created = service.create(
        request=NamespaceCreate(name='sales'),
        created_by=OWNER,
        created_by_email='owner@example.com',
    )
    tombstone = service.delete(namespace_id=created.id)
    assert tombstone.status == NamespaceStatus.DELETED.value
    assert service.list_grants(namespace_id=created.id) == []
    assert resolve_profile(user_id=OWNER, table=service.grants).resource_roles == []


def test_delete_frees_the_name(service) -> None:
    """A deleted namespace's name returns to the pool."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    service.delete(namespace_id=created.id)
    assert service.get_by_name(name='sales') is None
    reused = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    assert reused.id != created.id


def test_delete_of_a_missing_namespace_raises(service) -> None:
    """Deleting nothing is an error, not a silent success."""
    with pytest.raises(NamespaceNotFound):
        service.delete(namespace_id='nope')


def test_failed_delete_lands_in_delete_failed(service, monkeypatch) -> None:
    """A half-finished teardown must be visible, not reported as complete."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)

    def explode(**kwargs: Any) -> None:
        """Fail grant revocation.

        Args:
            **kwargs: Ignored.

        Returns:
            Never returns.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError('DynamoDB unavailable')

    monkeypatch.setattr(service, 'revoke_role', explode)
    with pytest.raises(RuntimeError):
        service.delete(namespace_id=created.id)
    monkeypatch.undo()
    assert (
        service.get(namespace_id=created.id).status
        == NamespaceStatus.DELETE_FAILED.value
    )


def test_delete_failed_can_be_archived(service, monkeypatch) -> None:
    """An operator must be able to park a namespace whose teardown failed."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    service._write_status(
        record=service.get(namespace_id=created.id),
        status=NamespaceStatus.DELETE_FAILED.value,
    )
    assert (
        service.set_status(
            namespace_id=created.id, status=NamespaceStatus.ARCHIVED.value
        ).status
        == NamespaceStatus.ARCHIVED.value
    )


# ── Grants ────────────────────────────────────────────────────────────


def test_grant_and_revoke_round_trip(service) -> None:
    """The stored grant is the one the resolver later finds."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    grant = service.grant_role(
        namespace_id=created.id,
        request=GrantCreate(
            principalType='User',
            principalId='analyst@example.com',
            role=Role.NAMESPACE_DATA_ANALYST.value,
        ),
        granted_by=OWNER,
    )
    assert grant.principalKey == 'User::analyst@example.com'
    assert grant.grantedBy == OWNER
    profile = resolve_profile(
        user_id='analyst-sub', email='analyst@example.com', table=service.grants
    )
    assert profile.resource_roles == [
        {'role': Role.NAMESPACE_DATA_ANALYST.value, 'resourceUID': created.id}
    ]
    service.revoke_role(
        namespace_id=created.id,
        principal_key='User::analyst@example.com',
        role=Role.NAMESPACE_DATA_ANALYST.value,
    )
    assert (
        resolve_profile(
            user_id='analyst-sub',
            email='analyst@example.com',
            table=service.grants,
        ).resource_roles
        == []
    )


def test_group_grant_resolves_for_a_member(service) -> None:
    """Granting a Cognito group is how a whole team gets a namespace role."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    service.grant_role(
        namespace_id=created.id,
        request=GrantCreate(
            principalType='Group',
            principalId='SalesAnalysts',
            role=Role.NAMESPACE_DATA_STEWARD.value,
        ),
        granted_by=OWNER,
    )
    profile = resolve_profile(
        user_id='member-1', groups=['SalesAnalysts'], table=service.grants
    )
    assert profile.resource_roles == [
        {'role': Role.NAMESPACE_DATA_STEWARD.value, 'resourceUID': created.id}
    ]


def test_unknown_role_is_rejected(service) -> None:
    """The policy loader skips roles it does not ship, so a typo grants nothing."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    with pytest.raises(ValueError, match='unknown role'):
        service.grant_role(
            namespace_id=created.id,
            request=GrantCreate(principalId='u1', role='namespace-god'),
            granted_by=OWNER,
        )


def test_platform_grant_lands_in_global_roles(service) -> None:
    """A GLOBAL resourceId is what separates a platform role from a scoped one."""
    service.grant_role(
        namespace_id=GLOBAL_RESOURCE_ID,
        request=GrantCreate(
            principalId='admin@example.com', role=Role.PLATFORM_ADMIN.value
        ),
        granted_by=OWNER,
    )
    profile = resolve_profile(
        user_id='admin-sub', email='admin@example.com', table=service.grants
    )
    assert profile.global_roles == [Role.PLATFORM_ADMIN.value]
    assert profile.resource_roles == []


def test_grant_platform_role_helper(service) -> None:
    """The bootstrap uses this helper to seed the first platform-admin."""
    grant = service.grant_platform_role(
        principal_type='Group',
        principal_id='Admin',
        role=Role.PLATFORM_ADMIN.value,
        granted_by='bootstrap',
    )
    assert grant.resourceId == GLOBAL_RESOURCE_ID
    assert grant.principalKey == 'Group::Admin'


def test_declared_restrictions_are_stored_and_absent_ones_are_not(service) -> None:
    """Absent means "no constraint"; empty means "deny everything"."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    service.grant_role(
        namespace_id=created.id,
        request=GrantCreate(
            principalId='analyst-1',
            role=Role.NAMESPACE_DATA_ANALYST.value,
            tableAllowlist=[],
            columnDenylist={'parties': ['ssn']},
        ),
        granted_by=OWNER,
    )
    stored = service.grants.get_item(
        Key={
            'pk': f'RES#{created.id}',
            'sk': f'User::analyst-1#{Role.NAMESPACE_DATA_ANALYST.value}',
        }
    )['Item']
    assert stored['tableAllowlist'] == []
    assert stored['columnDenylist'] == {'parties': ['ssn']}
    assert 'allowedMetrics' not in stored


def test_revoke_is_idempotent(service) -> None:
    """A retried revoke must not raise, or a caller may believe access remains."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    service.revoke_role(
        namespace_id=created.id, principal_key='User::nobody', role='namespace-owner'
    )


def test_grant_write_clears_the_role_cache(service, monkeypatch) -> None:
    """Otherwise a fresh grant looks like it did not take, for a whole TTL."""
    cleared: List[bool] = []
    monkeypatch.setattr(
        'services.namespace_service.clear_role_cache',
        lambda: cleared.append(True),
    )
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    assert cleared, 'create() grants ownership and must invalidate the cache'
    cleared.clear()
    service.revoke_role(
        namespace_id=created.id,
        principal_key=f'User::{OWNER}',
        role=Role.NAMESPACE_OWNER.value,
    )
    assert cleared == [True]


def test_list_grants_covers_every_page(service, monkeypatch) -> None:
    """A one-page read would silently drop grants past the 1 MB boundary."""
    created = service.create(request=NamespaceCreate(name='sales'), created_by=OWNER)
    for index in range(5):
        service.grant_role(
            namespace_id=created.id,
            request=GrantCreate(
                principalId=f'analyst-{index}',
                role=Role.NAMESPACE_DATA_ANALYST.value,
            ),
            granted_by=OWNER,
        )
    real_query = service.grants.query
    pages: List[int] = []

    def paged_query(**kwargs: Any) -> Dict[str, Any]:
        """Force one item per page to exercise the pagination loop.

        Args:
            **kwargs: The query arguments.

        Returns:
            A single-item page.
        """
        pages.append(1)
        return real_query(**{**kwargs, 'Limit': 1})

    monkeypatch.setattr(service.grants, 'query', paged_query)
    grants = service.list_grants(namespace_id=created.id)
    assert len(grants) == 6  # 5 analysts + the owner grant
    assert len(pages) > 1, 'the test did not actually paginate'
