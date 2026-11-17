"""Query-time Neptune structural-graph fetch for Hybrid SQL verification.

The Hybrid query agent verifies generated SQL against the ontology. The
RAG-style recalled slice is markdown-derived JSON that carries NO
``rdfs:domain``/``rdfs:range``/``rdfs:subClassOf`` and NO
``vkg:mapsToColumn``/``vkg:mapsToTable`` — those structural axioms and
virtual-knowledge-graph mappings live ONLY in Neptune. So the verify step needs
a query-time fetch that pulls the thin structural graph and returns it as an
``rdflib.Graph``.

This module provides :class:`StructuralGraphFetcher`, which caches the fetched
graph per ``(namespace, candidates)`` and FAILS LOUD on any gateway error —
it never silently degrades to a single-engine (RAG-only) fallback, because a
missing structural graph must surface as a hard error rather than a silently
weaker verification.

Isolation note: this module depends on stdlib + ``rdflib`` only. The AWS/gateway
CONSTRUCT call is injected through the ``construct_ttl`` seam so this class stays
unit-testable and self-contained (no import of ``agents.shared`` or any sibling
agent package).
"""
from __future__ import annotations

from typing import Callable, Dict, FrozenSet, List, Tuple

from rdflib import Graph

# Type alias for the injected CONSTRUCT seam: given a namespace and a list of
# candidate IRIs, it returns the structural slice as Turtle text. The real seam
# (a thin copy of the gateway CONSTRUCT call over M2M OAuth) is wired at runtime.
ConstructTtl = Callable[..., str]


class StructuralGraphFetcher:
    """Fetches and caches the Neptune structural graph for SQL verification."""

    def __init__(self, *, construct_ttl: ConstructTtl) -> None:
        """Initialize the fetcher with an injected CONSTRUCT seam.

        Args:
            construct_ttl: Callable invoked as
                ``construct_ttl(namespace=<str>, candidates=<List[str]>)`` that
                returns the structural slice as Turtle text. This seam isolates
                the AWS/gateway call so the class is unit-testable.

        Returns:
            None.
        """
        self._construct_ttl: ConstructTtl = construct_ttl
        # Cache maps (namespace, frozenset(candidates)) -> the parsed Graph.
        # Storing the Graph object itself (not the Turtle) lets us return the
        # SAME object on a hit — callers rely on identity to skip re-parsing.
        self._cache: Dict[Tuple[str, FrozenSet[str]], Graph] = {}

    def get(self, *, namespace: str, candidates: List[str]) -> Graph:
        """Return the structural graph for the given namespace and candidates.

        On a cache miss this calls the ``construct_ttl`` seam, parses the
        returned Turtle into an ``rdflib.Graph``, stores it, and returns it. On
        a cache hit it returns the SAME cached ``Graph`` object (by identity).

        Fail loud: exceptions raised by ``construct_ttl`` are NOT caught — they
        propagate to the caller so a gateway failure surfaces as a hard error
        rather than a silent single-engine fallback. Failures are never cached.

        Args:
            namespace: The ontology namespace to fetch the structural slice for.
            candidates: The candidate IRIs whose structural neighbourhood the
                slice must cover. Order does not affect the cache key.

        Returns:
            Graph: The parsed ``rdflib.Graph`` holding the structural slice.
        """
        # frozenset makes the key order-independent and hashable; two calls with
        # the same candidates in a different order share one cache entry.
        key: Tuple[str, FrozenSet[str]] = (namespace, frozenset(candidates))
        cached: Graph | None = self._cache.get(key)
        if cached is not None:
            return cached

        # Miss: invoke the seam. Any exception here propagates (fail loud) and
        # nothing is cached, so a later retry can succeed once the gateway is up.
        ttl: str = self._construct_ttl(namespace=namespace, candidates=candidates)

        graph: Graph = Graph()
        graph.parse(data=ttl, format="turtle")
        self._cache[key] = graph
        return graph
