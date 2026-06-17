"""Helper that runs a SPARQL CONSTRUCT against Neptune for Phase 2 slice build.

Pulls all triples within ``n_hops`` of any candidate IRI plus the
``rdfs:subClassOf`` chain to the root for each candidate. Returned as an
``rdflib.Graph`` so the slice builder can serialize, count tokens, and
truncate by centrality.
"""
from __future__ import annotations

import logging
from typing import Callable, List

from rdflib import Graph

logger = logging.getLogger(__name__)


class NeptuneConstruct:
    """Phase 2 collaborator wrapping a SPARQL CONSTRUCT endpoint."""

    def __init__(self, *, execute_sparql: Callable[[str], str],
                 graph_uri_prefix: str, graph_uri: str = "") -> None:
        """Initialize the helper.

        Args:
            execute_sparql: Callable that runs a CONSTRUCT and returns the
                response body as a Turtle string.
            graph_uri_prefix: Fallback prefix used to derive the named-graph URI
                from ``namespace`` when ``graph_uri`` is not supplied.
            graph_uri: The explicit Neptune named-graph URI (preferred). The
                ontology is published under
                ``http://{name}/ontology/{ontology_id}`` — the gateway's
                ``get_ontology_from_neptune`` returns this in ``graph_uri``, so
                the caller passes it directly rather than re-deriving it from a
                prefix + namespace (which never matched the publish-time URI).
        """
        self.execute_sparql = execute_sparql
        self.graph_uri_prefix = graph_uri_prefix
        self.graph_uri = graph_uri

    def construct(self, *, candidates: List[str], n_hops: int,
                  namespace: str) -> Graph:
        """CONSTRUCT the ontology slice and return it as an ``rdflib.Graph``.

        The ontology named graph is the *schema* (classes, properties,
        rdfs:domain/range/subClassOf, mappings) — small enough to materialize
        whole; ``VkgSliceBuilder`` then centrality-truncates it to the token
        budget. We CONSTRUCT the whole named graph with a single valid BGP
        (the previous ``(<>|!<>){0,n}`` property-path form is rejected by
        Neptune as a MalformedQuery — Blazegraph has no ``{n,m}`` path
        cardinality). ``candidates``/``n_hops`` are accepted for protocol
        compatibility; truncation (not the CONSTRUCT) does the scoping.
        """
        graph_uri = self.graph_uri or f"{self.graph_uri_prefix}{namespace}"
        query = (
            "CONSTRUCT { ?s ?p ?o } "
            f"WHERE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}"
        )  # nosec B608 - graph_uri from the gateway ontology payload (trusted)
        ttl = self.execute_sparql(query)
        g = Graph()
        if ttl:
            g.parse(data=ttl, format="turtle")
        return g
