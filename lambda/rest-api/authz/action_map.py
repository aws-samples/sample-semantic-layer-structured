"""Map an HTTP request to a Cedar action and the namespace it targets.

This is the structural half of the PDP. COA gets the same property from an API
Gateway REQUEST authorizer that sits in front of every route; SL cannot use one
(its HTTP API has a single ``/{proxy+}`` ``ANY`` route, deliberately, to stay
under Lambda's 20 KB resource-policy limit), so the equivalent guarantee is
enforced here instead: **every** request is matched against this table, and a
request that matches nothing maps to the ``denyAll`` sentinel, which
``default.cedar`` forbids outright.

That is why the table is exhaustive and ordered rather than convention-driven. A
new route added without a table entry is denied, not silently exposed — the
failure mode of a missing mapping is a 403 in testing, not an open endpoint in
production.

Three ways the target namespace is determined, per route:

``NamespaceSource.PLATFORM``
    No namespace in the request (``POST /namespaces``, ``/ontology/list``,
    ``/datasource/glue/*``). Evaluated against the ``PLATFORM`` sentinel.
``NamespaceSource.PATH``
    A genuine namespace id or name appears in the path — only the
    ``/namespaces/{id}`` family. Read from the ``ns`` capture group.
``NamespaceSource.ONTOLOGY``
    An ontology (semantic-layer) id appears in the path; the namespace is
    whichever namespace owns that ontology. Read from the ``ontology_id``
    capture group and resolved by the caller (the middleware) — this module
    stays free of I/O so it can be unit-tested against the route list alone.
    This covers the per-layer surfaces, INCLUDING ``/rules`` and ``/metrics``
    despite their ``/namespaces/{…}`` URL shape (see the rules section below).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import List, Optional, Pattern, Tuple

from authz.cedar_schema import PLATFORM_RESOURCE_ID, Action


class NamespaceSource(StrEnum):
    """Where the namespace for a decision comes from.

    Members:
        PLATFORM: No namespace — evaluate against the PLATFORM sentinel.
        PATH: A path segment names the namespace itself (capture group ``ns``).
        ONTOLOGY: A path segment names an ontology (capture group
            ``ontology_id``) whose owning namespace must be looked up.

    Choose by what the segment actually CONTAINS, not by what the URL calls it:
    ``/rules/namespaces/{x}/rules`` says "namespaces" but ``x`` is a layer id,
    so those routes are ONTOLOGY.
    """

    PLATFORM = 'platform'
    PATH = 'path'
    ONTOLOGY = 'ontology'


@dataclass(frozen=True)
class RouteMatch:
    """The authorization coordinates of one matched request.

    Attributes:
        action: Bare Cedar action name.
        namespace_source: How to obtain the target namespace.
        namespace: The namespace id/name captured from the path, or the
            ``PLATFORM`` sentinel. For ``ONTOLOGY`` routes this is ``''`` and
            ``ontology_id`` carries the value to resolve.
        ontology_id: The captured ontology id, or ``''``.
        matched_pattern: The regex that matched, for audit logging.
    """

    action: str
    namespace_source: NamespaceSource
    namespace: str = PLATFORM_RESOURCE_ID
    ontology_id: str = ''
    matched_pattern: str = ''


@dataclass(frozen=True)
class _Route:
    """One row of the route table.

    Attributes:
        methods: HTTP methods this row covers. Empty means any method.
        pattern: Compiled full-path regex, anchored at both ends.
        action: Bare Cedar action name.
        namespace_source: How to obtain the target namespace.
    """

    methods: Tuple[str, ...]
    pattern: Pattern[str]
    action: str
    namespace_source: NamespaceSource


# Path segments that are opaque identifiers. Kept narrow on purpose: a greedy
# ``.+`` would let one route's pattern swallow another's, and the first match
# wins.
_ID = r'[^/]+'
_TAIL = r'.+'


def _route(
    *,
    methods: Tuple[str, ...],
    path: str,
    action: Action,
    namespace_source: NamespaceSource,
) -> _Route:
    """Compile one route-table row.

    Args:
        methods: HTTP methods, uppercase. Empty tuple matches any method.
        path: Full-path regex fragment, without anchors.
        action: The Cedar action this route requires.
        namespace_source: How to obtain the target namespace.

    Returns:
        The compiled :class:`_Route`.
    """
    return _Route(
        methods=methods,
        pattern=re.compile(f'^{path}/?$'),
        action=action.value,
        namespace_source=namespace_source,
    )


# ── Unauthenticated / infrastructure paths ────────────────────────────
#
# These bypass authorization entirely. Kept to health, readiness and the OpenAPI
# surface: nothing here reads tenant data. ``/*/health`` covers the per-sub-app
# health endpoints (``/ontology/health``, ``/query/health``, …), which the ALB
# and the CDK smoke tests call without a token.
_BYPASS_PATTERNS: Tuple[Pattern[str], ...] = (
    re.compile(r'^/?$'),
    re.compile(r'^/health/?$'),
    re.compile(r'^/status/?$'),
    re.compile(r'^/openapi\.json$'),
    re.compile(r'^/docs(/.*)?$'),
    re.compile(r'^/redoc/?$'),
    re.compile(r'^/[^/]+/health/?$'),
)


# The route table. ORDER MATTERS — the first row whose method and pattern match
# wins, so a more specific path must precede a more general one that would also
# match it (e.g. `/rules/namespaces/{ns}/rules/{id}:approve` before
# `/rules/namespaces/{ns}/rules/{id}`).
_ROUTES: Tuple[_Route, ...] = (
    # ── Namespaces ────────────────────────────────────────────────────
    _route(
        methods=('POST',),
        path='/namespaces',
        action=Action.MANAGE_NAMESPACE,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    _route(
        methods=('GET',),
        path='/namespaces',
        action=Action.LIST,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    # "What are MY roles" — the frontend's source for hiding admin surfaces.
    # MUST precede the `/namespaces/{ns}` rows, which would otherwise capture
    # `me` as a namespace id and demand viewNamespace on a namespace that does
    # not exist. Carries LIST, the same action as enumerating namespaces: reading
    # back one's own already-resolved roles reveals nothing a caller does not
    # already hold, and gating it behind a role would make it useless to exactly
    # the callers who need to discover they have none.
    _route(
        methods=('GET',),
        path='/namespaces/me',
        action=Action.LIST,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    _route(
        methods=('GET',),
        path=f'/namespaces/(?P<ns>{_ID})/grants',
        action=Action.VIEW_NAMESPACE,
        namespace_source=NamespaceSource.PATH,
    ),
    _route(
        methods=('POST',),
        path=f'/namespaces/(?P<ns>{_ID})/grants',
        action=Action.GRANT_PERMISSIONS,
        namespace_source=NamespaceSource.PATH,
    ),
    _route(
        methods=('DELETE',),
        path=f'/namespaces/(?P<ns>{_ID})/grants/{_TAIL}',
        action=Action.GRANT_PERMISSIONS,
        namespace_source=NamespaceSource.PATH,
    ),
    _route(
        methods=('POST', 'PUT'),
        path=f'/namespaces/(?P<ns>{_ID})/status',
        action=Action.SET_NAMESPACE_STATUS,
        namespace_source=NamespaceSource.PATH,
    ),
    _route(
        methods=('GET',),
        path=f'/namespaces/(?P<ns>{_ID})',
        action=Action.VIEW_NAMESPACE,
        namespace_source=NamespaceSource.PATH,
    ),
    _route(
        methods=('PATCH', 'PUT'),
        path=f'/namespaces/(?P<ns>{_ID})',
        action=Action.MANAGE_NAMESPACE,
        namespace_source=NamespaceSource.PATH,
    ),
    _route(
        methods=('DELETE',),
        path=f'/namespaces/(?P<ns>{_ID})',
        action=Action.DELETE_NAMESPACE,
        namespace_source=NamespaceSource.PATH,
    ),
    # ── Governed rules (router mounted at /rules) ─────────────────────
    #
    # ⚠️ The ``/namespaces/{…}`` segment in these URLs does NOT hold a namespace
    # id — it holds a SEMANTIC LAYER (ontology) id, so every row below is
    # ONTOLOGY-sourced, not PATH-sourced. The URL is a misnomer inherited from
    # the storage layer, where "namespace" has always meant the layer: metrics
    # are keyed ``pk = NS#{layerId}`` and ``rule_service.list`` documents its
    # ``namespace`` parameter as "Ontology id owning the rules graph". The
    # frontend matches that convention — GovernedMetricsTab/GovernedRulesTab
    # both pass the layer id, because the query agents resolve their Tier 1
    # namespace as ``config.namespace || id`` and the config has no namespace
    # field.
    #
    # Declaring these PATH sent the layer id to the namespace registry, which
    # has no row for it, so the middleware denied every request with
    # ``reason=unknown_namespace`` — both governance tabs 403'd for every
    # caller, platform-admin included. Resolving layer → owning namespace via
    # the ``namespaceId`` already stored on each layer's metadata config is
    # what the other 30 per-layer routes do, and it makes the authorization
    # boundary the real namespace the layer lives in.
    #
    # Storage keying is unaffected: the routers read ``ns`` from the FastAPI
    # path parameter directly and never consult the authz scope, so metrics and
    # rules stay partitioned per layer while the DECISION is namespace-scoped.
    #
    # The approve / publish rows come FIRST and carry their own actions. These
    # are the two endpoints docs/comparison-coa.md §3.2 calls out as reachable
    # by any authenticated caller: the approval gate on a governed rule was
    # ungated, which made the gate decorative.
    _route(
        methods=('POST',),
        path=f'/rules/namespaces/(?P<ontology_id>{_ID})/rules/{_ID}:approve',
        action=Action.APPROVE_RULE,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('POST',),
        path=f'/rules/namespaces/(?P<ontology_id>{_ID})/rules/{_ID}:publish',
        action=Action.PUBLISH_RULE,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    # `:validate` executes a real Athena probe against namespace data, so it is
    # an authoring action rather than a read.
    _route(
        methods=('POST',),
        path=f'/rules/namespaces/(?P<ontology_id>{_ID})/rules/{_ID}:validate',
        action=Action.MANAGE_RULE,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('POST',),
        path=f'/rules/namespaces/(?P<ontology_id>{_ID})/rules:import',
        action=Action.MANAGE_RULE,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('POST',),
        path=f'/rules/namespaces/(?P<ontology_id>{_ID})/rules',
        action=Action.MANAGE_RULE,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/rules/namespaces/(?P<ontology_id>{_ID})/rules',
        action=Action.READ_RULE,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/rules/namespaces/(?P<ontology_id>{_ID})/rules/{_ID}',
        action=Action.READ_RULE,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('PUT', 'PATCH', 'DELETE'),
        path=f'/rules/namespaces/(?P<ontology_id>{_ID})/rules/{_ID}',
        action=Action.MANAGE_RULE,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    # ── Metrics (router mounted at /metrics) ──────────────────────────
    #
    # ONTOLOGY-sourced for the same reason as the /rules rows above.
    _route(
        methods=('POST',),
        path=f'/metrics/namespaces/(?P<ontology_id>{_ID})/metrics/{_ID}:publish',
        action=Action.PUBLISH_METRIC,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('POST',),
        path=f'/metrics/namespaces/(?P<ontology_id>{_ID})/metrics',
        action=Action.MANAGE_METRIC,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/metrics/namespaces/(?P<ontology_id>{_ID})/metrics',
        action=Action.READ_METRIC,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/metrics/namespaces/(?P<ontology_id>{_ID})/metrics/{_ID}',
        action=Action.READ_METRIC,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('PUT', 'PATCH', 'DELETE'),
        path=f'/metrics/namespaces/(?P<ontology_id>{_ID})/metrics/{_ID}',
        action=Action.MANAGE_METRIC,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    # ── Ontology lifecycle (/ontology) ────────────────────────────────
    #
    # `POST /ontology/config` and `POST /ontology/upload` create something that
    # does not exist yet, so no namespace can be read from the path. They are
    # PLATFORM-scoped: creating a semantic layer is a platform-admin act. A
    # namespace-scoped create belongs behind a future
    # `/namespaces/{ns}/ontologies` route, where the namespace IS in the path.
    _route(
        methods=('POST',),
        path='/ontology/config',
        action=Action.MANAGE_ONTOLOGY,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    _route(
        methods=('POST',),
        path='/ontology/upload',
        action=Action.MANAGE_ONTOLOGY,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    _route(
        methods=('GET',),
        path='/ontology/list',
        action=Action.LIST,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    _route(
        methods=('GET',),
        path=f'/ontology/config/(?P<ontology_id>{_ID})',
        action=Action.VIEW_ONTOLOGY,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('DELETE',),
        path=f'/ontology/config/(?P<ontology_id>{_ID})',
        action=Action.MANAGE_ONTOLOGY,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/ontology/build-status/(?P<ontology_id>{_ID})',
        action=Action.VIEW_ONTOLOGY,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/ontology/versions/(?P<ontology_id>{_ID})',
        action=Action.VIEW_ONTOLOGY,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/ontology/content/(?P<ontology_id>{_ID})/{_ID}',
        action=Action.VIEW_ONTOLOGY,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('POST',),
        path=f'/ontology/build/(?P<ontology_id>{_ID})',
        action=Action.MANAGE_ONTOLOGY,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('POST',),
        path=f'/ontology/delta/(?P<ontology_id>{_ID})',
        action=Action.MANAGE_ONTOLOGY,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('POST',),
        path=f'/ontology/revise/(?P<ontology_id>{_ID})/{_ID}',
        action=Action.MANAGE_ONTOLOGY,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    # ── Data sources (/datasource) ────────────────────────────────────
    #
    # Glue browsing is not namespace-scoped: it enumerates the account's
    # catalog, which is exactly why it should not be open to every signed-in
    # user. `list` gates it at platform-viewer and above.
    _route(
        methods=('GET',),
        path='/datasource/glue/databases',
        action=Action.LIST,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    _route(
        methods=('GET',),
        path=f'/datasource/glue/tables/{_ID}',
        action=Action.LIST,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    _route(
        methods=('GET',),
        path=f'/datasource/glue/metadata/{_ID}/{_ID}',
        action=Action.LIST,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    _route(
        methods=('POST',),
        path='/datasource/extract-metadata',
        action=Action.MANAGE_DATA_SOURCE,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    # ── Query + chat (/query) ─────────────────────────────────────────
    #
    # Session routes are PLATFORM-scoped because a session is owned by the
    # caller, not by a namespace, and the handlers already filter by user id.
    _route(
        methods=('GET',),
        path=f'/query/suggestions/(?P<ontology_id>{_ID})',
        action=Action.QUERY,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET', 'DELETE'),
        path='/query/sessions',
        action=Action.QUERY,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    _route(
        methods=('GET', 'DELETE'),
        path=f'/query/sessions/{_ID}',
        action=Action.QUERY,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    _route(
        methods=('POST',),
        path='/query/feedback',
        action=Action.MANAGE_FEEDBACK,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    # ── Graph traversal (/neptune) ────────────────────────────────────
    _route(
        methods=('GET',),
        path=f'/neptune/graph/{_ID}/(?P<ontology_id>{_ID})',
        action=Action.TRAVERSE_GRAPH,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    # ── Lessons (/lessons) ────────────────────────────────────────────
    _route(
        methods=('DELETE',),
        path=f'/lessons/(?P<ontology_id>{_ID})/{_TAIL}',
        action=Action.MANAGE_LESSONS,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/lessons/(?P<ontology_id>{_ID})',
        action=Action.VIEW_MONITORING,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    # ── Feedback (/feedback) ──────────────────────────────────────────
    _route(
        methods=('DELETE',),
        path=f'/feedback/(?P<ontology_id>{_ID})/{_ID}',
        action=Action.MANAGE_FEEDBACK,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/feedback/(?P<ontology_id>{_ID})',
        action=Action.VIEW_MONITORING,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    # ── Documents (/documents) ────────────────────────────────────────
    _route(
        methods=('POST',),
        path=f'/documents/(?P<ontology_id>{_ID})/upload',
        action=Action.MANAGE_DOCUMENTS,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('DELETE',),
        path=f'/documents/(?P<ontology_id>{_ID})/{_ID}',
        action=Action.MANAGE_DOCUMENTS,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/documents/(?P<ontology_id>{_ID})/{_ID}',
        action=Action.SEARCH_DOCUMENTS,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/documents/(?P<ontology_id>{_ID})',
        action=Action.SEARCH_DOCUMENTS,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    # ── Ground truth (/groundtruth) ───────────────────────────────────
    _route(
        methods=('POST',),
        path=f'/groundtruth/(?P<ontology_id>{_ID})/upload',
        action=Action.MANAGE_GROUND_TRUTH,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/groundtruth/(?P<ontology_id>{_ID})',
        action=Action.VIEW_EVALUATION,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('DELETE',),
        path=f'/groundtruth/(?P<ontology_id>{_ID})',
        action=Action.MANAGE_GROUND_TRUTH,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    # ── Evaluations (/evaluations) ────────────────────────────────────
    _route(
        methods=('POST',),
        path=f'/evaluations/(?P<ontology_id>{_ID})',
        action=Action.MANAGE_EVALUATION,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/evaluations/(?P<ontology_id>{_ID})',
        action=Action.VIEW_EVALUATION,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/evaluations/(?P<ontology_id>{_ID})/{_ID}',
        action=Action.VIEW_EVALUATION,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('DELETE',),
        path=f'/evaluations/(?P<ontology_id>{_ID})/{_ID}',
        action=Action.MANAGE_EVALUATION,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    # ── Monitoring (/monitoring) ──────────────────────────────────────
    _route(
        methods=('GET',),
        path=f'/monitoring/(?P<ontology_id>{_ID})',
        action=Action.VIEW_MONITORING,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    # ── Metadata enrichment (/metadata) ───────────────────────────────
    #
    # `/metadata/enrich` and the job-status poll carry no ontology in the path
    # (the job id is not an ontology id), so both are PLATFORM-scoped.
    _route(
        methods=('POST',),
        path='/metadata/enrich',
        action=Action.MANAGE_ONTOLOGY,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    _route(
        methods=('GET',),
        path=f'/metadata/enrich/status/{_ID}',
        action=Action.VIEW_ONTOLOGY,
        namespace_source=NamespaceSource.PLATFORM,
    ),
    _route(
        methods=('POST',),
        path=f'/metadata/delta/(?P<ontology_id>{_ID})',
        action=Action.MANAGE_ONTOLOGY,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('POST',),
        path=f'/metadata/revise/(?P<ontology_id>{_ID})/{_ID}',
        action=Action.MANAGE_ONTOLOGY,
        namespace_source=NamespaceSource.ONTOLOGY,
    ),
    _route(
        methods=('GET',),
        path=f'/metadata/table/{_ID}/{_ID}',
        action=Action.LIST,
        namespace_source=NamespaceSource.PLATFORM,
    ),
)


def is_bypass_path(*, path: str) -> bool:
    """Report whether a path skips authorization entirely.

    Args:
        path: The request path, e.g. ``/ontology/health``.

    Returns:
        True for health/readiness/OpenAPI paths, which expose no tenant data.
    """
    return any(pattern.match(path) for pattern in _BYPASS_PATTERNS)


def match_route(*, method: str, path: str) -> RouteMatch:
    """Resolve one request to its Cedar action and target namespace.

    Args:
        method: HTTP method, any case.
        path: The full request path as seen by the root app, including the mount
            prefix (``/rules/namespaces/ns-1/rules``).

    Returns:
        The matching :class:`RouteMatch`, or one carrying the ``denyAll``
        sentinel action when no row matches. ``denyAll`` is forbidden by
        ``default.cedar``, so an unmapped route is denied for every principal
        including platform-admin.
    """
    upper_method = method.upper()
    for route in _ROUTES:
        if route.methods and upper_method not in route.methods:
            continue
        match = route.pattern.match(path)
        if match is None:
            continue
        groups = match.groupdict()
        return RouteMatch(
            action=route.action,
            namespace_source=route.namespace_source,
            namespace=groups.get('ns') or PLATFORM_RESOURCE_ID,
            ontology_id=groups.get('ontology_id') or '',
            matched_pattern=route.pattern.pattern,
        )
    return RouteMatch(
        action=Action.DENY_ALL.value,
        namespace_source=NamespaceSource.PLATFORM,
        namespace=PLATFORM_RESOURCE_ID,
    )


def mapped_patterns() -> List[str]:
    """List every route pattern in the table.

    Used by the unit tests to assert the table has not drifted from the live
    route inventory.

    Returns:
        The regex source of each row, in table order.
    """
    return [route.pattern.pattern for route in _ROUTES]


def path_sources() -> List[Tuple[str, NamespaceSource]]:
    """Pair every route pattern with how its namespace is determined.

    Used by the unit tests to assert the structural invariant that
    ``NamespaceSource.PATH`` appears only on the ``/namespaces/{id}`` family —
    the one place a path segment genuinely holds a namespace identifier.

    Returns:
        ``(pattern source, namespace source)`` for each row, in table order.
    """
    return [(route.pattern.pattern, route.namespace_source) for route in _ROUTES]


def find_unmapped(*, requests: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Return the ``(method, path)`` pairs that no table row covers.

    Args:
        requests: Candidate requests to probe.

    Returns:
        The subset that maps to ``denyAll`` and is not a bypass path.
    """
    unmapped: List[Tuple[str, str]] = []
    for method, path in requests:
        if is_bypass_path(path=path):
            continue
        if match_route(method=method, path=path).action == Action.DENY_ALL.value:
            unmapped.append((method, path))
    return unmapped


def resolve_namespace(
    *, match: RouteMatch, ontology_namespace: Optional[str]
) -> str:
    """Produce the final resource id for a decision.

    Args:
        match: The matched route.
        ontology_namespace: For ``ONTOLOGY`` routes, the namespace that owns
            ``match.ontology_id``; ignored otherwise. ``None`` means the binding
            could not be resolved.

    Returns:
        The namespace id, or the ``PLATFORM`` sentinel.

    Raises:
        ValueError: If an ``ONTOLOGY`` route has no resolved namespace. Falling
            back to PLATFORM here would evaluate a namespace-scoped request
            against the platform resource, so a namespace-owner would be denied
            while a platform-viewer would slip through the read policies —
            wrong in both directions. The middleware turns this into a 403.
    """
    if match.namespace_source is not NamespaceSource.ONTOLOGY:
        return match.namespace
    if not ontology_namespace:
        raise ValueError(
            f'ontology {match.ontology_id!r} is not bound to a namespace'
        )
    return ontology_namespace
