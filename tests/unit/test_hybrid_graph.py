"""Tier-2 Hybrid graph: ontology-verified grounding gate + abstain back-edge.

Drives the full 6-phase Hybrid graph (recall router -> disambiguation -> slice
build -> slice disambiguation -> SQL generate -> ontology-verify gate) through
its Phase-5 back-edge with injected stubs. The scenario exercises a REAL
qualified-grounding violation: a predicate whose ``rdfs:domain`` is ``Address``
is mapped to BOTH ``address.state_tc`` (correctly domained) and
``party.state_tc`` (wrong class). SQL that reads ``party.state_tc`` therefore
resolves to that predicate but its subject class ``Party`` is NOT admitted by
the predicate's ``Address`` domain -> a ``property:<iri> on <Party>`` miss.
Reading ``address.state_tc`` grounds cleanly.
"""
from agents.semantic_layer_query_agent.graph import run_hybrid_workflow, HybridDeps


# The thin-VKG structural slice the fetch seam returns. Two class mappings
# (Party / Address) and one predicate (hasStateTc) whose rdfs:domain is Address
# but which is mapped to columns on BOTH tables. Using the predicate against a
# party row is the wrong-class violation; against an address row it grounds.
_SLICE_TTL = """\
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix vkg: <https://x#> .
<http://x/Address> vkg:mapsToTable "address" .
<http://x/Party>   vkg:mapsToTable "party" .
<http://x/hasStateTc> rdfs:domain <http://x/Address> .
<http://x/hasStateTc> vkg:mapsToColumn "address.state_tc" .
<http://x/hasStateTc> vkg:mapsToColumn "party.state_tc" .
"""


class _Router:
    def find_candidates(self, *, question, namespace):
        return ["db.address"]


class _Builder:
    def build(self, *, candidates, namespace, question):
        return "{}"

    def is_sufficient(self, *, slice_text, question):
        return True, None

    def expand(self, *, slice_text, missing):
        return "{}"


def _deps(gen_seq):
    calls = {"i": 0}

    class _Gen:
        def generate(self, *, slice_text, question, negative_constraint=""):
            sql = gen_seq[min(calls["i"], len(gen_seq) - 1)]
            calls["i"] += 1
            return sql

        last_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}

    class _Fetch:
        def get(self, *, namespace, candidates):
            from rdflib import Graph

            g = Graph()
            g.parse(data=_SLICE_TTL, format="turtle")
            return g

    return HybridDeps(
        router=_Router(),
        builder=_Builder(),
        generator=_Gen(),
        fetch=_Fetch(),
        run_execution=lambda ctx: {"answer": "ok", "usage": {}},
        vocab="https://x#",
        dialect="athena",
    )


def test_regenerate_then_ground():
    # First SQL uses state_tc on party (predicate domained to Address -> a
    # wrong-class violation), second uses it on address (grounds).
    deps = _deps([
        "SELECT p.state_tc FROM party p",
        "SELECT a.state_tc FROM address a",
    ])
    ctx = run_hybrid_workflow(question="q", namespace="ns", deps=deps)
    assert ctx.degraded is None
    assert ctx.grounding_rounds >= 1


def test_abstain_after_max_rounds():
    # Always emits the wrong-class SQL, so the violation fires every round and
    # the graph abstains once the grounding-round ceiling is exceeded.
    deps = _deps(["SELECT p.state_tc FROM party p"])
    ctx = run_hybrid_workflow(question="q", namespace="ns", deps=deps)
    assert ctx.degraded == "grounding_unresolved"


def test_ambiguous_unqualified_column_is_unverifiable_not_grounded():
    # An UNQUALIFIED column with TWO tables in scope cannot be pinned to one of
    # them, so the bridge emits no triple and grounding would be VACUOUS. The gate
    # must treat this as unverifiable and route to regenerate/abstain — NOT wave it
    # through. Here the generator never qualifies it, so the graph abstains.
    # (A SINGLE-table query is different: SQL itself resolves the bare column, so
    # the bridge does too — see the next test.)
    deps = _deps([
        "SELECT state_tc FROM address JOIN party ON address.x = party.x",
    ])
    ctx = run_hybrid_workflow(question="q", namespace="ns", deps=deps)
    assert ctx.degraded == "grounding_unresolved"


def test_ambiguous_unqualified_then_qualified_grounds():
    # First SQL is unqualified across two tables (unverifiable -> regenerate); the
    # second qualifies the column so it maps to a correctly-domained predicate.
    deps = _deps([
        "SELECT state_tc FROM address JOIN party ON address.x = party.x",
        "SELECT a.state_tc FROM address a",
    ])
    ctx = run_hybrid_workflow(question="q", namespace="ns", deps=deps)
    assert ctx.degraded is None
    assert ctx.grounding_rounds >= 1


def test_unqualified_column_on_a_single_table_grounds_without_a_round():
    # The shape that abstained on 8 of 15 eval scenarios: valid single-table SQL
    # with no alias. SQL resolves the bare column against the only table in scope,
    # so the gate must ground it FIRST TIME rather than flag a correct column and
    # burn regeneration rounds telling the model not to use it.
    deps = _deps(["SELECT state_tc FROM address"])
    ctx = run_hybrid_workflow(question="q", namespace="ns", deps=deps)
    assert ctx.degraded is None
    assert ctx.grounding_rounds == 0


def test_triple_free_but_clean_sql_grounds():
    # A query with NO ontology triples and NOTHING unmapped (no column refs at
    # all) has nothing to verify and is allowed through — the unverifiable guard
    # must not over-trigger on legitimately triple-free SQL like COUNT(*).
    deps = _deps(["SELECT COUNT(*) FROM address a"])
    ctx = run_hybrid_workflow(question="q", namespace="ns", deps=deps)
    assert ctx.degraded is None
    assert ctx.grounding_rounds == 0


# --------------------------------------------------------------------------- #
# Athena execution context.                                                    #
# --------------------------------------------------------------------------- #
def test_catalog_id_reaches_phase5_execution():
    # Phase 1 can only recover the DATABASE from a "database.table" candidate id.
    # The CATALOG comes from the layer config, and an S3 Tables schema resolves
    # ONLY when it is named in the Athena execution context — without it Phase 5
    # dies with SCHEMA_NOT_FOUND after a perfectly grounded query was generated.
    seen = {}

    deps = _deps(["SELECT COUNT(*) FROM address a"])
    deps.run_execution = lambda ctx: (
        seen.update(catalog=ctx.catalog_id, database=ctx.database_name),
        {"answer": "ok", "usage": {}},
    )[1]

    ctx = run_hybrid_workflow(
        question="q", namespace="ns", deps=deps,
        catalog_id="s3tablescatalog/semantic-layer-dev-analytics-tables",
    )
    assert ctx.degraded is None
    assert seen["catalog"] == "s3tablescatalog/semantic-layer-dev-analytics-tables"
    # Phase 1 still derives the database from the top candidate ("db.address").
    assert seen["database"] == "db"


def test_catalog_id_defaults_to_empty_for_standard_glue():
    # A plain Glue layer passes no catalog; the execution seam then omits it from
    # the QueryExecutionContext, which is what standard Glue requires.
    seen = {}
    deps = _deps(["SELECT COUNT(*) FROM address a"])
    deps.run_execution = lambda ctx: (
        seen.update(catalog=ctx.catalog_id), {"answer": "ok", "usage": {}},
    )[1]

    run_hybrid_workflow(question="q", namespace="ns", deps=deps)
    assert seen["catalog"] == ""
