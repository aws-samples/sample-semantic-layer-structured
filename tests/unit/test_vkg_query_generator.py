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


def test_generate_strips_prose_preamble():
    """On a repair round the model sometimes prepends prose ("Looking at the
    question, here is the corrected query:") before the SPARQL. rdflib fails at
    char 0, which tripped the gt-04 sparql_repair_failed/degrade path even though
    a valid query followed. The generator must trim the preamble to the first
    SPARQL keyword."""
    fake_agent = MagicMock()
    result = MagicMock()
    result.structured_output = None
    result.message = {"content": [{"text": (
        "Looking at the question, here is the corrected query:\n\n"
        "SELECT ?s WHERE { ?s ?p ?o }"
    )}]}
    fake_agent.return_value = result
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="x", question="q")
    assert out == "SELECT ?s WHERE { ?s ?p ?o }"
    assert "Looking at" not in out


def test_generate_strips_prose_preamble_before_prefix():
    """Prose preamble ahead of a PREFIX prologue is trimmed to the PREFIX line."""
    fake_agent = MagicMock()
    result = MagicMock()
    result.structured_output = None
    result.message = {"content": [{"text": (
        "Here is the query:\n"
        "PREFIX ex: <http://ex/o/>\n"
        "SELECT ?s WHERE { ?s a ex:Thing }"
    )}]}
    fake_agent.return_value = result
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="x", question="q")
    assert out.startswith("PREFIX ex:")
    assert "Here is" not in out


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


def test_generate_desugars_boolean_in_filter():
    """gt-06 fix: a `FILTER(?isDeleted = false)` comparison (boolean inside a
    FILTER, not a triple object) escaped the triple-object rewriter and slipped
    through to Ontop unrewritten — every column is VARCHAR, so the boolean
    matched ~0 rows and the query failed to translate. The generator must rewrite
    the FILTER in place to the string-tolerant set form."""
    from agents.ontology_query_agent.tier2.sparql_validator import validate_sparql
    ns = "http://ex/o/"
    sparql = (
        "SELECT ?productName WHERE {\n"
        f"  ?cp a <{ns}CoverageProduct> ;\n"
        f"      <{ns}product_name> ?productName ;\n"
        f"      <{ns}is_deleted> ?isDeleted .\n"
        "  FILTER(?isDeleted = false)\n"
        "}\n"
        "ORDER BY ?productName LIMIT 10"
    )
    fake_agent = MagicMock()
    result = MagicMock()
    result.structured_output = None
    result.message = {"content": [{"text": sparql}]}
    fake_agent.return_value = result
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="@prefix ex: <...>",
                     question="top 10 coverage products by name")
    # The bare-boolean FILTER comparison is gone; a tolerant string set replaces it.
    assert "= false" not in out
    assert "FILTER(LCASE(STR(?isDeleted)) IN (" in out
    assert '"false"' in out and '"0"' in out
    validate_sparql(out)


def test_generate_desugars_quoted_boolean_in_filter():
    """gt-00 fix: a QUOTED-string boolean comparison ``FILTER(?del = "false")``
    slips through the unquoted-only rewriter. Since the gt-08 typing fix made flag
    columns PHYSICALLY boolean in db-metadata, a string literal "false" has a
    different datatype than the boolean value and silently matches ~0 rows (the
    gt-00 self-join returned 0 rows for exactly this reason). The generator must
    rewrite the quoted form to the same string-tolerant set form that gt-03 used
    successfully — handling both single and double quotes."""
    from agents.ontology_query_agent.tier2.sparql_validator import validate_sparql
    ns = "http://ex/o/"
    sparql = (
        "SELECT ?holdingId WHERE {\n"
        f"  ?lp a <{ns}LifeParticipant> ;\n"
        f"      <{ns}holding_id> ?holdingId ;\n"
        f"      <{ns}is_deleted> ?del1 .\n"
        '  FILTER(?del1 = "false")\n'
        "}\n"
    )
    fake_agent = MagicMock()
    result = MagicMock()
    result.structured_output = None
    result.message = {"content": [{"text": sparql}]}
    fake_agent.return_value = result
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="@prefix ex: <...>",
                     question="policies where insured is also policyholder")
    # The quoted-boolean comparison is gone; the tolerant string set replaces it.
    assert '= "false"' not in out
    assert "FILTER(LCASE(STR(?del1)) IN (" in out
    assert '"false"' in out and '"0"' in out
    validate_sparql(out)


def test_generate_preserves_genuine_string_equality():
    """The quoted-boolean rewriter must NOT touch a genuine string equality like
    ``FILTER(?status = "Active")`` — the inner literal is pinned to true|false
    only, so a real value-filter is left exactly as the model wrote it."""
    ns = "http://ex/o/"
    sparql = (
        "SELECT ?holdingId WHERE {\n"
        f"  ?h a <{ns}Holding> ;\n"
        f"     <{ns}holding_id> ?holdingId ;\n"
        f"     <{ns}holding_status> ?status .\n"
        '  FILTER(?status = "Active")\n'
        "}\n"
    )
    fake_agent = MagicMock()
    result = MagicMock()
    result.structured_output = None
    result.message = {"content": [{"text": sparql}]}
    fake_agent.return_value = result
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="@prefix ex: <...>",
                     question="active holdings")
    # The genuine string equality is untouched (not rewritten to a boolean set).
    assert '?status = "Active"' in out
    assert "LCASE(STR(?status))" not in out


def test_generate_casts_numeric_aggregates():
    """gt-03/gt-08 fix: Ontop maps numeric columns to VARCHAR, so a bare
    SUM(?amount) aggregates TEXT — the SQL errors and the LLM repair rewrites it
    to count non-numeric rows. The generator must deterministically wrap the
    aggregated variable in xsd:decimal() (SUM/AVG/MIN/MAX, never COUNT)."""
    from agents.ontology_query_agent.tier2.sparql_validator import validate_sparql
    ns = "http://ex/o/"
    sparql = (
        "SELECT ?partyId (SUM(?marketValue) AS ?total) WHERE {\n"
        f"  ?h a <{ns}Holding> ; <{ns}market_value> ?marketValue ; "
        f"<{ns}party_id> ?partyId .\n"
        "}\n"
        "GROUP BY ?partyId ORDER BY DESC(SUM(?marketValue))"
    )
    fake_agent = MagicMock()
    result = MagicMock()
    result.structured_output = None
    result.message = {"content": [{"text": sparql}]}
    fake_agent.return_value = result
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="@prefix ex: <...>",
                     question="top parties by total market value")
    # Both the SELECT aggregate AND the ORDER BY aggregate are cast.
    assert "SUM(xsd:decimal(?marketValue))" in out
    assert "SUM(?marketValue)" not in out
    assert out.count("xsd:decimal") == 2
    validate_sparql(out)


def test_desugar_computed_groupby_moves_to_bind():
    """A computed SELECT alias used in GROUP BY is moved to a WHERE-clause BIND."""
    from agents.ontology_query_agent.tier2.vkg_query_generator import _desugar_computed_groupby
    sparql = ("SELECT (SUBSTR(?d, 1, 7) AS ?month) (SUM(xsd:decimal(?a)) AS ?t) "
              "WHERE { ?x <http://e/d> ?d ; <http://e/a> ?a . } GROUP BY ?month ORDER BY ?month")
    out = _desugar_computed_groupby(sparql)
    assert "BIND(SUBSTR(?d, 1, 7) AS ?month)" in out
    # projection now bare ?month, not the computed expr
    assert out.split("WHERE")[0].count("SUBSTR") == 0
    # aggregate projection untouched
    assert "(SUM(xsd:decimal(?a)) AS ?t)" in out
    # grouping still references the variable
    assert "GROUP BY ?month" in out


def test_desugar_computed_groupby_leaves_aggregates_and_plain_alone():
    """Aggregate projections and ungrouped computed aliases are NOT moved."""
    from agents.ontology_query_agent.tier2.vkg_query_generator import _desugar_computed_groupby
    # plain grouping on a triple-bound var — nothing to move
    plain = ("SELECT ?p (SUM(xsd:decimal(?mv)) AS ?t) WHERE "
             "{ ?h <http://e/p> ?p ; <http://e/mv> ?mv . } GROUP BY ?p")
    assert _desugar_computed_groupby(plain) == plain
    # a computed alias NOT used in GROUP BY/ORDER BY stays in the projection
    proj_only = ("SELECT (CONCAT(?a,?b) AS ?full) WHERE "
                 "{ ?x <http://e/a> ?a ; <http://e/b> ?b . }")
    assert _desugar_computed_groupby(proj_only) == proj_only


def test_desugar_computed_groupby_failsoft():
    """Malformed input returns unchanged (never raises)."""
    from agents.ontology_query_agent.tier2.vkg_query_generator import _desugar_computed_groupby
    assert _desugar_computed_groupby("") == ""
    assert _desugar_computed_groupby("SELECT garbage (((") == "SELECT garbage ((("


def _msg(text):
    r = MagicMock()
    r.structured_output = None
    r.message = {"content": [{"text": text}]}
    return r


# ── P2-1: disconnect guard + tautological-FILTER strip ───────────────────────
_DISCONNECTED = (
    "SELECT ?mv WHERE { "
    "?h a <http://b/Holding> ; <http://b/Holding/market_value> ?mv . "
    "?c a <http://b/Coverage> ; <http://b/Coverage/holding_id> ?hid . "
    "FILTER(?hid = ?hid) }"
)
_CONNECTED = (
    "SELECT ?mv WHERE { "
    "?h a <http://b/Holding> ; <http://b/Holding/holding_id> ?hid ; "
    "<http://b/Holding/market_value> ?mv . "
    "?c a <http://b/Coverage> ; <http://b/Coverage/holding_id> ?hid . }"
)


def test_generate_regenerates_on_disconnected_cartesian():
    """A disconnected (cartesian) query triggers ONE regenerate; the reconnected
    result is returned."""
    fake_agent = MagicMock()
    fake_agent.side_effect = [_msg(_DISCONNECTED), _msg(_CONNECTED)]
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="x", question="market value by party")
    assert "FILTER(?hid = ?hid)" not in out  # tautological filter stripped
    assert fake_agent.call_count == 2          # one regenerate happened
    # reconnected query kept (both Holding & Coverage joined on ?hid)
    assert "Holding/holding_id" in out


def test_generate_keeps_original_if_regenerate_still_disconnected():
    """If the regenerate is ALSO disconnected, keep the original (no degrade
    channel; Phase 5 is the backstop) — never loop further."""
    fake_agent = MagicMock()
    fake_agent.side_effect = [_msg(_DISCONNECTED), _msg(_DISCONNECTED)]
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="x", question="q")
    assert fake_agent.call_count == 2  # exactly one regenerate, then stop
    assert out.startswith("SELECT")


def test_generate_does_not_regenerate_connected_query():
    """A properly-joined multi-subject query is NOT flagged → no regenerate."""
    fake_agent = MagicMock()
    fake_agent.side_effect = [_msg(_CONNECTED)]
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="x", question="q")
    assert fake_agent.call_count == 1  # single call, no regenerate
    assert "Coverage/holding_id" in out


def test_strip_tautological_filters():
    from agents.ontology_query_agent.tier2.vkg_query_generator import (
        _strip_tautological_filters,
    )
    assert "FILTER" not in _strip_tautological_filters("X FILTER(?a = ?a) Y")
    # a real (distinct-var) FILTER is preserved
    assert "FILTER(?a = ?b)" in _strip_tautological_filters("X FILTER(?a = ?b) Y")


# ── R1: fabricated rename-BIND guard ─────────────────────────────────────────
_RENAME_FABRICATED = (
    "SELECT ?mv ?partyId WHERE { "
    "?h a <http://b/Holding> ; <http://b/Holding/holding_id> ?holdingId ; "
    "<http://b/Holding/market_value> ?mv . "
    "BIND(?holdingId AS ?partyId) }"
)
_RENAME_FIXED = (
    "SELECT ?mv ?fn WHERE { "
    "?cov a <http://b/Coverage> ; <http://b/Coverage/hasHolding> ?h ; "
    "<http://b/Coverage/hasParty> ?p . "
    "?h a <http://b/Holding> ; <http://b/Holding/market_value> ?mv . "
    "?p a <http://b/Party> ; <http://b/Party/full_name> ?fn . }"
)


def test_detect_fabricated_rename_bind():
    """A pure variable-to-variable BIND (no expression) is flagged; a computed
    BIND over an expression (CONCAT/SUBSTR/arithmetic) is NOT."""
    from agents.ontology_query_agent.tier2.vkg_query_generator import (
        detect_fabricated_rename_bind as f,
    )
    assert f("SELECT * WHERE { BIND(?holdingId AS ?partyId) }") is True
    assert f("SELECT * WHERE { bind(?x as ?y) }") is True  # case-insensitive
    # Legitimate computed binds: RHS is an expression, not a bare variable.
    assert f('SELECT * WHERE { BIND(CONCAT("P#", ?raw) AS ?pref) }') is False
    assert f("SELECT * WHERE { BIND(SUBSTR(STR(?d),1,7) AS ?month) }") is False
    assert f("SELECT * WHERE { BIND(?a + ?b AS ?sum) }") is False
    assert f("SELECT ?x WHERE { ?x a ex:C }") is False  # no bind


def test_generate_regenerates_on_fabricated_rename_bind():
    """A pure-rename BIND(?a AS ?b) that fakes a join triggers ONE regenerate; the
    fixed (real-join) result is returned (gt-07)."""
    fake_agent = MagicMock()
    fake_agent.side_effect = [_msg(_RENAME_FABRICATED), _msg(_RENAME_FIXED)]
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="x", question="top parties by holding market value")
    assert fake_agent.call_count == 2  # regenerated once
    assert "BIND(?holdingId AS ?partyId)" not in out
    assert "Coverage/hasParty" in out  # the real join survived


def test_generate_keeps_original_if_rename_persists():
    """If the regenerate STILL renames, keep the original (no infinite loop; Phase 5
    is the backstop)."""
    fake_agent = MagicMock()
    fake_agent.side_effect = [_msg(_RENAME_FABRICATED), _msg(_RENAME_FABRICATED)]
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text="x", question="q")
    assert fake_agent.call_count == 2  # one regenerate attempt, then stop
    assert "BIND(?holdingId AS ?partyId)" in out  # original kept


# ── state-filter guard (gt-03 mechanical enforcement) ────────────────────────
_STATE_SLICE = (
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "<http://b/Holding> a owl:Class .\n"
    "<http://b/Holding/holding_status> a owl:DatatypeProperty ;\n"
    "  rdfs:domain <http://b/Holding> ;\n"
    "  rdfs:comment \"Lifecycle status. Observed values: 'Active', 'Inactive', 'Closed'.\" .\n"
    "<http://b/Holding/market_value> a owl:DatatypeProperty ;\n"
    "  rdfs:domain <http://b/Holding> ; rdfs:comment \"Numeric market value.\" .\n"
)
_NO_FILTER = (
    "SELECT ?mv WHERE { ?h a <http://b/Holding> ; "
    "<http://b/Holding/market_value> ?mv . }"
)


def test_generate_no_state_guard_when_no_state_word():
    """A question with no documented state value never triggers the guard."""
    fake_agent = MagicMock()
    fake_agent.side_effect = [_msg(_NO_FILTER)]
    g = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    out = g.generate(slice_text=_STATE_SLICE,
                     question="total market value of all holdings")
    assert fake_agent.call_count == 1  # no regenerate
    assert out == _NO_FILTER
