"""Unit tests for the authorization bootstrap custom-resource handler.

The handler mirrors item shapes and the principal-key encoder from
``lambda/rest-api`` rather than importing them: it is a plain zip Lambda with no
access to that tree or to pydantic. That mirroring is the risk this module exists
to contain, in two layers:

1. **Drift guards** compare the handler's constants and its encoder against the
   canonical implementations. A writer that disagrees with the reader produces
   rows the resolver can never find — which presents as "the grant had no
   effect", with no error anywhere.
2. **Behavioural tests** run the handler against moto, and assert the seeded
   grant is visible through the *real* read path
   (:func:`agents.shared.grant_profile.resolve_profile`) rather than by
   re-reading the row the handler just wrote. Re-reading your own write would
   pass even if both sides were wrong in the same way.

Athena is stubbed rather than mocked: ``_ensure_default_namespace`` takes the
client as a parameter precisely so the create-or-adopt branch can be observed,
and moto's workgroup fidelity is not what is under test here.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from typing import Any, Dict, List

import boto3
import pytest
from moto import mock_aws

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api')
)

from agents.shared.grant_profile import (  # noqa: E402
    GLOBAL_RESOURCE_ID,
    sanitize_principal_key,
)
from authz.cedar_schema import NamespaceStatus, Role  # noqa: E402
from agents.shared.grant_profile import resolve_profile  # noqa: E402
from services import namespace_service as canonical  # noqa: E402

NAMESPACES_TABLE = 'semantic-layer-namespaces'
GRANTS_TABLE = 'semantic-layer-grants'
METADATA_TABLE = 'semantic-layer-metadata'
RESULTS_BUCKET = 'semantic-layer-athena-results'
PREFIX = 'semantic-layer'


def _load_handler() -> Any:
    """Import the bootstrap handler from its CDK asset directory.

    The directory name contains a dash, so it is not importable as a package and
    has to be loaded by path.

    Returns:
        The loaded ``index`` module.
    """
    path = os.path.join(
        os.path.dirname(__file__),
        '..',
        '..',
        'cdk',
        'lib',
        'stacks',
        'backend',
        'authz-bootstrap',
        'index.py',
    )
    spec = importlib.util.spec_from_file_location('authz_bootstrap_handler', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = _load_handler()


class StubAthena:
    """Athena client stub recording workgroup creation.

    Attributes:
        created: Every ``create_work_group`` call's kwargs.
        existing: Workgroup names ``get_work_group`` should report as present.
    """

    def __init__(self, *, existing: List[str] | None = None) -> None:
        """Build the stub.

        Args:
            existing: Workgroup names to report as already present.
        """
        self.created: List[Dict[str, Any]] = []
        self.existing = list(existing or [])

    def get_work_group(self, **kwargs: Any) -> Dict[str, Any]:
        """Report whether a workgroup exists.

        Args:
            **kwargs: Must carry ``WorkGroup``.

        Returns:
            A minimal ``get_work_group`` response.

        Raises:
            ClientError: With ``InvalidRequestException`` when absent, matching
                what Athena returns for an unknown workgroup.
        """
        from botocore.exceptions import ClientError

        name = kwargs['WorkGroup']
        if name in self.existing:
            return {'WorkGroup': {'Name': name}}
        raise ClientError(
            {'Error': {'Code': 'InvalidRequestException', 'Message': 'not found'}},
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


def _create_tables() -> None:
    """Create the namespaces, grants and ontology-metadata tables.

    The grants table carries the PrincipalIndex GSI because the assertions read
    grants back through ``resolve_profile``, which queries that index.

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
    client.create_table(
        TableName=METADATA_TABLE,
        AttributeDefinitions=[
            {'AttributeName': 'id', 'AttributeType': 'S'},
            {'AttributeName': 'version', 'AttributeType': 'S'},
        ],
        KeySchema=[
            {'AttributeName': 'id', 'KeyType': 'HASH'},
            {'AttributeName': 'version', 'KeyType': 'RANGE'},
        ],
        BillingMode='PAY_PER_REQUEST',
    )


@pytest.fixture
def tables():
    """Provide the three DynamoDB tables under moto.

    Yields:
        A dict of ``Table`` resources keyed ``namespaces`` / ``grants`` /
        ``metadata``.
    """
    with mock_aws():
        _create_tables()
        resource = boto3.resource('dynamodb', region_name='us-east-1')
        yield {
            'namespaces': resource.Table(NAMESPACES_TABLE),
            'grants': resource.Table(GRANTS_TABLE),
            'metadata': resource.Table(METADATA_TABLE),
        }


def _seed_default(*, tables: Dict[str, Any], athena: StubAthena) -> Dict[str, str]:
    """Run the default-namespace seeding step with the standard arguments.

    Args:
        tables: The fixture's table map.
        athena: The Athena stub to provision through.

    Returns:
        The handler's provisioning result.
    """
    return bootstrap._ensure_default_namespace(
        namespaces=tables['namespaces'],
        athena=athena,
        namespace_id='default',
        name='default',
        description='Default namespace',
        resource_prefix=PREFIX,
        results_bucket=RESULTS_BUCKET,
    )


# ── Drift guards ──────────────────────────────────────────────────────


def test_mirrored_item_shape_constants_match_the_service() -> None:
    """A key-prefix that drifts writes rows the reader will never look at."""
    assert bootstrap._METADATA_SK == canonical._METADATA_SK
    assert bootstrap._RESERVATION_SK == canonical._RESERVATION_SK
    assert bootstrap._NS_PK_PREFIX == canonical._NS_PK_PREFIX
    assert bootstrap._NAME_PK_PREFIX == canonical._NAME_PK_PREFIX
    assert bootstrap._GRANT_PK_PREFIX == canonical._GRANT_PK_PREFIX


def test_mirrored_authz_literals_match_the_enums() -> None:
    """An unrecognised role name is skipped by the policy loader, granting nothing."""
    assert bootstrap._GLOBAL_RESOURCE_ID == GLOBAL_RESOURCE_ID
    assert bootstrap._PLATFORM_ADMIN_ROLE == Role.PLATFORM_ADMIN.value
    assert bootstrap._STATUS_ACTIVE == NamespaceStatus.ACTIVE.value


@pytest.mark.parametrize(
    'value',
    [
        'plain-sub-uuid',
        'Mixed@Example.COM',
        '  spaced@example.com  ',
        'group/with/slashes',
        'has+plus',
        'has#hash',
        'MixedCaseGroup',
    ],
)
def test_mirrored_encoder_agrees_with_the_canonical_one(value: str) -> None:
    """Email case-folding and the encoded character set must match exactly."""
    assert bootstrap._sanitize_principal_key(value=value) == sanitize_principal_key(
        value=value
    )


@pytest.mark.parametrize('principal_type', ['User', 'user', 'Group', 'group'])
def test_mirrored_principal_key_builder_agrees(principal_type: str) -> None:
    """The full ``<type>::<id>`` key, not just the encoded component."""
    assert bootstrap._build_principal_key(
        principal_type=principal_type, principal_id='Someone@Example.com'
    ) == canonical.build_principal_key(
        principal_type=principal_type, principal_id='Someone@Example.com'
    )


@pytest.mark.parametrize(
    'principal_type,principal_id',
    [('Robot', 'x'), ('User', '   ')],
)
def test_a_bad_principal_fails_the_deploy(principal_type: str, principal_id: str) -> None:
    """These come from stack configuration, so a typo must not be written silently."""
    with pytest.raises(ValueError):
        bootstrap._build_principal_key(
            principal_type=principal_type, principal_id=principal_id
        )


# ── Default namespace ─────────────────────────────────────────────────


def test_seeding_registers_the_namespace_and_its_workgroup(tables) -> None:
    """The row the REST API's DEFAULT_NAMESPACE_ID points at has to exist."""
    athena = StubAthena()
    result = _seed_default(tables=tables, athena=athena)

    assert result['adopted'] == 'false'
    assert result['workgroup'] == f'{PREFIX}-ns-default'
    assert result['resultLocation'] == f's3://{RESULTS_BUCKET}/namespaces/default/'

    item = tables['namespaces'].get_item(
        Key={'pk': 'NS#default', 'sk': 'METADATA'}
    )['Item']
    assert item['id'] == 'default'
    assert item['name'] == 'default'
    assert item['status'] == NamespaceStatus.ACTIVE.value
    assert item['workgroup'] == f'{PREFIX}-ns-default'

    # The service reads the namespace through its own model, so seeding must
    # produce a row that model accepts — a missing attribute would only surface
    # on the first API read.
    service = canonical.NamespaceService(
        namespaces_table=tables['namespaces'],
        grants_table=tables['grants'],
        athena_client=athena,
        results_bucket=RESULTS_BUCKET,
        resource_prefix=PREFIX,
    )
    loaded = service.get(namespace_id='default')
    assert loaded is not None
    assert loaded.name == 'default'
    assert loaded.workgroup == f'{PREFIX}-ns-default'


def test_the_workgroup_enforces_its_output_location(tables) -> None:
    """Without enforcement the per-namespace prefix is a default, not a boundary."""
    athena = StubAthena()
    _seed_default(tables=tables, athena=athena)

    assert len(athena.created) == 1
    config = athena.created[0]['Configuration']
    assert config['EnforceWorkGroupConfiguration'] is True
    assert config['ResultConfiguration']['OutputLocation'] == (
        f's3://{RESULTS_BUCKET}/namespaces/default/'
    )


def test_the_name_reservation_is_permanent(tables) -> None:
    """A reservation left carrying a TTL would expire and free the name `default`."""
    _seed_default(tables=tables, athena=StubAthena())

    reservation = tables['namespaces'].get_item(
        Key={'pk': 'NS_NAME#default', 'sk': 'RESERVATION'}
    )['Item']
    assert reservation['namespaceId'] == 'default'
    assert 'ttl' not in reservation


def test_the_name_is_claimed_against_the_services_own_create_path(tables) -> None:
    """A user must not be able to create a second namespace named `default`."""
    _seed_default(tables=tables, athena=StubAthena())
    service = canonical.NamespaceService(
        namespaces_table=tables['namespaces'],
        grants_table=tables['grants'],
        athena_client=StubAthena(),
        results_bucket=RESULTS_BUCKET,
        resource_prefix=PREFIX,
    )
    with pytest.raises(canonical.NamespaceNameTaken):
        service.create(
            request=canonical.NamespaceCreate(name='default'), created_by='someone'
        )


def test_reseeding_adopts_without_reprovisioning(tables) -> None:
    """Every stack update re-invokes the handler; it must be a no-op by then."""
    athena = StubAthena()
    _seed_default(tables=tables, athena=athena)
    result = _seed_default(tables=tables, athena=athena)

    assert result['adopted'] == 'true'
    assert result['workgroup'] == f'{PREFIX}-ns-default'
    assert len(athena.created) == 1


def test_reseeding_does_not_revert_an_operators_edits(tables) -> None:
    """An adopted row may carry an edited description or an ARCHIVED status."""
    _seed_default(tables=tables, athena=StubAthena())
    tables['namespaces'].update_item(
        Key={'pk': 'NS#default', 'sk': 'METADATA'},
        UpdateExpression='SET description = :d, #s = :s',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':d': 'edited by an operator',
            ':s': NamespaceStatus.ARCHIVED.value,
        },
    )

    _seed_default(tables=tables, athena=StubAthena(existing=[f'{PREFIX}-ns-default']))

    item = tables['namespaces'].get_item(
        Key={'pk': 'NS#default', 'sk': 'METADATA'}
    )['Item']
    assert item['description'] == 'edited by an operator'
    assert item['status'] == NamespaceStatus.ARCHIVED.value


def test_an_abandoned_reservation_is_confirmed_rather_than_stranding_the_seed(
    tables,
) -> None:
    """A create that died after claiming `default` must not block the bootstrap."""
    tables['namespaces'].put_item(
        Item={
            'pk': 'NS_NAME#default',
            'sk': 'RESERVATION',
            'namespaceId': 'default',
            'ttl': int(time.time()) + 300,
        }
    )

    _seed_default(tables=tables, athena=StubAthena())

    reservation = tables['namespaces'].get_item(
        Key={'pk': 'NS_NAME#default', 'sk': 'RESERVATION'}
    )['Item']
    assert 'ttl' not in reservation
    assert tables['namespaces'].get_item(
        Key={'pk': 'NS#default', 'sk': 'METADATA'}
    ).get('Item') is not None


def test_a_name_held_by_another_namespace_is_not_stolen(tables) -> None:
    """Re-pointing the name would detach that namespace's live grants from it."""
    tables['namespaces'].put_item(
        Item={
            'pk': 'NS_NAME#default',
            'sk': 'RESERVATION',
            'namespaceId': 'some-other-uuid',
        }
    )

    with pytest.raises(RuntimeError, match='already reserved'):
        _seed_default(tables=tables, athena=StubAthena())


def test_seeding_without_a_results_bucket_fails_loudly(tables) -> None:
    """A workgroup with no enforced location gives the namespace no isolation."""
    with pytest.raises(RuntimeError, match='AthenaResultsBucket'):
        bootstrap._ensure_default_namespace(
            namespaces=tables['namespaces'],
            athena=StubAthena(),
            namespace_id='default',
            name='default',
            description='',
            resource_prefix=PREFIX,
            results_bucket='',
        )


# ── Platform-admin grants ─────────────────────────────────────────────


def test_a_seeded_admin_resolves_through_the_real_read_path(tables) -> None:
    """Asserted via resolve_profile, not by re-reading the row we just wrote."""
    written = bootstrap._seed_platform_admins(
        grants=tables['grants'], principals=['Operator@Example.com']
    )
    assert written == 1

    profile = resolve_profile(
        user_id='some-sub', email='operator@example.com', table=tables['grants']
    )
    assert profile.global_roles == [Role.PLATFORM_ADMIN.value]


def test_a_seeded_admin_carries_no_restrictions(tables) -> None:
    """An empty restriction list would deny everything, not nothing."""
    bootstrap._seed_platform_admins(grants=tables['grants'], principals=['admin-sub'])

    item = tables['grants'].get_item(
        Key={
            'pk': f'RES#{GLOBAL_RESOURCE_ID}',
            'sk': f'User::admin-sub#{Role.PLATFORM_ADMIN.value}',
        }
    )['Item']
    assert 'tableAllowlist' not in item
    assert 'columnDenylist' not in item
    assert 'allowedMetrics' not in item


def test_reseeding_an_admin_leaves_the_existing_grant_alone(tables) -> None:
    """The conditional write keeps grantedAt as the original grant's timestamp."""
    bootstrap._seed_platform_admins(grants=tables['grants'], principals=['admin-sub'])
    key = {
        'pk': f'RES#{GLOBAL_RESOURCE_ID}',
        'sk': f'User::admin-sub#{Role.PLATFORM_ADMIN.value}',
    }
    first = tables['grants'].get_item(Key=key)['Item']['grantedAt']

    assert (
        bootstrap._seed_platform_admins(
            grants=tables['grants'], principals=['admin-sub']
        )
        == 0
    )
    assert tables['grants'].get_item(Key=key)['Item']['grantedAt'] == first


def test_seeding_no_principals_writes_nothing(tables) -> None:
    """The default configuration relies on group mapping, not seeded grants."""
    assert bootstrap._seed_platform_admins(grants=tables['grants'], principals=[]) == 0
    assert tables['grants'].scan()['Count'] == 0


# ── namespaceId backfill ──────────────────────────────────────────────


def test_backfill_binds_pre_namespace_ontologies(tables) -> None:
    """Without this, enforcement would 403 every ontology created before today."""
    tables['metadata'].put_item(Item={'id': 'onto-a', 'version': 'v1'})
    tables['metadata'].put_item(Item={'id': 'onto-b', 'version': 'v1'})

    assert (
        bootstrap._backfill_namespace_id(
            metadata=tables['metadata'], namespace_id='default'
        )
        == 2
    )
    for ontology_id in ('onto-a', 'onto-b'):
        item = tables['metadata'].get_item(
            Key={'id': ontology_id, 'version': 'v1'}
        )['Item']
        assert item['namespaceId'] == 'default'


def test_backfill_leaves_an_assigned_namespace_alone(tables) -> None:
    """Re-homing an ontology someone deliberately placed would be a silent move."""
    tables['metadata'].put_item(
        Item={'id': 'onto-a', 'version': 'v1', 'namespaceId': 'sales'}
    )

    assert (
        bootstrap._backfill_namespace_id(
            metadata=tables['metadata'], namespace_id='default'
        )
        == 0
    )
    assert (
        tables['metadata'].get_item(Key={'id': 'onto-a', 'version': 'v1'})['Item'][
            'namespaceId'
        ]
        == 'sales'
    )


def test_backfill_is_idempotent(tables) -> None:
    """A stack update re-runs it; the second pass must find nothing to do."""
    tables['metadata'].put_item(Item={'id': 'onto-a', 'version': 'v1'})
    bootstrap._backfill_namespace_id(metadata=tables['metadata'], namespace_id='default')
    assert (
        bootstrap._backfill_namespace_id(
            metadata=tables['metadata'], namespace_id='default'
        )
        == 0
    )


class PagedMetadataTable:
    """Metadata-table double that returns two Scan pages.

    moto will not produce a 1MB-boundary page for a handful of items, so the
    pagination loop is exercised with a double instead. An un-paginated scan
    would silently leave later ontologies unbound.

    Attributes:
        pages: The Scan responses to return in order.
        scans: Every ``scan`` call's kwargs.
        updated: Keys passed to ``update_item``.
    """

    def __init__(self) -> None:
        """Build the double with two pages of one item each."""
        self.pages = [
            {'Items': [{'id': 'onto-a', 'version': 'v1'}], 'LastEvaluatedKey': {'id': 'onto-a'}},
            {'Items': [{'id': 'onto-b', 'version': 'v1'}]},
        ]
        self.scans: List[Dict[str, Any]] = []
        self.updated: List[Dict[str, str]] = []

    def scan(self, **kwargs: Any) -> Dict[str, Any]:
        """Return the next canned page.

        Args:
            **kwargs: The Scan arguments, recorded for assertion.

        Returns:
            The next page.
        """
        self.scans.append(kwargs)
        return self.pages[len(self.scans) - 1]

    def update_item(self, **kwargs: Any) -> Dict[str, Any]:
        """Record an update.

        Args:
            **kwargs: The UpdateItem arguments.

        Returns:
            An empty response.
        """
        self.updated.append(kwargs['Key'])
        return {}


def test_backfill_follows_every_scan_page() -> None:
    """A single Scan call would leave everything past the first page unbound."""
    table = PagedMetadataTable()

    assert bootstrap._backfill_namespace_id(metadata=table, namespace_id='default') == 2
    assert table.updated == [
        {'id': 'onto-a', 'version': 'v1'},
        {'id': 'onto-b', 'version': 'v1'},
    ]
    assert table.scans[1]['ExclusiveStartKey'] == {'id': 'onto-a'}


def test_backfill_reads_reserved_key_names_through_expression_names() -> None:
    """`version` is a DynamoDB reserved word; projecting it bare is a ValidationException."""
    table = PagedMetadataTable()
    bootstrap._backfill_namespace_id(metadata=table, namespace_id='default')

    names = table.scans[0]['ExpressionAttributeNames']
    assert set(names.values()) == {'id', 'version'}
    for placeholder in names:
        assert placeholder in table.scans[0]['ProjectionExpression']


# ── Property parsing and the lifecycle event ──────────────────────────


@pytest.mark.parametrize(
    'raw,expected',
    [
        (['a', 'b'], ['a', 'b']),
        ([' a ', '', 'b'], ['a', 'b']),
        ('', []),
        ('solo', ['solo']),
        (None, []),
    ],
)
def test_string_list_tolerates_cloudformations_shapes(raw: Any, expected: List[str]) -> None:
    """CFN delivers an empty list property as '' or omits it entirely."""
    properties: Dict[str, Any] = {} if raw is None else {'PlatformAdminUsers': raw}
    assert bootstrap._string_list(properties=properties, key='PlatformAdminUsers') == expected


def test_string_list_refuses_an_unexpected_shape() -> None:
    """Guessing at the shape would seed the wrong principals."""
    with pytest.raises(TypeError):
        bootstrap._string_list(properties={'PlatformAdminUsers': {'a': 1}}, key='PlatformAdminUsers')


def _event(*, request_type: str, **overrides: Any) -> Dict[str, Any]:
    """Build a custom-resource event with the standard properties.

    Args:
        request_type: ``Create``, ``Update`` or ``Delete``.
        **overrides: Property overrides; a value of ``None`` removes the property.

    Returns:
        The event dict.
    """
    properties: Dict[str, Any] = {
        'NamespacesTable': NAMESPACES_TABLE,
        'GrantsTable': GRANTS_TABLE,
        'MetadataTable': METADATA_TABLE,
        'DefaultNamespaceId': 'default',
        'DefaultNamespaceName': 'default',
        'DefaultNamespaceDescription': 'Default namespace',
        'ResourcePrefix': PREFIX,
        'AthenaResultsBucket': RESULTS_BUCKET,
        'PlatformAdminUsers': ['admin@example.com'],
    }
    for key, value in overrides.items():
        if value is None:
            properties.pop(key, None)
        else:
            properties[key] = value
    return {'RequestType': request_type, 'ResourceProperties': properties}


@pytest.fixture
def stubbed_athena(monkeypatch):
    """Replace the handler's Athena client with a stub.

    Yields:
        The :class:`StubAthena` the handler will use.
    """
    stub = StubAthena()
    real_client = boto3.client

    def _client(service_name: str, **kwargs: Any) -> Any:
        """Return the stub for Athena and the real client otherwise.

        Args:
            service_name: The AWS service name.
            **kwargs: Client arguments.

        Returns:
            The stub or a real boto3 client.
        """
        if service_name == 'athena':
            return stub
        return real_client(service_name, **kwargs)

    monkeypatch.setattr(bootstrap.boto3, 'client', _client)
    monkeypatch.setenv('AWS_REGION', 'us-east-1')
    yield stub


@pytest.mark.parametrize('request_type', ['Create', 'Update'])
def test_create_and_update_run_the_same_seeding_path(
    tables, stubbed_athena, request_type: str
) -> None:
    """A stack update must repair missing state, not skip it."""
    tables['metadata'].put_item(Item={'id': 'onto-a', 'version': 'v1'})

    response = bootstrap.on_event(_event(request_type=request_type), None)

    data = response['Data']
    assert data['DefaultNamespaceId'] == 'default'
    assert data['Workgroup'] == f'{PREFIX}-ns-default'
    assert data['Adopted'] == 'false'
    assert data['PlatformAdminGrantsWritten'] == '1'
    assert data['OntologyRowsBackfilled'] == '1'


def test_the_physical_id_is_stable_across_invocations(tables, stubbed_athena) -> None:
    """A changed physical id makes CloudFormation delete the previous resource."""
    first = bootstrap.on_event(_event(request_type='Create'), None)
    second = bootstrap.on_event(_event(request_type='Update'), None)
    assert first['PhysicalResourceId'] == second['PhysicalResourceId']


def test_delete_touches_nothing(tables, stubbed_athena) -> None:
    """A rollback arrives as a Delete; stripping admin grants then is the worst moment."""
    bootstrap.on_event(_event(request_type='Create'), None)
    grants_before = tables['grants'].scan()['Count']

    response = bootstrap.on_event(_event(request_type='Delete'), None)

    assert response['Data'] == {}
    assert tables['grants'].scan()['Count'] == grants_before
    assert tables['namespaces'].get_item(
        Key={'pk': 'NS#default', 'sk': 'METADATA'}
    ).get('Item') is not None


@pytest.mark.parametrize(
    'missing',
    [
        'NamespacesTable',
        'GrantsTable',
        'MetadataTable',
        'DefaultNamespaceId',
        'DefaultNamespaceName',
    ],
)
def test_a_missing_required_property_fails_the_deploy(
    tables, stubbed_athena, missing: str
) -> None:
    """Seeding into a half-configured deployment would look like it succeeded."""
    with pytest.raises(RuntimeError, match=missing):
        bootstrap.on_event(_event(request_type='Create', **{missing: None}), None)
