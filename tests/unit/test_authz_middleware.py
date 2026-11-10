"""Unit tests for the ASGI policy enforcement point.

The property under test is *structural coverage*: because the middleware wraps
the root app, no route — including routes on mounted sub-apps and included
routers — can be reached without a decision. These tests assert that by mounting
a sub-app and a router and driving real HTTP requests through the middleware.
"""
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api')
)

from authz import role_resolver  # noqa: E402
from authz.cedar_schema import NamespaceStatus  # noqa: E402
from authz.middleware import (  # noqa: E402
    SCOPE_KEY,
    AuthzMiddleware,
    authz_enabled,
    extract_claims,
    parse_groups,
)

NS = 'ns-1'


@pytest.fixture(autouse=True)
def clean_authz_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from ambient authz configuration and cached roles.

    Args:
        monkeypatch: pytest's environment patcher.

    Returns:
        None.
    """
    monkeypatch.delenv('GRANTS_TABLE', raising=False)
    monkeypatch.delenv('AUTHZ_GROUP_ROLE_MAP', raising=False)
    monkeypatch.delenv('AUTHZ_CACHE_TTL_SECONDS', raising=False)
    monkeypatch.setenv('ENABLE_AUTHZ', 'true')
    role_resolver.clear_cache()


class EventInjector:
    """ASGI shim that stashes a Lambda event on the scope, as Mangum does.

    Attributes:
        app: The wrapped ASGI application.
        claims: JWT claims to publish under
            ``requestContext.authorizer.jwt.claims``, or None for an
            unauthenticated request.
    """

    def __init__(
        self, app: Callable[..., Any], *, claims: Optional[Dict[str, Any]]
    ) -> None:
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


def build_app() -> FastAPI:
    """Build a miniature stand-in for the REST API's route topology.

    Mirrors the real app in the way that matters here: routes on the root app, a
    mounted sub-app, and an included router under a prefix.

    Returns:
        The FastAPI application.
    """
    root = FastAPI()

    @root.get('/health')
    def health() -> Dict[str, str]:
        """Unauthenticated liveness probe.

        Returns:
            A static status payload.
        """
        return {'status': 'ok'}

    @root.get('/namespaces')
    def list_namespaces() -> Dict[str, List[str]]:
        """Namespace discovery.

        Returns:
            An empty namespace list.
        """
        return {'namespaces': []}

    # A genuine ``NamespaceSource.PATH`` route — the path segment really does
    # hold a namespace id or name here, unlike the /rules and /metrics routes.
    @root.get('/namespaces/{ns}/grants')
    def list_grants(ns: str) -> Dict[str, str]:
        """List the grants recorded on one namespace.

        Args:
            ns: The namespace id or name from the path.

        Returns:
            The echoed namespace identifier.
        """
        return {'namespace': ns}

    ontology = FastAPI()

    @ontology.get('/config/{ontology_id}')
    def get_config(ontology_id: str) -> Dict[str, str]:
        """Read one ontology configuration.

        Args:
            ontology_id: The ontology identifier.

        Returns:
            The echoed identifier.
        """
        return {'id': ontology_id}

    @ontology.get('/health')
    def ontology_health() -> Dict[str, str]:
        """Sub-app liveness probe.

        Returns:
            A static status payload.
        """
        return {'status': 'ok'}

    root.mount('/ontology', ontology)

    rules = APIRouter()

    @rules.get('/namespaces/{namespace}/rules')
    def list_rules(namespace: str) -> Dict[str, str]:
        """List rules in a namespace.

        Args:
            namespace: The namespace identifier.

        Returns:
            The echoed namespace.
        """
        return {'namespace': namespace}

    @rules.post('/namespaces/{namespace}/rules/{rule_id}:approve')
    def approve_rule(namespace: str, rule_id: str) -> Dict[str, str]:
        """Approve a governed rule.

        Args:
            namespace: The namespace identifier.
            rule_id: The rule identifier.

        Returns:
            The approved rule identifier.
        """
        return {'approved': rule_id}

    root.include_router(rules, prefix='/rules')

    return root


def make_client(
    *,
    claims: Optional[Dict[str, Any]],
    ontology_namespace: Optional[str] = NS,
    namespace_status: Optional[str] = NamespaceStatus.ACTIVE.value,
    canonical_id: Optional[str] = None,
    app: Optional[FastAPI] = None,
    status_raises: bool = False,
) -> TestClient:
    """Build a TestClient over app -> AuthzMiddleware -> event injector.

    Args:
        claims: JWT claims to inject, or None for an unauthenticated request.
        ontology_namespace: What the ontology->namespace resolver returns.
        namespace_status: What the namespace resolver reports as the status, or
            None to make the namespace unknown.
        canonical_id: The id the namespace resolver canonicalises to. Defaults to
            echoing the requested identifier.
        app: Override the application under test.
        status_raises: Make the namespace resolver raise, for the 503 path.

    Returns:
        A configured TestClient.
    """

    def resolve_ontology(ontology_id: str) -> Optional[str]:
        """Stub ontology->namespace binding.

        Args:
            ontology_id: The ontology identifier.

        Returns:
            The configured namespace, or None.
        """
        return ontology_namespace

    def resolve_namespace(namespace: str) -> Optional[Tuple[str, str]]:
        """Stub namespace id-or-name -> (id, status) lookup.

        Args:
            namespace: The namespace identifier from the request.

        Returns:
            The canonical id and status, or None when unknown.

        Raises:
            RuntimeError: When ``status_raises`` is set.
        """
        if status_raises:
            raise RuntimeError('DynamoDB unavailable')
        if namespace_status is None:
            return None
        return (canonical_id or namespace, namespace_status)

    guarded = AuthzMiddleware(
        app or build_app(),
        ontology_namespace_resolver=resolve_ontology,
        namespace_resolver=resolve_namespace,
    )
    return TestClient(EventInjector(guarded, claims=claims))


def admin_claims() -> Dict[str, Any]:
    """Claims for a member of the Admin Cognito group.

    Returns:
        The claims mapping.
    """
    return {'sub': 'admin-1', 'email': 'admin@example.com', 'cognito:groups': '[Admin]'}


def viewer_claims() -> Dict[str, Any]:
    """Claims for a member of the Users Cognito group.

    Returns:
        The claims mapping.
    """
    return {'sub': 'user-1', 'email': 'user@example.com', 'cognito:groups': '[Users]'}


# ── Group claim parsing ───────────────────────────────────────────────


@pytest.mark.parametrize(
    'raw,expected',
    [
        (None, []),
        ('', []),
        ('Admin', ['Admin']),
        (['Admin', 'Users'], ['Admin', 'Users']),
        ('[Admin Users]', ['Admin', 'Users']),
        ('[Admin]', ['Admin']),
        ('Admin,Users', ['Admin', 'Users']),
        ('[Admin, Users]', ['Admin', 'Users']),
    ],
)
def test_parse_groups_handles_every_claim_shape(
    raw: Any, expected: List[str]
) -> None:
    """The JWT authorizer stringifies multi-valued claims as ``[a b]``.

    Handling only the list form would drop every group of every multi-group
    user, which presents as "this admin has no roles".
    """
    assert parse_groups(raw=raw) == expected


def test_extract_claims_returns_empty_without_an_authorizer() -> None:
    """A request that never passed the authorizer yields no claims."""
    assert extract_claims(scope={}) == {}
    assert extract_claims(scope={'aws.event': {'requestContext': {}}}) == {}


# ── Feature flag ──────────────────────────────────────────────────────


def test_disabled_flag_passes_everything_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deploying this code changes nothing until ENABLE_AUTHZ is set."""
    monkeypatch.setenv('ENABLE_AUTHZ', 'false')
    assert authz_enabled() is False
    client = make_client(claims=None)
    assert client.get('/namespaces').status_code == 200


@pytest.mark.parametrize('value', ['true', 'TRUE', '1', 'yes'])
def test_flag_truthy_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Operators write the flag several ways; all of them enable enforcement."""
    monkeypatch.setenv('ENABLE_AUTHZ', value)
    assert authz_enabled() is True


# ── Authentication ────────────────────────────────────────────────────


def test_missing_principal_is_401() -> None:
    """No verified `sub` means the request never passed the JWT authorizer."""
    response = make_client(claims=None).get('/namespaces')
    assert response.status_code == 401
    assert response.json() == {'detail': 'Unauthenticated'}


def test_claims_without_sub_are_401() -> None:
    """A claims block with no subject is not a principal."""
    response = make_client(claims={'email': 'x@y.com'}).get('/namespaces')
    assert response.status_code == 401


# ── Bypass paths ──────────────────────────────────────────────────────


def test_root_health_needs_no_token() -> None:
    """The liveness probe must answer without credentials."""
    assert make_client(claims=None).get('/health').status_code == 200


def test_mounted_sub_app_health_needs_no_token() -> None:
    """Per-sub-app health endpoints are bypassed too."""
    assert make_client(claims=None).get('/ontology/health').status_code == 200


def test_options_preflight_is_not_denied() -> None:
    """CORS preflight carries no Authorization header by specification."""
    response = make_client(claims=None).options('/namespaces')
    assert response.status_code != 401


# ── Structural coverage ───────────────────────────────────────────────


def test_mounted_sub_app_route_is_gated() -> None:
    """Root middleware wraps mounted ASGI apps — this is the coverage guarantee."""
    response = make_client(claims=viewer_claims()).get('/ontology/config/ont-1')
    assert response.status_code == 200
    response = make_client(claims={'sub': 'nobody-1'}).get('/ontology/config/ont-1')
    assert response.status_code == 403


def test_included_router_route_is_gated() -> None:
    """Included routers are covered by the same wrapper."""
    response = make_client(claims={'sub': 'nobody-1'}).get(
        f'/rules/namespaces/{NS}/rules'
    )
    assert response.status_code == 403


def test_unmapped_route_is_403_even_for_an_admin() -> None:
    """The denyAll sentinel is forbidden, so privilege does not open a new route."""
    app = build_app()

    @app.get('/undeclared')
    def undeclared() -> Dict[str, bool]:
        """A route that exists in code but not in the action map.

        Returns:
            A marker payload that must never be returned.
        """
        return {'reached': True}

    response = make_client(claims=admin_claims(), app=app).get('/undeclared')
    assert response.status_code == 403


# ── Decisions ─────────────────────────────────────────────────────────


def test_admin_group_may_approve_a_rule() -> None:
    """The Admin Cognito group maps to platform-admin, which holds approveRule."""
    response = make_client(claims=admin_claims()).post(
        f'/rules/namespaces/{NS}/rules/r1:approve'
    )
    assert response.status_code == 200


def test_users_group_may_read_but_not_approve() -> None:
    """The migration-safe default: reads keep working, the approval gate closes.

    This is the concrete hole docs/comparison-coa.md §3.2 names — the governed
    rule approval endpoint was reachable by any authenticated caller.
    """
    client = make_client(claims=viewer_claims())
    assert client.get(f'/rules/namespaces/{NS}/rules').status_code == 200
    assert (
        client.post(f'/rules/namespaces/{NS}/rules/r1:approve').status_code == 403
    )


def test_group_map_override_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator can retire the built-in mapping without a redeploy."""
    monkeypatch.setenv('AUTHZ_GROUP_ROLE_MAP', '{"Admin": "platform-viewer"}')
    response = make_client(claims=admin_claims()).post(
        f'/rules/namespaces/{NS}/rules/r1:approve'
    )
    assert response.status_code == 403


def test_malformed_group_map_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in the map must not silently restore the built-in default."""
    monkeypatch.setenv('AUTHZ_GROUP_ROLE_MAP', '{not json')
    response = make_client(claims=admin_claims()).get(f'/rules/namespaces/{NS}/rules')
    assert response.status_code == 503


# ── Namespace lifecycle ───────────────────────────────────────────────


def test_archived_namespace_blocks_mutation_even_for_an_admin() -> None:
    """The non-ACTIVE forbid overrides platform-admin."""
    response = make_client(
        claims=admin_claims(), namespace_status=NamespaceStatus.ARCHIVED.value
    ).post(f'/rules/namespaces/{NS}/rules/r1:approve')
    assert response.status_code == 403


def test_archived_namespace_still_reads() -> None:
    """An archived namespace is retained precisely so it can still be read."""
    response = make_client(
        claims=admin_claims(), namespace_status=NamespaceStatus.ARCHIVED.value
    ).get(f'/rules/namespaces/{NS}/rules')
    assert response.status_code == 200


def test_path_name_is_canonicalised_to_the_namespace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant on the id must authorize a request that addressed the name.

    Legacy routes take a namespace *name* while grants record the namespace
    **id**. Without canonicalisation the decision compares a name to an id, so
    every namespace-scoped grant silently fails to match and only platform roles
    work — a denial with no error anywhere.
    """
    from agents.shared.grant_profile import ResolvedProfile

    namespace_id = 'ns-uuid-1'
    monkeypatch.setattr(
        role_resolver,
        'resolve_profile',
        lambda **kwargs: ResolvedProfile(
            user_id='owner-1',
            resource_roles=[
                {'role': 'namespace-owner', 'resourceUID': namespace_id}
            ],
        ),
    )
    client = make_client(
        claims={'sub': 'owner-1'}, canonical_id=namespace_id
    )
    # 'sales' is the namespace NAME; the resolver maps it to namespace_id.
    # Uses a genuine PATH route: /namespaces/{ns} is the only family whose path
    # segment holds a namespace identifier at all.
    response = client.get('/namespaces/sales/grants')
    assert response.status_code == 200


def test_layer_id_is_resolved_to_its_owning_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a governed-rules request names a LAYER, not a namespace.

    ``/rules/namespaces/{x}/rules`` carries a semantic-layer id in ``x``. When
    the route was declared PATH, ``x`` went straight to the namespace registry,
    which has no row for a layer id, and the middleware answered 403
    ``reason=unknown_namespace`` — so both governance tabs were dead for every
    caller. The decision must be evaluated against the namespace that OWNS the
    layer, which is where the caller's grant lives.
    """
    from agents.shared.grant_profile import ResolvedProfile

    namespace_id = 'ns-owning-1'
    layer_id = '51e29707-cafe-4a1b-9f00-1d2c3b4a5e6f'
    monkeypatch.setattr(
        role_resolver,
        'resolve_profile',
        lambda **kwargs: ResolvedProfile(
            user_id='owner-1',
            resource_roles=[
                {'role': 'namespace-owner', 'resourceUID': namespace_id}
            ],
        ),
    )
    # A registry that behaves like the live one: it knows the namespace and
    # NOTHING else. Passing the layer id to it must fail, which is what makes
    # this test fail if the route is ever declared PATH again.
    def registry(namespace: str) -> Optional[Tuple[str, str]]:
        """Stub namespace registry holding exactly one row.

        Args:
            namespace: The identifier the middleware resolved.

        Returns:
            The canonical id and status, or None when the identifier is not a
            known namespace (e.g. when a layer id is passed by mistake).
        """
        if namespace != namespace_id:
            return None
        return (namespace_id, NamespaceStatus.ACTIVE.value)

    guarded = AuthzMiddleware(
        build_app(),
        ontology_namespace_resolver=lambda _: namespace_id,
        namespace_resolver=registry,
    )
    client = TestClient(EventInjector(guarded, claims={'sub': 'owner-1'}))

    response = client.get(f'/rules/namespaces/{layer_id}/rules')
    assert response.status_code == 200
    # The handler still receives the LAYER id — the authz change must not
    # rewrite the path, because metrics and rules are stored per layer.
    assert response.json() == {'namespace': layer_id}


def test_unknown_namespace_is_403_not_404() -> None:
    """A 404 would let an unauthorized caller enumerate which namespaces exist."""
    response = make_client(claims=admin_claims(), namespace_status=None).get(
        f'/rules/namespaces/{NS}/rules'
    )
    assert response.status_code == 403


def test_status_lookup_failure_is_503() -> None:
    """An infrastructure failure is reported as one, and the request still stops."""
    response = make_client(claims=admin_claims(), status_raises=True).get(
        f'/rules/namespaces/{NS}/rules'
    )
    assert response.status_code == 503


def test_unbound_ontology_is_403() -> None:
    """An ontology with no namespace cannot be evaluated, so it is denied."""
    response = make_client(claims=admin_claims(), ontology_namespace=None).get(
        '/ontology/config/ont-1'
    )
    assert response.status_code == 403


def test_platform_scoped_route_needs_no_namespace_status() -> None:
    """A PLATFORM decision must not consult (or require) a namespace."""
    response = make_client(claims=viewer_claims(), status_raises=True).get(
        '/namespaces'
    )
    assert response.status_code == 200


# ── Published context ─────────────────────────────────────────────────


def test_allowed_request_publishes_its_authz_context() -> None:
    """Handlers and the SQL firewall reuse this resolution instead of re-reading."""
    captured: Dict[str, Any] = {}
    app = build_app()

    @app.get('/namespaces/{namespace}')
    def read_namespace(namespace: str) -> Dict[str, str]:
        """Read one namespace, capturing the published context.

        Args:
            namespace: The namespace identifier.

        Returns:
            The echoed namespace.
        """
        return {'namespace': namespace}

    class Capture:
        """Innermost ASGI app that records the scope key then delegates."""

        def __init__(self, inner: Callable[..., Any]) -> None:
            """Wrap the application under test.

            Args:
                inner: The ASGI application to delegate to.
            """
            self.inner = inner

        async def __call__(
            self, scope: Dict[str, Any], receive: Any, send: Any
        ) -> None:
            """Record ``scope['authz']`` and delegate.

            Args:
                scope: The ASGI connection scope.
                receive: The ASGI receive callable.
                send: The ASGI send callable.

            Returns:
                None.
            """
            if scope.get('type') == 'http':
                captured.update(scope.get(SCOPE_KEY) or {})
            await self.inner(scope, receive, send)

    guarded = AuthzMiddleware(
        Capture(app),
        ontology_namespace_resolver=lambda _: NS,
        namespace_resolver=lambda ns: (ns, NamespaceStatus.ACTIVE.value),
    )
    client = TestClient(EventInjector(guarded, claims=admin_claims()))
    assert client.get(f'/namespaces/{NS}').status_code == 200
    assert captured['userId'] == 'admin-1'
    assert captured['namespace'] == NS
    assert captured['action'] == 'viewNamespace'
    assert captured['globalRoles'] == ['platform-admin']
    assert captured['namespaceStatus'] == NamespaceStatus.ACTIVE.value
    # No grant declares a restriction, so the firewall profile carries identity
    # only — absent restrictions must not become empty ones.
    assert captured['grantProfile'] == {'userId': 'admin-1'}


def test_denied_request_never_reaches_the_handler() -> None:
    """The PDP short-circuits: a denied request must not run the route."""
    reached: List[str] = []
    app = build_app()

    @app.post('/namespaces/{namespace}/status')
    def set_status(namespace: str) -> Dict[str, str]:
        """Namespace status transition that records being reached.

        Args:
            namespace: The namespace identifier.

        Returns:
            The echoed namespace.
        """
        reached.append(namespace)
        return {'namespace': namespace}

    response = make_client(claims=viewer_claims(), app=app).post(
        f'/namespaces/{NS}/status'
    )
    assert response.status_code == 403
    assert reached == []
