"""Unit tests for the VKG Phase 3 SPARQL generator + repair loop."""
from unittest.mock import MagicMock

from agents.ontology_query_agent.tier2.vkg_query_generator import VkgQueryGenerator


def test_generate_returns_sparql_on_first_try():
    fake_agent = MagicMock()
    result = MagicMock()
    result.structured_output = None
    result.message = {"content": [{"text": "SELECT ?s WHERE { ?s ?p ?o }"}]}
    fake_agent.return_value = result
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="@prefix ex: <...>", question="all things")
    assert out.startswith("SELECT")


def test_generate_strips_markdown_fences():
    """Models often wrap SPARQL in ```sparql fences — the generator strips them
    so rdflib parseQuery sees raw SPARQL (the live sparql_repair_failed bug)."""
    fake_agent = MagicMock()
    result = MagicMock()
    result.structured_output = None
    result.message = {"content": [{"text": "```sparql\nSELECT ?s WHERE { ?s ?p ?o }\n```"}]}
    fake_agent.return_value = result
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="x", question="q")
    assert out == "SELECT ?s WHERE { ?s ?p ?o }"
    assert "```" not in out


def test_generate_repairs_on_syntax_error():
    fake_agent = MagicMock()
    bad = MagicMock()
    bad.structured_output = None
    bad.message = {"content": [{"text": "SELECT bad sparql"}]}
    good = MagicMock()
    good.structured_output = None
    good.message = {"content": [{"text": "SELECT ?s WHERE { ?s ?p ?o }"}]}
    fake_agent.side_effect = [bad, good]
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="x", question="q")
    assert out == "SELECT ?s WHERE { ?s ?p ?o }"
    assert fake_agent.call_count == 2


def test_generate_desugars_unquoted_boolean_object():
    """Ontop maps every column to VARCHAR, so a bare `?x :is_deleted false`
    matches ~0 rows (a COUNT collapsed to 1 instead of 15). The generator must
    deterministically rewrite the boolean object into a bound var + a
    string-tolerant FILTER, and the result must be valid SPARQL."""
    from agents.ontology_query_agent.tier2.sparql_validator import validate_sparql
    ns = "http://ex/o/"
    sparql = (
        "SELECT (COUNT(*) AS ?n) WHERE {\n"
        f"  ?party a <{ns}Party> ;\n"
        f"         <{ns}is_deleted> false .\n"
        "}"
    )
    fake_agent = MagicMock()
    result = MagicMock()
    result.structured_output = None
    result.message = {"content": [{"text": sparql}]}
    fake_agent.return_value = result
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="@prefix ex: <...>",
                     question="how many parties exist")
    # The bare boolean object is gone; a tolerant string FILTER replaces it.
    assert "is_deleted> false" not in out
    assert "FILTER(LCASE(STR(" in out
    assert '"false"' in out and '"0"' in out
    # Still valid SPARQL (and the COUNT structure is preserved).
    validate_sparql(out)
    assert "COUNT(*)" in out
