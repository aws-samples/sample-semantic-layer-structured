"""Ontop-envelope SPARQL guard: keep generated SPARQL inside the subset Ontop
(the SPARQL→SQL reformulator behind the VKG) can actually translate.

The ``ontology_query_agent`` sometimes emits SPARQL that is syntactically valid
yet lies OUTSIDE the fragment Ontop can reformulate. Ontop then raises a
``SQLSerializationException`` and the query never executes — a hard zero on the
benchmark even when the SPARQL is semantically correct. Eval analysis surfaced a
handful of recurring shapes that trip this:

  (a) arithmetic dividing two aggregates directly in the SELECT projection, e.g.
      ``SELECT (SUM(?a)/SUM(?b) AS ?r)`` — Ontop cannot serialize an aggregate-
      over-aggregate arithmetic expression;
  (b) a computed GROUP BY / ORDER BY key projected as ``(<expr> AS ?k)`` and then
      grouped on ``?k`` — Ontop needs the computation bound via a WHERE ``BIND``;
  (c) redundant ``xsd:decimal()`` / ``xsd:double()`` / ``xsd:integer()`` casts over
      a bare column variable inside an aggregate — the mapped columns are now
      numerically typed, so the cast is dead weight that can defeat translation.

This module consolidates + extends the deterministic SPARQL rewriters that already
live in
``agents/ontology_query_agent/tier2/vkg_query_generator.py`` (``_desugar_*`` /
``_NUM_AGG_RE`` / ``_PROJ_ALIAS_RE``) into a single reusable ``apply`` pipeline.
Every rewriter is:

  * PURE — no I/O, no shared mutable state; input string → output string;
  * IDEMPOTENT — running it on its own output is a no-op (``apply(apply(q)) ==
    apply(q)``), which lets callers apply it freely without accumulating damage;
  * FAIL-SOFT — a regex surprise or malformed input returns the input UNCHANGED
    rather than raising, so this guard can never break query generation. This is a
    best-effort translatability enrichment, NOT a validator; genuinely malformed
    SPARQL still surfaces its real error downstream in the syntax-repair round.
"""
from __future__ import annotations

import re
from typing import Callable, List

# ---------------------------------------------------------------------------
# Regex fragments / patterns
# ---------------------------------------------------------------------------

# An aggregate call over a BARE variable, e.g. ``SUM(?a)``, ``AVG( ?amt )``,
# ``COUNT(?x)``. Used by the arithmetic-hoist pass. The inner token is a single
# bare variable — by the time this pass runs, redundant casts have already been
# stripped by ``_strip_redundant_decimal_casts`` upstream in ``apply``.
_AGG = r"(?:SUM|AVG|MIN|MAX|COUNT)\s*\(\s*\?[A-Za-z_]\w*\s*\)"


# Matches an aggregate whose SOLE argument is a redundant numeric cast over a bare
# variable, e.g. ``SUM(xsd:decimal(?a))`` / ``AVG(xsd:double(?x))`` — capturing the
# aggregate function and the inner variable. Ontop maps the underlying columns as
# numeric now, so the cast is redundant and can block reformulation. Rewritten to
# ``fn(?var)``. An already-stripped ``SUM(?a)`` has no cast token, so it does not
# re-match → idempotent.
_AGG_CAST_RE = re.compile(
    r"\b(?P<fn>SUM|AVG|MIN|MAX|COUNT)\s*\(\s*"
    r"xsd:(?:decimal|double|integer)\s*\(\s*(?P<var>\?[A-Za-z_]\w*)\s*\)\s*\)",
    re.IGNORECASE,
)


# Matches a SELECT-projection alias whose expression is arithmetic between TWO
# aggregate calls, e.g. ``(SUM(?a)/SUM(?b) AS ?r)`` — capturing the numerator
# aggregate, the operator, the denominator aggregate, and the alias variable. Ontop
# cannot serialize aggregate-over-aggregate arithmetic in the projection. Once split
# into two separate ``(<agg> AS ?x)`` aliases there is no arithmetic operator between
# aggregates in a single alias, so this does not re-match → idempotent.
_AGG_ARITH_RE = re.compile(
    r"\(\s*(?P<num>" + _AGG + r")\s*(?P<op>[/*+\-])\s*(?P<den>" + _AGG + r")"
    r"\s+AS\s+(?P<var>\?[A-Za-z_]\w*)\s*\)",
    re.IGNORECASE,
)


# Matches a SELECT-projection alias ``(<expr> AS ?v)`` where <expr> is a COMPUTED
# expression (contains a function call / paren), NOT a bare aggregate. Ontop cannot
# translate a computed projection that is then used in GROUP BY / ORDER BY directly —
# it must be bound via a WHERE ``BIND`` first. Aggregates (SUM/COUNT/AVG/MIN/MAX) are
# excluded via the leading negative lookahead — those are legitimately projected.
#
# NOTE: adapted from ``vkg_query_generator._PROJ_ALIAS_RE`` (and the function below
# from ``_desugar_computed_groupby``) so this shared module stays self-contained rather
# than importing from the tier2 agent package. Keep the two in sync if either changes.
# ADAPTATION: an existing ``BIND(<expr> AS ?v)`` — whether injected by a prior pass or
# author-written — must NOT be matched as a SELECT-projection alias, otherwise it is
# corrupted into a bare ``BIND ?v`` plus a duplicate injected BIND. We do NOT use a
# fixed-width ``(?<!BIND)`` negative lookbehind for this: it only blocks the no-space
# ``BIND(`` form and lets a valid author-written ``BIND (<expr> AS ?v)`` (note the space)
# through, regressing translatable input to broken output. Instead the callback below
# inspects the text immediately preceding each match and rejects it when that text ends
# with ``BIND`` (whitespace-tolerant, case-insensitive), covering both ``BIND(`` and
# ``BIND (…``. This is also what keeps the pass idempotent across re-application.
_PROJ_ALIAS_RE = re.compile(
    r"\(\s*(?P<expr>(?![\s]*(?:SUM|COUNT|AVG|MIN|MAX)\s*\()[^()]*\([^()]*\)[^()]*)"
    r"\s+AS\s+(?P<var>\?[A-Za-z_]\w*)\s*\)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Rewriters (each pure, idempotent, fail-soft)
# ---------------------------------------------------------------------------

def _strip_redundant_decimal_casts(sparql: str) -> str:
    """Strip a redundant numeric cast wrapping a bare variable inside an aggregate.

    Rewrites ``SUM(xsd:decimal(?v))`` → ``SUM(?v)`` (and the ``xsd:double`` /
    ``xsd:integer`` variants) for SUM/AVG/MIN/MAX/COUNT. The VKG relational columns
    are numerically typed now, so casting a bare column variable inside the aggregate
    is dead weight that can defeat Ontop reformulation. Only the sole-argument cast
    form is targeted (the shape the model emits); a bare ``SUM(?v)`` has no cast token
    so it does not re-match → idempotent. Fail-soft: returns the input unchanged on any
    regex surprise.

    Args:
        sparql: The generated SPARQL query text.

    Returns:
        str: The query with redundant in-aggregate numeric casts removed.
    """
    try:
        def _repl(m: "re.Match") -> str:
            # Preserve the aggregate's original spelling; drop only the cast wrapper.
            return f"{m.group('fn')}({m.group('var')})"

        return _AGG_CAST_RE.sub(_repl, sparql)
    except Exception:  # noqa: BLE001 — enrichment must never break generation
        return sparql


def _hoist_aggregate_arithmetic(sparql: str) -> str:
    """Split arithmetic-over-aggregates in a SELECT projection into raw-aggregate aliases.

    Ontop cannot serialize an expression like ``(SUM(?a)/SUM(?b) AS ?r)`` — arithmetic
    combining two aggregate calls in the projection. We rewrite the single arithmetic
    alias into TWO plain aggregate aliases exposing the raw operands, e.g.::

        (SUM(?a)/SUM(?b) AS ?r)  ->  (SUM(?a) AS ?r_num) (SUM(?b) AS ?r_den)

    and DROP the arithmetic alias entirely. The downstream answer renderer recombines
    the two raw aggregates (e.g. computes the ratio) outside SPARQL. The new alias names
    are derived from the original (``?r`` → ``?r_num`` / ``?r_den``) so they stay stable
    and human-readable. After the split there is no arithmetic operator between two
    aggregates in a single alias, so this pass does not re-match its own output →
    idempotent. Fail-soft: returns the input unchanged on any regex surprise.

    Dangling-var guard: the alias var ``?r`` is DROPPED by the split, so we only hoist
    when ``?r`` is TERMINAL — i.e. it appears nowhere else in the query. If ``?r`` is
    referenced anywhere else (``ORDER BY DESC(?r)``, ``HAVING(?r > 1)``, another
    projection like ``(?r * 100 AS ?pct)``), hoisting would leave those references
    unbound → silently wrong or lost results. In that case we SKIP the hoist for that
    alias and leave it unchanged (fail-soft: a possibly-untranslatable query beats a
    silently-wrong one).

    Args:
        sparql: The generated SPARQL query text.

    Returns:
        str: The query with aggregate-arithmetic projections split into raw aggregates.
    """
    try:
        def _repl(m: "re.Match") -> str:
            var = m.group("var")
            # Count whole-token occurrences of the alias var across the WHOLE query.
            # The matched alias expression contains exactly one occurrence of ``?r``
            # (the ``AS ?r`` tail); a trailing negative lookahead prevents matching
            # ``?r`` as a prefix of ``?r_num`` / ``?rate`` etc. More than one occurrence
            # means ``?r`` is referenced elsewhere → dropping it would dangle → skip.
            occurrences = len(
                re.findall(re.escape(var) + r"(?![A-Za-z0-9_])", sparql)
            )
            if occurrences > 1:
                return m.group(0)  # not terminal → leave the arithmetic alias unchanged
            base = var[1:]  # strip leading '?' to build derived alias names
            num = m.group("num").strip()
            den = m.group("den").strip()
            # Two independent projected aggregates in place of the arithmetic alias.
            return f"({num} AS ?{base}_num) ({den} AS ?{base}_den)"

        return _AGG_ARITH_RE.sub(_repl, sparql)
    except Exception:  # noqa: BLE001 — enrichment must never break generation
        return sparql


def _prebind_computed_groupby(sparql: str) -> str:
    """Move a computed SELECT alias used in GROUP BY / ORDER BY into a WHERE ``BIND``.

    Ported from ``vkg_query_generator._desugar_computed_groupby`` (kept self-contained
    here). The model often emits ``SELECT (SUBSTR(?d,1,7) AS ?month) ... GROUP BY
    ?month`` — a computed expression in the projection that Ontop refuses to translate.
    For each such ``(<expr> AS ?v)`` where ``<expr>`` is a function call (NOT a bare
    aggregate) and ``?v`` appears in a GROUP BY / ORDER BY clause, we:

      1. replace the projection ``(<expr> AS ?v)`` with the bare variable ``?v``; and
      2. inject ``BIND(<expr> AS ?v)`` just before the final ``}`` of the WHERE clause.

    Aggregate projections (``(SUM(?x) AS ?t)``) are left untouched — they belong in
    SELECT. Purely SPARQL-syntactic and layer-agnostic (no IRI matched by name).
    Idempotent: after the move the alias is a bare variable (no ``(... AS ...)`` for the
    regex to re-match) and the BIND expression is excluded from the projection scan.
    Fail-soft: returns the input unchanged on any surprise or when no movable alias is
    grouped on.

    Args:
        sparql: The generated SPARQL query text.

    Returns:
        str: The query with computed grouping keys moved to BIND, or the input unchanged.
    """
    try:
        binds: List[str] = []

        def _maybe_move(m: "re.Match") -> str:
            # Whitespace-tolerant guard: if the text immediately before this ``(`` ends
            # with ``BIND`` (case-insensitive), this is an existing BIND expression
            # (``BIND(<expr> AS ?v)`` OR author-written ``BIND (<expr> AS ?v)``), NOT a
            # SELECT projection alias — leave it untouched. This replaces the old
            # fixed-width ``(?<!BIND)`` lookbehind, which missed the spaced form and
            # corrupted valid input into ``BIND ?v`` + a duplicate injected BIND.
            preceding = sparql[:m.start()].rstrip()
            # Word-boundary match so a variable/prefixed-name that merely ends in
            # "bind" (e.g. ``?rebind``, ``ex:unbind``) preceding a real projection is
            # NOT mistaken for an existing BIND keyword.
            if re.search(r"(?<![A-Za-z0-9_])BIND$", preceding, re.IGNORECASE):
                return m.group(0)  # existing BIND, not a projection → untouched
            expr = m.group("expr").strip()
            var = m.group("var")
            # Only move when the alias var is actually used in GROUP BY / ORDER BY —
            # otherwise it is a plain computed projection that Ontop can handle.
            grouped = re.search(
                r"\b(?:GROUP\s+BY|ORDER\s+BY)\b[^{}]*" + re.escape(var) + r"\b",
                sparql, re.IGNORECASE,
            )
            if not grouped:
                return m.group(0)  # leave untouched
            binds.append(f"  BIND({expr} AS {var})")
            return var  # bare variable in the projection

        rewritten = _PROJ_ALIAS_RE.sub(_maybe_move, sparql)
        if not binds:
            return sparql
        # Inject the BIND(s) before the LAST '}' (the WHERE-clause close). Use rfind so
        # a trailing GROUP BY / ORDER BY / LIMIT after the brace is untouched.
        close = rewritten.rfind("}")
        if close == -1:
            return sparql
        return (rewritten[:close].rstrip() + "\n" + "\n".join(binds) + "\n"
                + rewritten[close:])
    except Exception:  # noqa: BLE001 — enrichment must never break generation
        return sparql


# Ordered pipeline. Order matters:
#   1. strip casts first, so the aggregate-arithmetic pass sees clean ``SUM(?a)`` forms
#      (its ``_AGG`` fragment expects a bare variable, not a cast wrapper);
#   2. hoist aggregate arithmetic;
#   3. prebind computed GROUP BY / ORDER BY keys last.
# Each stage is individually idempotent and the combination is order-stable, so
# ``apply(apply(q)) == apply(q)`` holds.
_PIPELINE: List[Callable[[str], str]] = [
    _strip_redundant_decimal_casts,
    _hoist_aggregate_arithmetic,
    _prebind_computed_groupby,
]


def apply(sparql: str) -> str:
    """Rewrite ``sparql`` to keep it inside Ontop's translatable envelope.

    Runs the ordered pipeline of pure, idempotent, fail-soft rewriters
    (:data:`_PIPELINE`). Each stage is already individually fail-soft; this function
    adds a top-level guard so that even an unexpected error in the pipeline wiring
    returns the ORIGINAL ``sparql`` unchanged. The rewriters only transform the recurring
    Ontop-hostile shapes and leave everything else byte-for-byte identical, so applying
    ``apply`` to already-safe SPARQL is a no-op.

    Args:
        sparql: The generated SPARQL query text.

    Returns:
        str: The rewritten SPARQL, or the original input unchanged on any failure.
    """
    try:
        out = sparql
        for rewriter in _PIPELINE:
            out = rewriter(out)
        return out
    except Exception:  # noqa: BLE001 — top-level safety net; never break generation
        return sparql
