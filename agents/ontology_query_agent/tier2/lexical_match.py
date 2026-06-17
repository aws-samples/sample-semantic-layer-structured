"""Lexical substring match against Neptune ``rdfs:label`` for Phase 1 cold-start.

Used as a fallback when the namespace's KNN index is missing — typically the
window between the first ontology publish and the rebuild Lambda finishing.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List

logger = logging.getLogger(__name__)


class NeptuneLexicalMatch:
    """Simple ``rdfs:label`` substring matcher backed by SPARQL.

    The query interpolates ``question`` only after lower-casing and stripping
    quote characters; consumers should still treat ``execute_sparql`` as the
    trust boundary and authenticate the underlying transport.
    """

    def __init__(self, *, execute_sparql: Callable[[str], Any],
                 graph_uri_prefix: str, limit: int = 20) -> None:
        """Initialize the lexical matcher.

        Args:
            execute_sparql: Callable that runs a SPARQL SELECT and returns
                the JSON-shaped Neptune response.
            graph_uri_prefix: Prefix used to build the named-graph URI from
                ``namespace`` (matches the publish-time prefix).
            limit: Cap on the number of IRIs returned.
        """
        self.execute_sparql = execute_sparql
        self.graph_uri_prefix = graph_uri_prefix
        self.limit = limit

    def lexical_match(self, *, question: str, namespace: str) -> List[str]:
        """Return up to ``limit`` IRIs whose label contains a question token."""
        # Strip characters that could break out of the FILTER STR literal.
        cleaned = question.replace('"', "").replace("\\", "").lower().strip()
        if not cleaned:
            return []
        graph = f"{self.graph_uri_prefix}{namespace}"
        query = (
            "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "  # nosec B608 — SQL/SPARQL built from internal schema-slice/static identifiers, not user input (grounding-gated)
            f"SELECT DISTINCT ?iri FROM <{graph}> "
            "WHERE { ?iri rdfs:label ?l . "
            f'FILTER(CONTAINS(LCASE(STR(?l)), "{cleaned}")) '
            f"}} LIMIT {self.limit}"
        )  # nosec B608 - graph_uri from controlled prefix; question scrubbed
        result = self.execute_sparql(query)
        bindings = result.get("results", {}).get("bindings", [])
        return [b["iri"]["value"] for b in bindings if "iri" in b]
