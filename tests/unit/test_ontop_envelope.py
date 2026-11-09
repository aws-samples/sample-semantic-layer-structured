"""Contract tests for the shared Ontop-envelope SPARQL guard.

These 3 tests are the behavioural contract for ``agents.shared.ontop_envelope.apply``:
the rewriter must keep SPARQL inside the subset Ontop can reformulate into SQL —
hoisting arithmetic-over-aggregates out of the projection, stripping redundant
numeric casts, and remaining idempotent + fail-soft.
"""
from agents.shared.ontop_envelope import apply


def test_hoists_arithmetic_over_aggregates():
    out = apply("SELECT (SUM(?a)/SUM(?b) AS ?r) WHERE { ?x :a ?a ; :b ?b }")
    assert "/" not in out.split("WHERE")[0]        # no division in projection
    assert "SUM(?a)" in out and "SUM(?b)" in out    # raw aggregates preserved


def test_strips_redundant_decimal_cast():
    out = apply("SELECT (SUM(xsd:decimal(?a)) AS ?s) WHERE { ?x :a ?a }")
    assert "xsd:decimal(?a)" not in out and "SUM(?a)" in out


def test_idempotent_and_failsoft():
    q = "SELECT ?c (COUNT(?x) AS ?n) WHERE { ?x a ?c } GROUP BY ?c"
    assert apply(apply(q)) == apply(q)
    assert apply("not { valid ( sparql") == "not { valid ( sparql"


# --- Finding 1: dangling ?r guard when the hoisted alias is referenced elsewhere ---

def test_hoist_skipped_when_alias_used_in_order_by():
    # ?r is referenced in ORDER BY DESC(?r); hoisting would drop ?r and leave it
    # unbound → must be left UNCHANGED (fail-soft).
    q = ("SELECT (SUM(?a)/SUM(?b) AS ?r) WHERE { ?x :a ?a ; :b ?b } "
         "ORDER BY DESC(?r)")
    assert apply(q) == q


def test_hoist_skipped_when_alias_used_in_having():
    # ?r is referenced in HAVING(?r > 1); hoisting would leave it unbound → UNCHANGED.
    q = ("SELECT (SUM(?a)/SUM(?b) AS ?r) WHERE { ?x :a ?a ; :b ?b } "
         "GROUP BY ?x HAVING(?r > 1)")
    assert apply(q) == q


def test_hoist_still_applies_when_alias_terminal():
    # ?r appears solely in its own (... AS ?r) → terminal → still hoisted as before.
    out = apply("SELECT (SUM(?a)/SUM(?b) AS ?r) WHERE { ?x :a ?a ; :b ?b }")
    assert "/" not in out.split("WHERE")[0]
    assert "SUM(?a)" in out and "SUM(?b)" in out
    assert "?r_num" in out and "?r_den" in out


# --- Finding 2: author-written BIND ( with a space must not be corrupted ---

def test_existing_spaced_bind_left_unchanged():
    # A valid author-written BIND (with a space) must NOT be treated as a SELECT
    # projection alias and rewritten → returned UNCHANGED.
    q = ("SELECT ?m (COUNT(?x) AS ?n) WHERE { ?x :d ?d "
         "BIND (SUBSTR(?d,1,7) AS ?m) } GROUP BY ?m")
    out = apply(q)
    assert out == q
    assert "BIND ?m" not in out            # not rewritten to a bare BIND
    assert out.count("BIND") == 1          # no duplicate BIND injected


def test_var_ending_in_bind_not_mistaken_for_bind_keyword():
    # A projection alias preceded by a variable that merely ENDS in "bind"
    # (e.g. ?rebind) must still be rewritten — the BIND guard is word-boundary aware.
    q = ("SELECT ?rebind (SUBSTR(?d,1,7) AS ?m) (COUNT(?x) AS ?n) "
         "WHERE { ?x :r ?rebind ; :d ?d } GROUP BY ?m")
    out = apply(q)
    assert out != q                        # the computed ?m projection WAS moved
    assert "BIND(SUBSTR(?d,1,7) AS ?m)" in out


def test_computed_groupby_rewrite_idempotent():
    # A computed GROUP BY that the pass DOES rewrite must stay idempotent.
    q = ("SELECT (SUBSTR(?d,1,7) AS ?m) (COUNT(?x) AS ?n) "
         "WHERE { ?x :d ?d } GROUP BY ?m")
    once = apply(q)
    assert once != q                       # it was rewritten
    assert apply(once) == once             # idempotent
