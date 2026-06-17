"""Parse-only SPARQL validator using rdflib.

We don't have a Java Jena ARQ available in the runtime; rdflib's parser is
adequate for syntax-level checks ("did the LLM emit a well-formed query?")
which is the only invariant Phase 3 needs to enforce before handing off to
Neptune. Semantic mistakes (unknown classes/predicates) surface as Neptune
empty-result-sets, not as 5xx, so they don't need to be caught here.
"""
from __future__ import annotations

from rdflib.plugins.sparql.parser import parseQuery


class SparqlSyntaxError(ValueError):
    """Raised when a SPARQL query string fails to parse."""


def validate_sparql(query: str) -> None:
    """Raise ``SparqlSyntaxError`` if ``query`` doesn't parse.

    Args:
        query: A SPARQL 1.1 query string.
    """
    try:
        parseQuery(query)
    except Exception as e:  # noqa: BLE001 - rdflib raises a grab-bag of types
        raise SparqlSyntaxError(str(e)) from e
