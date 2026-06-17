"""Unit tests for the rdflib-backed SPARQL syntax validator."""
import pytest

from agents.ontology_query_agent.tier2.sparql_validator import (
    SparqlSyntaxError,
    validate_sparql,
)


def test_valid_select_passes():
    validate_sparql("SELECT ?s WHERE { ?s ?p ?o }")


def test_invalid_raises():
    with pytest.raises(SparqlSyntaxError):
        validate_sparql("SELECT ?s WHERE { malformed")
