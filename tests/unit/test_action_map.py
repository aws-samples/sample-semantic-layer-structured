"""Unit tests for the route -> Cedar action map.

The map is the structural half of the PDP: coverage here is what makes "an
unmapped route is denied" true. The centrepiece is
:func:`test_every_live_route_is_mapped`, which walks the live FastAPI route table
and fails when a route exists that the map does not cover.
"""
import os
import sys
from typing import List, Tuple

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api')
)

from authz.action_map import (  # noqa: E402
    NamespaceSource,
    RouteMatch,
    find_unmapped,
    is_bypass_path,
    mapped_patterns,
    match_route,
    path_sources,
    resolve_namespace,
)
from authz.cedar_schema import PLATFORM_RESOURCE_ID, Action  # noqa: E402

# The live route inventory, as (method, concrete path). Kept as concrete paths
# rather than templates so the regexes are exercised the way the middleware
# exercises them.
LIVE_ROUTES: List[Tuple[str, str]] = [
    # Namespaces
    ('POST', '/namespaces'),
    ('GET', '/namespaces'),
    ('GET', '/namespaces/ns-1'),
    ('PATCH', '/namespaces/ns-1'),
    ('DELETE', '/namespaces/ns-1'),
    ('POST', '/namespaces/ns-1/status'),
    ('GET', '/namespaces/ns-1/grants'),
    ('POST', '/namespaces/ns-1/grants'),
    ('DELETE', '/namespaces/ns-1/grants/User::u1/namespace-owner'),
    # Ontology
    ('POST', '/ontology/config'),
    ('GET', '/ontology/config/ont-1'),
    ('DELETE', '/ontology/config/ont-1'),
    ('GET', '/ontology/list'),
    ('POST', '/ontology/build/ont-1'),
    ('POST', '/ontology/delta/ont-1'),
    ('GET', '/ontology/build-status/ont-1'),
    ('GET', '/ontology/versions/ont-1'),
    ('GET', '/ontology/content/ont-1/v2'),
    ('POST', '/ontology/revise/ont-1/v2'),
    ('POST', '/ontology/upload'),
    # Data sources
    ('GET', '/datasource/glue/databases'),
    ('GET', '/datasource/glue/tables/db1'),
    ('GET', '/datasource/glue/metadata/db1/t1'),
    ('POST', '/datasource/extract-metadata'),
    # Query
    ('GET', '/query/suggestions/ont-1'),
    ('GET', '/query/sessions'),
    ('GET', '/query/sessions/s1'),
    ('DELETE', '/query/sessions'),
    ('DELETE', '/query/sessions/s1'),
    ('POST', '/query/feedback'),
    # Neptune
    ('GET', '/neptune/graph/summary/ont-1'),
    ('GET', '/neptune/graph/stats/ont-1'),
    ('GET', '/neptune/graph/classes/ont-1'),
    ('GET', '/neptune/graph/properties/ont-1'),
    # Lessons
    ('GET', '/lessons/ont-1'),
    ('DELETE', '/lessons/ont-1/records/r1'),
    # Feedback
    ('GET', '/feedback/ont-1'),
    ('DELETE', '/feedback/ont-1/f1'),
    # Documents
    ('POST', '/documents/ont-1/upload'),
    ('GET', '/documents/ont-1'),
    ('GET', '/documents/ont-1/doc-1'),
    ('DELETE', '/documents/ont-1/doc-1'),
    # Ground truth
    ('POST', '/groundtruth/ont-1/upload'),
    ('GET', '/groundtruth/ont-1'),
    ('DELETE', '/groundtruth/ont-1'),
    # Evaluations
    ('GET', '/evaluations/ont-1'),
    ('GET', '/evaluations/ont-1/run-1'),
    ('POST', '/evaluations/ont-1'),
    ('DELETE', '/evaluations/ont-1/run-1'),
    # Monitoring
    ('GET', '/monitoring/ont-1'),
    # Metadata
    ('POST', '/metadata/enrich'),
    ('POST', '/metadata/delta/ont-1'),
    ('GET', '/metadata/enrich/status/job-1'),
    ('POST', '/metadata/revise/ont-1/v2'),
    ('GET', '/metadata/table/db1/t1'),
    # Governed rules
    ('POST', '/rules/namespaces/ns-1/rules'),
    ('POST', '/rules/namespaces/ns-1/rules:import'),
    ('GET', '/rules/namespaces/ns-1/rules'),
    ('GET', '/rules/namespaces/ns-1/rules/r1'),
    ('PUT', '/rules/namespaces/ns-1/rules/r1'),
    ('POST', '/rules/namespaces/ns-1/rules/r1:approve'),
    ('POST', '/rules/namespaces/ns-1/rules/r1:validate'),
    ('POST', '/rules/namespaces/ns-1/rules/r1:publish'),
    ('DELETE', '/rules/namespaces/ns-1/rules/r1'),
    # Metrics
    ('POST', '/metrics/namespaces/ns-1/metrics'),
    ('GET', '/metrics/namespaces/ns-1/metrics'),
    ('GET', '/metrics/namespaces/ns-1/metrics/m1'),
    ('PUT', '/metrics/namespaces/ns-1/metrics/m1'),
    ('POST', '/metrics/namespaces/ns-1/metrics/m1:publish'),
    ('DELETE', '/metrics/namespaces/ns-1/metrics/m1'),
]


# ── Coverage ──────────────────────────────────────────────────────────


@pytest.mark.parametrize('method,path', LIVE_ROUTES)
def test_every_live_route_is_mapped(method: str, path: str) -> None:
    """No live route may fall through to the denyAll sentinel.

    A route reaching denyAll is a hard 403 for every principal including
    platform-admin, so an unmapped route breaks the feature rather than
    exposing it — but it still breaks it, so the map must be exhaustive.
    """
    match = match_route(method=method, path=path)
    assert match.action != Action.DENY_ALL.value, f'{method} {path} is unmapped'


def test_find_unmapped_reports_nothing_for_the_live_inventory() -> None:
    """The bulk helper agrees with the per-route assertions."""
    assert find_unmapped(requests=LIVE_ROUTES) == []


def test_trailing_slash_variants_are_mapped() -> None:
    """FastAPI redirects, but a proxied request can still arrive with a slash."""
    assert match_route(method='GET', path='/ontology/list/').action == Action.LIST.value


# ── Unmapped routes deny ──────────────────────────────────────────────


@pytest.mark.parametrize(
    'method,path',
    [
        ('GET', '/admin/secrets'),
        ('POST', '/ontology/config/ont-1/promote'),
        ('GET', '/rules/namespaces/ns-1/rules/r1/history'),
        ('POST', '/namespaces/ns-1/dangerous'),
        ('GET', '/'.join(['', 'query', 'sessions', 's1', 'extra'])),
    ],
)
def test_unknown_routes_map_to_deny_all(method: str, path: str) -> None:
    """A route this build does not know about is denied, not guessed at."""
    assert match_route(method=method, path=path).action == Action.DENY_ALL.value


def test_wrong_method_on_a_known_path_denies() -> None:
    """Method is part of the match — a DELETE on a read-only path is not a read."""
    assert (
        match_route(method='DELETE', path='/ontology/list').action
        == Action.DENY_ALL.value
    )


# ── Bypass paths ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    'path',
    [
        '/',
        '/health',
        '/status',
        '/openapi.json',
        '/docs',
        '/ontology/health',
        '/query/health',
        '/documents/health',
        '/metadata/health',
    ],
)
def test_infrastructure_paths_bypass(path: str) -> None:
    """Health and OpenAPI paths expose no tenant data and carry no token."""
    assert is_bypass_path(path=path) is True


@pytest.mark.parametrize(
    'path',
    [
        '/ontology/list',
        '/namespaces',
        '/documents/ont-1',
        '/health/secrets',
        '/query/sessions',
    ],
)
def test_data_paths_do_not_bypass(path: str) -> None:
    """The bypass patterns must stay narrow — a wide one is an open endpoint."""
    assert is_bypass_path(path=path) is False


# ── Action assignment ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    'method,path,expected',
    [
        # Separation of duties: approve/publish are distinct from authoring.
        ('POST', '/rules/namespaces/ns-1/rules/r1:approve', Action.APPROVE_RULE),
        ('POST', '/rules/namespaces/ns-1/rules/r1:publish', Action.PUBLISH_RULE),
        ('POST', '/rules/namespaces/ns-1/rules/r1:validate', Action.MANAGE_RULE),
        ('POST', '/rules/namespaces/ns-1/rules', Action.MANAGE_RULE),
        ('GET', '/rules/namespaces/ns-1/rules/r1', Action.READ_RULE),
        ('DELETE', '/rules/namespaces/ns-1/rules/r1', Action.MANAGE_RULE),
        ('POST', '/metrics/namespaces/ns-1/metrics/m1:publish', Action.PUBLISH_METRIC),
        ('GET', '/metrics/namespaces/ns-1/metrics', Action.READ_METRIC),
        ('PUT', '/metrics/namespaces/ns-1/metrics/m1', Action.MANAGE_METRIC),
        # Reads vs writes elsewhere.
        ('GET', '/ontology/config/ont-1', Action.VIEW_ONTOLOGY),
        ('DELETE', '/ontology/config/ont-1', Action.MANAGE_ONTOLOGY),
        ('POST', '/ontology/build/ont-1', Action.MANAGE_ONTOLOGY),
        ('GET', '/neptune/graph/summary/ont-1', Action.TRAVERSE_GRAPH),
        ('GET', '/documents/ont-1/doc-1', Action.SEARCH_DOCUMENTS),
        ('DELETE', '/documents/ont-1/doc-1', Action.MANAGE_DOCUMENTS),
        ('POST', '/groundtruth/ont-1/upload', Action.MANAGE_GROUND_TRUTH),
        ('GET', '/groundtruth/ont-1', Action.VIEW_EVALUATION),
        ('DELETE', '/groundtruth/ont-1', Action.MANAGE_GROUND_TRUTH),
        ('POST', '/evaluations/ont-1', Action.MANAGE_EVALUATION),
        ('GET', '/monitoring/ont-1', Action.VIEW_MONITORING),
        ('DELETE', '/lessons/ont-1/records/r1', Action.MANAGE_LESSONS),
        ('GET', '/query/suggestions/ont-1', Action.QUERY),
        # Namespace lifecycle.
        ('POST', '/namespaces', Action.MANAGE_NAMESPACE),
        ('GET', '/namespaces', Action.LIST),
        ('POST', '/namespaces/ns-1/status', Action.SET_NAMESPACE_STATUS),
        ('DELETE', '/namespaces/ns-1', Action.DELETE_NAMESPACE),
        ('POST', '/namespaces/ns-1/grants', Action.GRANT_PERMISSIONS),
        ('GET', '/namespaces/ns-1/grants', Action.VIEW_NAMESPACE),
        # Catalog browsing is gated, not open.
        ('GET', '/datasource/glue/databases', Action.LIST),
        ('POST', '/datasource/extract-metadata', Action.MANAGE_DATA_SOURCE),
    ],
)
def test_action_assignment(method: str, path: str, expected: Action) -> None:
    """Each route requires the action the access matrix says it should."""
    assert match_route(method=method, path=path).action == expected.value


def test_approve_is_not_confused_with_a_plain_read() -> None:
    """Ordering guard: the `:approve` row must precede the generic rule row.

    If the generic `GET .../rules/{id}` row were reachable first for a POST, the
    approval gate would collapse back into `manageRule`.
    """
    patterns = mapped_patterns()
    approve = next(i for i, p in enumerate(patterns) if ':approve' in p)
    generic = next(
        i
        for i, p in enumerate(patterns)
        if p.endswith('/rules/[^/]+/?$') and 'namespaces' in p
    )
    assert approve < generic


# ── Namespace source ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    'method,path,expected_ns',
    [
        ('GET', '/namespaces/ns-7', 'ns-7'),
        ('POST', '/namespaces/ns-7/grants', 'ns-7'),
        ('POST', '/namespaces/ns-7/status', 'ns-7'),
    ],
)
def test_namespace_read_from_the_path(
    method: str, path: str, expected_ns: str
) -> None:
    """A namespace-scoped route resolves without any lookup.

    Only the ``/namespaces/{id}`` family qualifies: it is the one place where the
    path segment really holds a namespace id.
    """
    match = match_route(method=method, path=path)
    assert match.namespace_source is NamespaceSource.PATH
    assert match.namespace == expected_ns
    assert resolve_namespace(match=match, ontology_namespace=None) == expected_ns


@pytest.mark.parametrize(
    'method,path,expected_ontology',
    [
        ('GET', '/ontology/config/ont-9', 'ont-9'),
        ('GET', '/documents/ont-9/doc-1', 'ont-9'),
        ('GET', '/neptune/graph/stats/ont-9', 'ont-9'),
        ('POST', '/metadata/revise/ont-9/v3', 'ont-9'),
    ],
)
def test_ontology_id_captured_for_indirect_routes(
    method: str, path: str, expected_ontology: str
) -> None:
    """The ontology id is captured so the middleware can resolve its namespace."""
    match = match_route(method=method, path=path)
    assert match.namespace_source is NamespaceSource.ONTOLOGY
    assert match.ontology_id == expected_ontology


@pytest.mark.parametrize(
    'method,path',
    [
        ('GET', '/rules/namespaces/lay-7/rules'),
        ('GET', '/rules/namespaces/lay-7/rules/r1'),
        ('POST', '/rules/namespaces/lay-7/rules'),
        ('POST', '/rules/namespaces/lay-7/rules:import'),
        ('PUT', '/rules/namespaces/lay-7/rules/r1'),
        ('DELETE', '/rules/namespaces/lay-7/rules/r1'),
        ('POST', '/rules/namespaces/lay-7/rules/r1:approve'),
        ('POST', '/rules/namespaces/lay-7/rules/r1:publish'),
        ('POST', '/rules/namespaces/lay-7/rules/r1:validate'),
        ('GET', '/metrics/namespaces/lay-7/metrics'),
        ('GET', '/metrics/namespaces/lay-7/metrics/m1'),
        ('POST', '/metrics/namespaces/lay-7/metrics'),
        ('PUT', '/metrics/namespaces/lay-7/metrics/m1'),
        ('DELETE', '/metrics/namespaces/lay-7/metrics/m1'),
        ('POST', '/metrics/namespaces/lay-7/metrics/m1:publish'),
    ],
)
def test_rules_and_metrics_resolve_via_the_owning_layer(
    method: str, path: str
) -> None:
    """Regression: the ``/namespaces/{…}`` segment here holds a LAYER id.

    Declaring these routes PATH sent the layer id to the namespace registry,
    which holds no row for it, so the middleware denied every request with
    ``reason=unknown_namespace`` — the Governed Metrics and Governed Rules tabs
    403'd for every caller including platform-admin. The id must be captured as
    an ontology id so the owning namespace is looked up instead.
    """
    match = match_route(method=method, path=path)
    assert match.namespace_source is NamespaceSource.ONTOLOGY
    assert match.ontology_id == 'lay-7'
    # The layer id must NOT leak through as the decision resource.
    assert resolve_namespace(match=match, ontology_namespace='ns-3') == 'ns-3'


def test_path_sourced_routes_are_only_the_namespaces_family() -> None:
    """``PATH`` is legitimate only where the segment really holds a namespace id.

    Every other per-layer surface must be ``ONTOLOGY``. Pinning the invariant
    here is what stops the metrics/rules bug from being reintroduced on a new
    route: a ``/{thing}/namespaces/{x}/…`` path is almost always carrying a layer
    id in ``x``, and declaring it ``PATH`` sends that id to the namespace
    registry, which denies it as unknown.
    """
    offenders = [
        pattern
        for pattern, source in path_sources()
        if source is NamespaceSource.PATH
        and not pattern.startswith('^/namespaces/')
    ]
    assert offenders == []


def test_ontology_route_resolves_to_the_owning_namespace() -> None:
    """The resolved namespace replaces the PLATFORM default."""
    match = match_route(method='GET', path='/ontology/config/ont-9')
    assert resolve_namespace(match=match, ontology_namespace='ns-3') == 'ns-3'


@pytest.mark.parametrize('unbound', [None, ''])
def test_unbound_ontology_raises_rather_than_falling_back(unbound: object) -> None:
    """Falling back to PLATFORM would be wrong in both directions.

    A namespace-owner would be denied (their role is scoped to a namespace) while
    a platform-viewer would pass the platform read policies — so the fallback
    both breaks legitimate access and widens illegitimate access.
    """
    match = match_route(method='GET', path='/ontology/config/ont-9')
    with pytest.raises(ValueError, match='not bound to a namespace'):
        resolve_namespace(match=match, ontology_namespace=unbound)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    'method,path',
    [
        ('GET', '/ontology/list'),
        ('POST', '/ontology/config'),
        ('GET', '/datasource/glue/databases'),
        ('POST', '/namespaces'),
        ('GET', '/query/sessions'),
        ('POST', '/metadata/enrich'),
        ('GET', '/metadata/enrich/status/job-1'),
    ],
)
def test_platform_scoped_routes(method: str, path: str) -> None:
    """Routes with no namespace in the request evaluate against the sentinel."""
    match = match_route(method=method, path=path)
    assert match.namespace_source is NamespaceSource.PLATFORM
    assert match.namespace == PLATFORM_RESOURCE_ID


def test_deny_all_match_is_platform_scoped() -> None:
    """An unmapped route must not carry a namespace guess into the decision."""
    match: RouteMatch = match_route(method='GET', path='/nope')
    assert match.namespace == PLATFORM_RESOURCE_ID
    assert match.ontology_id == ''
