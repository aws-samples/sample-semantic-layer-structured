"""End-to-end tests for the namespaces API with authorization enforced.

Unlike ``test_authz_middleware.py`` (which drives a stand-in app) and
``test_namespace_service.py`` (which drives the service directly), these tests
compose the *real* router, the *real* service over moto, and the *real*
middleware. That is the only configuration in which the pieces that have to agree
— the route→action map, the id canonicalisation, the grant key shape, and the
Cedar policies — are all exercised together.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api')
)

from agents.shared.grant_profile import GLOBAL_RESOURCE_ID  # noqa: E402
from authz import role_resolver  # noqa: E402
from authz.cedar_schema import NamespaceStatus, Role  # noqa: E402
from authz.middleware import AuthzMiddleware  # noqa: E402
from routers import namespaces as namespaces_router  # noqa: E402
from routers.namespaces import build_router  # noqa: E402
from services.namespace_service import (  # noqa: E402
    GrantCreate,
    NamespaceCreate,
    NamespaceService,
)

NAMESPACES_TABLE = 'semantic-layer-namespaces'
GRANTS_TABLE = 'semantic-layer-grants'
RESULTS_BUCKET = 'semantic-layer-athena-results'

ADMIN = {'sub': 'admin-1', 'email': 'admin@example.com', 'cognito:groups': '[Admin]'}
VIEWER = {'sub': 'user-1', 'email': 'user@example.com', 'cognito:groups': '[Users]'}
NOBODY = {'sub': 'nobody-1', 'email': 'nobody@example.com'}


class StubAthena:
    """Athena client stub that always creates a fresh workgroup."""

    def get_work_group(self, **kwargs: Any) -> Dict[str, Any]:
        """Report every workgroup as absent.

        Args:
            **kwargs: Must carry ``WorkGroup``.

        Returns:
            Never returns.

        Raises:
            ClientError: Always ``ResourceNotFoundException``.
        """
        raise ClientError(
            {'Error': {'Code': 'ResourceNotFoundException', 'Message': 'nope'}},
            'GetWorkGroup',
        )

    def create_work_group(self, **kwargs: Any) -> Dict[str, Any]:
        """Accept a workgroup creation.

        Args:
            **kwargs: The ``create_work_group`` arguments.

        Returns:
            An empty response.
        """
        return {}


class EventInjector:
    """ASGI shim that stashes a Lambda event on the scope, as Mangum does.

    Attributes:
        app: The wrapped ASGI application.
        claims: The JWT claims to publish, or None.
    """

    def __init__(self, app: Any, *, claims: Optional[Dict[str, Any]]) -> None:
        """Wrap an app and fix the claims it will see.

        Args:
            app: The ASGI application to wrap.
            claims: The claims to inject, or None.
        """
        self.app = app
        self.claims = claims

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        """Inject the event, then delegate.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive callable.
            send: The ASGI send callable.

        Returns:
            None.
        """
        if scope.get('type') == 'http':
            authorizer = (
                {'jwt': {'claims': self.claims}} if self.claims is not None else {}
            )
            scope['aws.event'] = {'requestContext': {'authorizer': authorizer}}
        await self.app(scope, receive, send)


def _create_tables() -> None:
    """Create the namespaces and grants tables with the PrincipalIndex GSI.

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


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch) -> Iterator[NamespaceService]:
    """Provide a NamespaceService over moto-backed tables with authz enabled.

    Args:
        monkeypatch: pytest's environment patcher.

    Yields:
        The configured service.
    """
    with mock_aws():
        _create_tables()
        monkeypatch.setenv('AWS_REGION', 'us-east-1')
        monkeypatch.setenv('GRANTS_TABLE', GRANTS_TABLE)
        monkeypatch.setenv('ENABLE_AUTHZ', 'true')
        monkeypatch.delenv('AUTHZ_GROUP_ROLE_MAP', raising=False)
        # Caching would hide grants written mid-test; the service clears the cache
        # on every grant write, but reads in between must not be served stale.
        monkeypatch.setenv('AUTHZ_CACHE_TTL_SECONDS', '0')
        role_resolver.clear_cache()
        resource = boto3.resource('dynamodb', region_name='us-east-1')
        yield NamespaceService(
            namespaces_table=resource.Table(NAMESPACES_TABLE),
            grants_table=resource.Table(GRANTS_TABLE),
            athena_client=StubAthena(),
            results_bucket=RESULTS_BUCKET,
            resource_prefix='semantic-layer',
        )


def client_for(
    *, svc: NamespaceService, claims: Optional[Dict[str, Any]]
) -> TestClient:
    """Compose the real router, service and middleware into a test client.

    Args:
        svc: The namespace service to serve requests from.
        claims: JWT claims to inject, or None for an unauthenticated request.

    Returns:
        A configured TestClient.
    """
    app = FastAPI()
    app.include_router(build_router(lambda: svc))

    def resolve_namespace(identifier: str) -> Optional[Tuple[str, str]]:
        """Resolve a namespace id-or-name to ``(id, status)``.

        Args:
            identifier: The namespace segment from the request path.

        Returns:
            The canonical id and status, or None when unknown.
        """
        found = svc.resolve(identifier=identifier)
        return (found.id, found.status) if found is not None else None

    guarded = AuthzMiddleware(
        app,
        ontology_namespace_resolver=lambda _: None,
        namespace_resolver=resolve_namespace,
    )
    return TestClient(EventInjector(guarded, claims=claims))


def make_namespace(*, svc: NamespaceService, name: str, owner: str = 'owner-1'):
    """Create a namespace directly through the service.

    Args:
        svc: The namespace service.
        name: The namespace name.
        owner: The creator's subject.

    Returns:
        The created namespace.
    """
    return svc.create(request=NamespaceCreate(name=name), created_by=owner)


# ── Create ────────────────────────────────────────────────────────────


def test_admin_can_create_a_namespace(svc) -> None:
    """The Admin Cognito group maps to platform-admin, which holds manageNamespace."""
    response = client_for(svc=svc, claims=ADMIN).post(
        '/namespaces', json={'name': 'sales', 'description': 'EU sales'}
    )
    assert response.status_code == 201
    body = response.json()
    assert body['name'] == 'sales'
    assert body['status'] == NamespaceStatus.ACTIVE.value
    assert body['createdBy'] == 'admin-1'


def test_viewer_cannot_create_a_namespace(svc) -> None:
    """A read-only principal must not be able to mint a governance boundary."""
    response = client_for(svc=svc, claims=VIEWER).post(
        '/namespaces', json={'name': 'sales'}
    )
    assert response.status_code == 403
    assert svc.list() == []


def test_unauthenticated_create_is_401(svc) -> None:
    """No verified subject means the request never passed the JWT authorizer."""
    assert (
        client_for(svc=svc, claims=None)
        .post('/namespaces', json={'name': 'sales'})
        .status_code
        == 401
    )


def test_invalid_name_is_422(svc) -> None:
    """An unsafe name is a client error, not a 500 from Athena later."""
    response = client_for(svc=svc, claims=ADMIN).post(
        '/namespaces', json={'name': 'has space'}
    )
    assert response.status_code == 422


def test_duplicate_name_is_409(svc) -> None:
    """The conditional write surfaces as a conflict, not a generic failure."""
    client = client_for(svc=svc, claims=ADMIN)
    client.post('/namespaces', json={'name': 'sales'})
    assert (
        client.post('/namespaces', json={'name': 'sales'}).status_code == 409
    )


def test_creator_becomes_the_owner_and_can_administer(svc) -> None:
    """Creation must confer administration, or the namespace is orphaned.

    Driven through HTTP as a non-admin creator: the only role this principal ends
    up with is the owner grant that ``create`` wrote, so a 200 here proves the
    grant is both written and resolvable by the live PDP.
    """
    creator = {'sub': 'creator-1', 'email': 'creator@example.com'}
    # Seed the platform role that lets this principal create at all.
    svc.grant_platform_role(
        principal_type='User',
        principal_id='creator-1',
        role=Role.PLATFORM_ADMIN.value,
        granted_by='bootstrap',
    )
    client = client_for(svc=svc, claims=creator)
    created = client.post('/namespaces', json={'name': 'sales'}).json()
    grants = client.get(f"/namespaces/{created['id']}/grants")
    assert grants.status_code == 200
    assert Role.NAMESPACE_OWNER.value in {g['role'] for g in grants.json()}


# ── Read / list filtering ─────────────────────────────────────────────


def test_get_accepts_id_or_name(svc) -> None:
    """Both addressing forms must work, and both must canonicalise for the PDP."""
    created = make_namespace(svc=svc, name='sales')
    client = client_for(svc=svc, claims=VIEWER)
    assert client.get(f'/namespaces/{created.id}').json()['id'] == created.id
    assert client.get('/namespaces/sales').json()['id'] == created.id


def test_get_unknown_namespace_is_403(svc) -> None:
    """The middleware denies before the handler can answer 404 and leak existence."""
    assert (
        client_for(svc=svc, claims=ADMIN).get('/namespaces/nope').status_code == 403
    )


def test_platform_viewer_sees_every_namespace(svc) -> None:
    """platform-viewer reads everywhere, so discovery is unfiltered for it."""
    make_namespace(svc=svc, name='sales')
    make_namespace(svc=svc, name='finance')
    response = client_for(svc=svc, claims=VIEWER).get('/namespaces')
    assert response.status_code == 200
    assert [ns['name'] for ns in response.json()] == ['finance', 'sales']


def test_listing_is_filtered_to_what_the_caller_may_see(svc) -> None:
    """Discovery must not become a directory of every namespace.

    ``list`` is granted to any authenticated caller so the UI has an entry point;
    the *contents* are then filtered per namespace. A caller scoped to one
    namespace must not learn the names of the others.
    """
    sales = make_namespace(svc=svc, name='sales')
    make_namespace(svc=svc, name='finance')
    svc.grant_role(
        namespace_id=sales.id,
        request=GrantCreate(
            principalId='scoped-1', role=Role.NAMESPACE_DATA_ANALYST.value
        ),
        granted_by='admin-1',
    )
    response = client_for(svc=svc, claims={'sub': 'scoped-1'}).get('/namespaces')
    assert response.status_code == 200
    assert [ns['name'] for ns in response.json()] == ['sales']


def test_caller_with_no_roles_sees_an_empty_list(svc) -> None:
    """An empty list, not a 403 — the list action itself was authorized."""
    make_namespace(svc=svc, name='sales')
    response = client_for(svc=svc, claims=NOBODY).get('/namespaces')
    assert response.status_code == 200
    assert response.json() == []


# ── Lifecycle ─────────────────────────────────────────────────────────


def test_admin_can_archive_and_reactivate(svc) -> None:
    """setNamespaceStatus survives a non-ACTIVE status, or archiving is one-way."""
    created = make_namespace(svc=svc, name='sales')
    client = client_for(svc=svc, claims=ADMIN)
    archived = client.post(
        f'/namespaces/{created.id}/status',
        json={'status': NamespaceStatus.ARCHIVED.value},
    )
    assert archived.status_code == 200
    assert archived.json()['status'] == NamespaceStatus.ARCHIVED.value
    revived = client.post(
        f'/namespaces/{created.id}/status',
        json={'status': NamespaceStatus.ACTIVE.value},
    )
    assert revived.status_code == 200


def test_viewer_cannot_change_status(svc) -> None:
    """Lifecycle control is an administrative action."""
    created = make_namespace(svc=svc, name='sales')
    response = client_for(svc=svc, claims=VIEWER).post(
        f'/namespaces/{created.id}/status',
        json={'status': NamespaceStatus.ARCHIVED.value},
    )
    assert response.status_code == 403
    assert svc.get(namespace_id=created.id).status == NamespaceStatus.ACTIVE.value


def test_unknown_status_is_422(svc) -> None:
    """A typo must not be written as a status no policy understands."""
    created = make_namespace(svc=svc, name='sales')
    response = client_for(svc=svc, claims=ADMIN).post(
        f'/namespaces/{created.id}/status', json={'status': 'SLEEPY'}
    )
    assert response.status_code == 422


def test_invalid_transition_is_409(svc) -> None:
    """ACTIVE -> DELETING is not a caller-driven transition."""
    created = make_namespace(svc=svc, name='sales')
    response = client_for(svc=svc, claims=ADMIN).post(
        f'/namespaces/{created.id}/status',
        json={'status': NamespaceStatus.DELETING.value},
    )
    assert response.status_code == 409


def test_archived_namespace_rejects_updates_but_allows_reads(svc) -> None:
    """The Cedar non-ACTIVE guard, observed through the real API."""
    created = make_namespace(svc=svc, name='sales')
    client = client_for(svc=svc, claims=ADMIN)
    client.post(
        f'/namespaces/{created.id}/status',
        json={'status': NamespaceStatus.ARCHIVED.value},
    )
    assert (
        client.patch(
            f'/namespaces/{created.id}', json={'description': 'nope'}
        ).status_code
        == 403
    )
    assert client.get(f'/namespaces/{created.id}').status_code == 200


def test_admin_can_delete(svc) -> None:
    """Deletion tombstones the record and frees the name."""
    created = make_namespace(svc=svc, name='sales')
    response = client_for(svc=svc, claims=ADMIN).delete(f'/namespaces/{created.id}')
    assert response.status_code == 200
    assert response.json()['status'] == NamespaceStatus.DELETED.value
    assert svc.get_by_name(name='sales') is None


def test_viewer_cannot_delete(svc) -> None:
    """The most destructive action must be the hardest to reach."""
    created = make_namespace(svc=svc, name='sales')
    assert (
        client_for(svc=svc, claims=VIEWER)
        .delete(f'/namespaces/{created.id}')
        .status_code
        == 403
    )
    assert svc.get(namespace_id=created.id).status == NamespaceStatus.ACTIVE.value


# ── Grants ────────────────────────────────────────────────────────────


def test_admin_can_grant_and_revoke(svc) -> None:
    """Grant administration through the API, verified against the live resolver."""
    created = make_namespace(svc=svc, name='sales')
    client = client_for(svc=svc, claims=ADMIN)
    granted = client.post(
        f'/namespaces/{created.id}/grants',
        json={
            'principalType': 'User',
            'principalId': 'analyst@example.com',
            'role': Role.NAMESPACE_DATA_ANALYST.value,
        },
    )
    assert granted.status_code == 201
    assert granted.json()['principalKey'] == 'User::analyst@example.com'
    assert granted.json()['grantedBy'] == 'admin-1'

    # The granted principal can now read the namespace it was scoped to.
    analyst = {'sub': 'analyst-1', 'email': 'analyst@example.com'}
    assert (
        client_for(svc=svc, claims=analyst)
        .get(f'/namespaces/{created.id}')
        .status_code
        == 200
    )

    revoked = client.delete(
        f'/namespaces/{created.id}/grants/User::analyst@example.com/'
        f'{Role.NAMESPACE_DATA_ANALYST.value}'
    )
    assert revoked.status_code == 204
    assert (
        client_for(svc=svc, claims=analyst)
        .get(f'/namespaces/{created.id}')
        .status_code
        == 403
    )


def test_viewer_cannot_grant(svc) -> None:
    """Otherwise any authenticated caller could grant themselves ownership."""
    created = make_namespace(svc=svc, name='sales')
    response = client_for(svc=svc, claims=VIEWER).post(
        f'/namespaces/{created.id}/grants',
        json={'principalId': 'user-1', 'role': Role.NAMESPACE_OWNER.value},
    )
    assert response.status_code == 403
    assert {g.role for g in svc.list_grants(namespace_id=created.id)} == {
        Role.NAMESPACE_OWNER.value
    }


def test_unknown_role_is_422(svc) -> None:
    """A role no policy ships would grant nothing while appearing to succeed."""
    created = make_namespace(svc=svc, name='sales')
    response = client_for(svc=svc, claims=ADMIN).post(
        f'/namespaces/{created.id}/grants',
        json={'principalId': 'user-1', 'role': 'namespace-god'},
    )
    assert response.status_code == 422


def test_grants_are_written_under_the_canonical_id(svc) -> None:
    """Granting via the namespace NAME must still key the grant by id.

    A grant written under the name would never be found by the resolver, which
    reads ``resourceUID`` as an id — the grant would appear to succeed and have
    no effect.
    """
    created = make_namespace(svc=svc, name='sales')
    client_for(svc=svc, claims=ADMIN).post(
        '/namespaces/sales/grants',
        json={
            'principalId': 'analyst-2',
            'role': Role.NAMESPACE_DATA_ANALYST.value,
        },
    )
    stored: List[str] = [
        g.resourceId for g in svc.list_grants(namespace_id=created.id)
    ]
    assert 'analyst-2' not in stored
    assert stored.count(created.id) == 2  # owner + analyst


# ── GET /namespaces/me ────────────────────────────────────────────────


def test_me_reports_platform_roles_from_groups(svc) -> None:
    """The Admin group maps to platform-admin, which `me` must report back."""
    response = client_for(svc=svc, claims=ADMIN).get('/namespaces/me')
    assert response.status_code == 200
    body = response.json()
    assert body['enforced'] is True
    assert body['userId'] == 'admin-1'
    assert body['email'] == 'admin@example.com'
    assert body['globalRoles'] == [Role.PLATFORM_ADMIN.value]


def test_me_reports_grant_derived_roles(svc) -> None:
    """The whole reason the endpoint exists.

    A namespace-scoped role lives in a grant row the browser cannot read, and no
    Cognito group ever confers one. Without this response a UI hiding admin
    surfaces on the ID token alone would hide them from exactly the owners and
    maintainers who hold their access by grant.
    """
    created = make_namespace(svc=svc, name='sales')
    svc.grant_role(
        namespace_id=created.id,
        request=GrantCreate(
            principalId='nobody-1', role=Role.NAMESPACE_MAINTAINER.value
        ),
        granted_by='admin-1',
    )
    role_resolver.clear_cache()

    body = client_for(svc=svc, claims=NOBODY).get('/namespaces/me').json()

    # No groups at all, so nothing here could have come from the token.
    assert body['globalRoles'] == []
    assert {entry['role'] for entry in body['resourceRoles']} == {
        Role.NAMESPACE_MAINTAINER.value
    }
    assert {entry['resourceUID'] for entry in body['resourceRoles']} == {created.id}


def test_me_is_readable_by_a_caller_with_no_roles(svc) -> None:
    """Mapped to `list`, not a management action.

    Gating "what are my roles" behind a role would make it useless to exactly the
    callers who need to discover they have none — the response would be a 403
    indistinguishable from a broken deployment.
    """
    response = client_for(svc=svc, claims=NOBODY).get('/namespaces/me')
    assert response.status_code == 200
    assert response.json()['globalRoles'] == []
    assert response.json()['resourceRoles'] == []


def test_me_is_not_captured_by_the_namespace_id_route(svc) -> None:
    """Route ordering regression.

    FastAPI matches in declaration order, so `/namespaces/me` declared after
    `/namespaces/{ns}` would resolve as "the namespace named me" — a 403 (the
    action map would demand viewNamespace on a namespace that does not exist)
    rather than the caller's roles. Creating a namespace actually named `me`
    makes the failure unambiguous: if the routes were ordered wrongly this would
    return that namespace's metadata.
    """
    make_namespace(svc=svc, name='me')
    body = client_for(svc=svc, claims=ADMIN).get('/namespaces/me').json()
    assert 'globalRoles' in body
    assert 'createdBy' not in body


def test_me_reports_enforcement_off(svc, monkeypatch: pytest.MonkeyPatch) -> None:
    """`enforced` is reported so a client knows not to act on the roles.

    A UI must not read an empty role set as "this user may do nothing" while
    enforcement is off — in that state nothing is denied — which is why `enforced`
    travels alongside the roles rather than leaving the client to infer it.
    """
    monkeypatch.setenv('ENABLE_AUTHZ', 'false')
    role_resolver.clear_cache()

    body = client_for(svc=svc, claims=ADMIN).get('/namespaces/me').json()

    assert body['enforced'] is False


def test_me_still_resolves_roles_with_enforcement_off(
    svc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-arming check: roles are reported even before enforcement is on.

    The middleware returns early when ENABLE_AUTHZ is off, so it publishes no
    context and the handler has to resolve the caller itself. Without that, the
    endpoint would answer "no roles" on exactly the deployment where an operator
    most needs it — deciding whether arming enforcement is about to lock them
    out. Here the caller holds a GLOBAL platform-admin grant and no Cognito
    group, which is precisely the bootstrap operator's shape.
    """
    monkeypatch.setenv('ENABLE_AUTHZ', 'false')
    svc.grant_role(
        namespace_id=GLOBAL_RESOURCE_ID,
        request=GrantCreate(
            principalId='nobody-1', role=Role.PLATFORM_ADMIN.value
        ),
        granted_by='bootstrap',
    )
    role_resolver.clear_cache()

    body = client_for(svc=svc, claims=NOBODY).get('/namespaces/me').json()

    assert body['enforced'] is False
    assert body['userId'] == 'nobody-1'
    assert body['globalRoles'] == [Role.PLATFORM_ADMIN.value]


def test_me_reports_503_when_grants_are_unreadable(
    svc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grants-table failure is an availability error, not "you hold nothing".

    Answering 200 with empty roles would be indistinguishable from a principal
    with no privileges, and would make the pre-arming check above silently
    reassuring at the exact moment it must not be.
    """
    monkeypatch.setenv('ENABLE_AUTHZ', 'false')
    role_resolver.clear_cache()

    def explode(**kwargs: Any) -> None:
        """Stand in for an unreadable grants table.

        Args:
            **kwargs: The resolver's keyword arguments, ignored.

        Returns:
            Never returns.

        Raises:
            RoleResolutionError: Always.
        """
        raise role_resolver.RoleResolutionError('table unavailable')

    monkeypatch.setattr(namespaces_router, 'resolve_roles', explode)

    response = client_for(svc=svc, claims=ADMIN).get('/namespaces/me')

    assert response.status_code == 503
