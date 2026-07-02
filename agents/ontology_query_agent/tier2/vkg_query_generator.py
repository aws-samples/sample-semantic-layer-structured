"""Phase 3 (VKG): SPARQL generation with one repair round on syntax error.

The generator owns the LLM call; it does not own the SPARQL execution. The
orchestrator runs the validated query against Neptune. If the second attempt
still fails parsing, ``SparqlSyntaxError`` propagates and the calling
agent's ``main._run_query`` falls through to Tier 3 (F4).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

from .sparql_validator import SparqlSyntaxError, validate_sparql
from .grounding import detect_disconnected_subjects
from .enum_constraints import extract_enum_constraints, inject_enum_filters

logger = logging.getLogger(__name__)

# NOTE: the detailed SPARQL-generation guidance now lives in the DEPLOYED
# agent system prompt built in main.py (the agent_factory passed to
# VkgQueryGenerator). An earlier standalone _GEN_PROMPT constant here was
# never wired to an Agent (the generator takes an injected agent_factory),
# so it was removed to avoid drift between a dead constant and the live prompt.

# A leading ```sparql / ``` fence the model often wraps its output in, plus a
# trailing fence — strip them so rdflib's parser sees raw SPARQL.
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


# Matches a triple whose object is a bare SPARQL boolean literal on a FULL-IRI
# predicate, e.g.  `<…/is_deleted> false .`  or  `<…/active> true ;` — capturing
# the predicate IRI, the boolean, and the terminator (. ; ]). Ontop exposes every
# column as VARCHAR, so a boolean object compares against a string and matches ~0
# rows; we rewrite it to a string-tolerant FILTER (see _desugar_boolean_objects).
_BOOL_OBJ_RE = re.compile(
    r"<(?P<pred>[^>]+)>\s+(?P<bool>true|false)\s*(?P<term>[.;\]])"
)


# Matches a FILTER equality comparing a variable to a boolean literal, in EITHER
# the bare (`FILTER(?isDeleted = false)`) or QUOTED-string (`FILTER(?del = "false")`,
# `FILTER(?x = 'true')`) form, optionally with an `xsd:boolean` / `^^xsd:boolean`
# annotation. The bare-object rewriter above does NOT catch this form — the boolean
# lives inside a FILTER expression, not as a triple object.
#
# BOTH forms misbehave: the bare form fails to TRANSLATE on a VARCHAR column, and
# the QUOTED form silently matches ~0 rows when the column is PHYSICALLY boolean
# (the gt-08 typing fix made flag columns BOOLEAN in db-metadata, so a string
# literal "false" has a different datatype than the boolean value → no match — the
# gt-00 self-join returned 0 rows for exactly this reason). The optional-quote
# group `(?P<q>['"]?)` with the `(?P=q)` backreference matches `false`, `"false"`,
# and `'false'` while REJECTING mismatched quotes. CRITICAL: the inner token is
# pinned to `true|false` ONLY, so a genuine string equality like
# `FILTER(?status = "Active")` is NEVER rewritten. We rewrite to the same
# string-tolerant `LCASE(STR(?v)) IN (...)` set form the prompt prescribes — the
# form gt-03 used successfully against the same boolean-typed column.
_BOOL_FILTER_RE = re.compile(
    r"FILTER\s*\(\s*(?P<var>\?[A-Za-z_][\w]*)\s*=\s*"
    r"(?P<q>['\"]?)(?P<bool>true|false)(?P=q)\s*(?:\^\^\s*xsd:boolean)?\s*\)",
    re.IGNORECASE,
)


# Matches a numeric aggregate over a BARE variable, e.g. `SUM(?marketValue)` or
# `AVG( ?amt )` — capturing the aggregate function and the variable. Ontop maps
# every column to VARCHAR, so SUM/AVG/MIN/MAX over a raw amount variable
# aggregates TEXT: the translated SQL errors and the downstream LLM repair tends
# to "fix" it by counting non-numeric rows instead of casting. We deterministically
# wrap the variable in xsd:decimal() so the aggregate is numeric. Already-cast
# forms (`SUM(xsd:decimal(?v))`) do NOT match — the inner token is not a bare
# variable — so this is idempotent. COUNT is excluded: counting is correct over
# text and casting it would be wrong.
_NUM_AGG_RE = re.compile(
    r"\b(?P<fn>SUM|AVG|MIN|MAX)\s*\(\s*(?P<var>\?[A-Za-z_][\w]*)\s*\)",
    re.IGNORECASE,
)


def _desugar_boolean_objects(sparql: str) -> str:
    """Rewrite unquoted boolean comparisons into string-tolerant FILTERs.

    DETERMINISTIC backstop for the LLM (the prompt asks it not to emit unquoted
    booleans, but it sometimes still does). Because Ontop maps every relational
    column to VARCHAR, a SPARQL `xsd:boolean` never matches the stored string
    ("false"/"0"/"f"), silently collapsing a COUNT to ~0/1 instead of the true
    total — and the resulting query frequently fails to TRANSLATE at all. This
    handles BOTH unquoted-boolean shapes the model emits:

    1. Bare boolean triple-object (`?p :is_deleted false`): bind the predicate's
       value to a fresh variable on the same subject, then append a string-set
       FILTER. The terminator after the object (. ; ]) is preserved.
    2. Boolean equality FILTER (`FILTER(?isDeleted = false)`): rewrite IN PLACE to
       the string-tolerant `FILTER(LCASE(STR(?isDeleted)) IN (...))` form. This
       form lives inside a FILTER expression, not as a triple object, so the
       triple-object pass cannot see it — without this branch it slipped through
       to Ontop unrewritten (the gt-06 translation failure).

    Fresh var names are positional (``?_b0``, ``?_b1``…) so repeated predicates
    don't collide. No-op when there is no bare boolean. Fail-soft: returns the
    input unchanged on any regex/format surprise (prompt guidance remains the
    first line of defence).

    Args:
        sparql: The generated SPARQL query text.

    Returns:
        The query with boolean comparisons rewritten to string-tolerant FILTERs.
    """
    try:
        filters: list = []
        counter = {"n": 0}
        _TRUE = '"true", "1", "t", "yes", "y"'
        _FALSE = '"false", "0", "f", "no", "n"'

        def _repl(m: "re.Match") -> str:
            var = f"?_b{counter['n']}"
            counter["n"] += 1
            wanted = _TRUE if m.group("bool") == "true" else _FALSE
            filters.append(
                f"  FILTER(LCASE(STR({var})) IN ({wanted}))"
            )
            # Replace the bare boolean object with the bound variable, keeping the
            # predicate and the original terminator (. ; ]) intact.
            return f"<{m.group('pred')}> {var} {m.group('term')}"

        def _repl_filter(m: "re.Match") -> str:
            # Rewrite `FILTER(?v = true|false)` in place — the variable is already
            # bound by a triple pattern, so no fresh binding is needed.
            wanted = _TRUE if m.group("bool").lower() == "true" else _FALSE
            return f"FILTER(LCASE(STR({m.group('var')})) IN ({wanted}))"

        rewritten = _BOOL_OBJ_RE.sub(_repl, sparql)
        # In-place FILTER(?v = bool) rewrite — independent of the appended-filter
        # path below, so count it toward "did we change anything".
        rewritten, n_filter = _BOOL_FILTER_RE.subn(_repl_filter, rewritten)
        if not filters:
            # No bare boolean OBJECTS to append; but a FILTER rewrite may still
            # have happened in place — return that (changed) text if so.
            return rewritten if n_filter else sparql
        # Insert the FILTER lines just before the final closing brace of the WHERE
        # block (the last '}' in the query). This keeps them inside the graph
        # pattern Ontop reformulates.
        close = rewritten.rfind("}")
        if close == -1:
            return sparql
        return (rewritten[:close] + "\n" + "\n".join(filters) + "\n"
                + rewritten[close:])
    except Exception:  # noqa: BLE001 — telemetry-grade safety; never break gen
        return sparql


def _desugar_numeric_aggregates(sparql: str) -> str:
    """Wrap bare-variable numeric aggregates in xsd:decimal() for Ontop.

    DETERMINISTIC backstop (mirrors :func:`_desugar_boolean_objects`): the prompt
    asks the model to cast numeric values inside SUM/AVG/MIN/MAX, but it often
    still emits a bare ``SUM(?amount)``. Ontop maps every column to VARCHAR, so a
    bare aggregate sums TEXT — the translated Athena SQL errors and the downstream
    LLM SQL-repair tends to rewrite it to COUNT non-numeric rows (the observed
    ``nonNumCount`` results on gt-03/gt-08) instead of casting, silently changing
    what the query computes. We rewrite ``SUM(?v)`` → ``SUM(xsd:decimal(?v))`` for
    every SUM/AVG/MIN/MAX (NOT COUNT — counting text is correct). The rewrite is
    applied everywhere the pattern appears (SELECT projection AND ORDER BY over an
    aggregate), and is idempotent: an already-cast ``SUM(xsd:decimal(?v))`` has no
    bare variable inside, so it does not re-match. Fail-soft: returns the input
    unchanged on any regex surprise.

    Args:
        sparql: The generated SPARQL query text.

    Returns:
        The query with numeric aggregates cast to xsd:decimal.
    """
    try:
        def _repl(m: "re.Match") -> str:
            # Preserve the original function spelling (SUM/sum) but normalise to a
            # cast inner expression.
            return f"{m.group('fn')}(xsd:decimal({m.group('var')}))"

        return _NUM_AGG_RE.sub(_repl, sparql)
    except Exception:  # noqa: BLE001 — telemetry-grade safety; never break gen
        return sparql


# Matches a SELECT-projection alias `(<expr> AS ?v)` where <expr> is a COMPUTED
# expression (contains a function call / paren), NOT a bare aggregate. Ontop cannot
# translate a computed expression that is then used in GROUP BY/ORDER BY directly in
# the projection — it must be bound via BIND in the WHERE clause first
# (e.g. gt-08: `SELECT (SUBSTR(?d,1,7) AS ?month) ... GROUP BY ?month` fails). We
# detect these and move them to a BIND. Aggregates (SUM/COUNT/AVG/MIN/MAX) are NOT
# moved — those are legitimately projected and must stay in SELECT.
_PROJ_ALIAS_RE = re.compile(
    r"\(\s*(?P<expr>(?![\s]*(?:SUM|COUNT|AVG|MIN|MAX)\s*\()[^()]*\([^()]*\)[^()]*)"
    r"\s+AS\s+(?P<var>\?[A-Za-z_]\w*)\s*\)",
    re.IGNORECASE,
)


def _desugar_computed_groupby(sparql: str) -> str:
    """Move a computed SELECT alias used in GROUP BY/ORDER BY into a WHERE-clause BIND.

    DETERMINISTIC backstop: the prompt tells the model to BIND a computed grouping
    key before grouping on it, but it often still emits
    ``SELECT (SUBSTR(?d,1,7) AS ?month) ... GROUP BY ?month`` — a computed expression
    in the projection that Ontop refuses to translate (gt-08). For each such
    ``(<expr> AS ?v)`` where ``<expr>`` is a function call (NOT a bare aggregate) and
    ``?v`` appears in a GROUP BY or ORDER BY clause, we:

      1. replace the projection ``(<expr> AS ?v)`` with the bare variable ``?v``; and
      2. inject ``BIND(<expr> AS ?v)`` just before the final ``}`` of the WHERE clause.

    Aggregate projections (``(SUM(?x) AS ?t)``) are left untouched — they belong in
    SELECT. Purely SPARQL-syntactic, layer-agnostic (no IRI matched by name). Fail-soft:
    returns the input unchanged on any surprise or when no movable alias is grouped on.

    Args:
        sparql: The generated SPARQL query text.

    Returns:
        The query with computed grouping keys moved to BIND, or the input unchanged.
    """
    try:
        binds: list = []

        def _maybe_move(m: "re.Match") -> str:
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
        # a trailing GROUP BY/ORDER BY/LIMIT after the brace is untouched.
        close = rewritten.rfind("}")
        if close == -1:
            return sparql
        return (rewritten[:close].rstrip() + "\n" + "\n".join(binds) + "\n"
                + rewritten[close:])
    except Exception:  # noqa: BLE001 — telemetry-grade safety; never break gen
        return sparql


# A tautological self-equality FILTER, e.g. `FILTER(?holdingId = ?holdingId)` — a
# no-op the model emits IN PLACE OF a real join key binding (the gt-03 tell). It
# never constrains anything, so stripping it is always safe; it also lets the
# disconnect check + regenerate see the query without the misleading pseudo-join.
_TAUTOLOGICAL_FILTER_RE = re.compile(
    r"\s*FILTER\s*\(\s*(\?[A-Za-z_]\w*)\s*=\s*\1\s*\)\s*", re.IGNORECASE
)


def _strip_tautological_filters(sparql: str) -> str:
    """Remove ``FILTER(?v = ?v)`` self-equalities (safe no-ops; the gt-03 tell)."""
    try:
        return _TAUTOLOGICAL_FILTER_RE.sub("\n", sparql)
    except Exception:  # noqa: BLE001 — never break gen on a regex surprise
        return sparql


# A pure variable-to-variable rename BIND, e.g. `BIND(?holdingId AS ?partyId)` — the
# right-hand side is a SINGLE bare variable, not an expression. The model emits this
# to FABRICATE a join: it renames one entity's id variable to another entity's id
# variable so the two look joined, when no real shared key exists (the gt-07 tell:
# `BIND(?holdingId AS ?partyId)` equated a holding id with a party id). A legitimate
# computed binding always wraps an EXPRESSION (a function call, CONCAT, arithmetic);
# a bare `BIND(?a AS ?b)` is never a real transform — it is either a redundant alias
# or this fabricated-join smell. We DETECT it (don't auto-rewrite — the right fix is
# a real join the model must supply) and trigger one regenerate with feedback.
_RENAME_BIND_RE = re.compile(
    r"BIND\s*\(\s*(?P<src>\?[A-Za-z_]\w*)\s+AS\s+(?P<dst>\?[A-Za-z_]\w*)\s*\)",
    re.IGNORECASE,
)


def detect_fabricated_rename_bind(sparql: str) -> bool:
    """True iff the query contains a pure variable-rename ``BIND(?a AS ?b)``.

    A bare variable-to-variable BIND (RHS is a single ``?var``, no expression) is
    never a legitimate computed binding — it is the model renaming one entity's key
    to another's to fake a join (gt-07: ``BIND(?holdingId AS ?partyId)``). Returns
    True so the generator can regenerate with feedback demanding a real join key.
    Computed BINDs over an EXPRESSION (``BIND(CONCAT(...) AS ?x)``,
    ``BIND(SUBSTR(...) AS ?m)``) do NOT match — their RHS is not a bare variable.
    Fail-soft: any regex surprise returns False (never block generation).

    Args:
        sparql: The generated SPARQL query text.

    Returns:
        True when a pure-rename BIND is present.
    """
    try:
        return _RENAME_BIND_RE.search(sparql) is not None
    except Exception:  # noqa: BLE001 — detection must never break generation
        return False


# A SPARQL query always begins with the prologue (BASE / PREFIX declarations)
# or, absent a prologue, a query form (SELECT / CONSTRUCT / ASK / DESCRIBE).
# Repair rounds sometimes prepend a prose preamble ("Looking at the question,
# here is the corrected query:") which rdflib rejects at char 0, tripping the
# degrade path even though a perfectly valid query follows. We anchor to the
# first of these keywords to trim any such leading prose.
_SPARQL_START_RE = re.compile(
    r"\b(?:PREFIX|BASE|SELECT|CONSTRUCT|ASK|DESCRIBE)\b", re.IGNORECASE
)


def _strip_fences(text: str) -> str:
    """Strip Markdown fences + prose preambles + surrounding whitespace.

    Two failure modes this repairs, both of which otherwise spuriously trip the
    Phase-4 repair/degrade path via an rdflib ``parseQuery`` syntax error:

    1. Models wrap SPARQL in ```` ```sparql … ``` ```` despite being told not to.
    2. On a repair round the model prepends a prose preamble (e.g. "Looking at
       the question, here is …") before the query. rdflib fails at char 0.

    Args:
        text: Raw LLM output that should contain a single SPARQL query.

    Returns:
        The SPARQL query text with fences and any leading prose removed.
    """
    out = text.strip()
    if out.startswith("```"):
        # Drop the opening fence line (``` or ```sparql) and a trailing fence.
        out = re.sub(r"^```[a-zA-Z]*\n?", "", out)
        out = re.sub(r"\n?```\s*$", "", out)
        out = out.strip()
    # Trim a leading prose preamble: if the text does not already begin with a
    # SPARQL keyword, cut everything before the first one. We only cut leading
    # prose (never mid-query content) — if no keyword is found we return as-is
    # so a genuinely malformed query still surfaces its real syntax error.
    match = _SPARQL_START_RE.search(out)
    if match and match.start() > 0:
        out = out[match.start():]
    return out.strip()


class VkgQueryGenerator:
    """Phase 3 SPARQL generator with one syntax-driven repair round."""

    def __init__(self, *, agent_factory: Callable[[], Any]) -> None:
        """Initialize the generator.

        Args:
            agent_factory: Zero-arg callable returning a Strands agent. A
                fresh agent is constructed per attempt so the repair attempt
                doesn't inherit the bad state from the first call.
        """
        self.agent_factory = agent_factory
        # Accumulated token usage across all agent calls in the most recent
        # generate() (initial + any repair). Read by Phase 4 to roll into the
        # workflow's running total. Reset at the start of each generate().
        self.last_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}

    def _accumulate_usage(self, result: Any) -> None:
        """Fold one agent call's usage into ``self.last_usage`` (best-effort)."""
        try:
            usage = result.metrics.accumulated_usage
        except AttributeError:
            return
        for key in self.last_usage:
            value = (usage.get(key) if isinstance(usage, dict)
                     else getattr(usage, key, None))
            if value is not None:
                self.last_usage[key] += int(value)

    def _ask(self, agent: Any, slice_text: str, question: str,
             repair: str = "", grounding_feedback: str = "") -> str:
        """Invoke ``agent`` with the slice + question prompt; return text.

        Args:
            agent: A fresh Strands agent for this attempt.
            slice_text: The serialized ontology slice (Turtle).
            question: The natural-language question.
            repair: An rdflib parse error from a prior attempt, if any.
            grounding_feedback: A grounding-gate hint naming IRIs the previous
                SPARQL used that are NOT valid in the slice — the model must
                rewrite using only slice classes/predicates.
        """
        suffix = ""
        if repair:
            suffix += f"\n\nPrevious attempt had syntax error: {repair}"
        if grounding_feedback:
            suffix += (
                f"\n\nIMPORTANT: your previous SPARQL referenced IRIs that are "
                f"NOT valid in the slice: {grounding_feedback}. Rewrite the "
                f"query using ONLY the classes and predicates present in the "
                f"slice above, applied to subjects of the correct class. Do not "
                f"invent predicates; if the slice lacks a predicate needed to "
                f"express a constraint, omit it or use one that IS in the slice."
            )
        prompt = f"# Slice\n{slice_text}\n\n# Question\n{question}{suffix}"
        result = agent(prompt)
        self._accumulate_usage(result)
        return _strip_fences(result.message["content"][0]["text"])

    def generate(self, *, slice_text: str, question: str,
                 grounding_feedback: str = "") -> str:
        """Return a syntactically-valid SPARQL string for ``question``.

        Args:
            slice_text: The serialized ontology slice (Turtle).
            question: The natural-language question.
            grounding_feedback: Optional hint from the Phase 5 grounding gate
                naming hallucinated/misused IRIs from a prior round so this
                regeneration avoids them.
        """
        self.last_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
        agent = self.agent_factory()
        sparql = self._ask(agent, slice_text, question,
                           grounding_feedback=grounding_feedback)
        sparql = self._finalize(sparql, slice_text, question, grounding_feedback)
        # Structural guard (R1): a pure-rename `BIND(?a AS ?b)` fabricates a join by
        # equating two entities' key variables (gt-07: BIND(?holdingId AS ?partyId)).
        # It parses fine but is semantically wrong, so detection + ONE regenerate with
        # generic feedback (no layer keys) is the right lever — we can't safely guess
        # the real join the model meant. Runs BEFORE the disconnect guard because a
        # rename-BIND often MASKS a disconnect (it pseudo-binds the missing key).
        try:
            fabricated = detect_fabricated_rename_bind(sparql)
        except Exception:  # noqa: BLE001 — detection must never break generation
            fabricated = False
        if fabricated:
            logger.info("phase4.rename_bind_guard: query renames one entity's key to "
                        "another's via BIND(?a AS ?b) (fabricated join) — regenerating "
                        "once | query=%r", sparql[:400])
            feedback = (
                "The previous query used `BIND(?a AS ?b)` to RENAME one entity's "
                "identifier into another entity's identifier variable. That fabricates "
                "a join that does not exist. Remove the rename. Join the two entities "
                "only on a REAL shared key — reuse the SAME variable in both patterns, "
                "or relate them through a predicate that EXISTS in the slice. If the "
                "slice has no predicate linking them, do not join them at all."
            )
            agent_r = self.agent_factory()
            regen = self._ask(agent_r, slice_text, question, repair=feedback,
                              grounding_feedback=grounding_feedback)
            try:
                regen = self._finalize(regen, slice_text, question, grounding_feedback)
                # Keep the regenerate only if it actually dropped the fabricated rename;
                # otherwise the original is no worse and we avoid a second bad variant.
                if not detect_fabricated_rename_bind(regen):
                    logger.info("phase4.rename_bind_guard: regenerate removed the "
                                "fabricated rename — using it.")
                    sparql = regen
                else:
                    logger.info("phase4.rename_bind_guard: regenerate STILL renames — "
                                "keeping original (Phase 5 is the backstop).")
            except SparqlSyntaxError:
                logger.info("phase4.rename_bind_guard: regenerate failed to validate — "
                            "keeping original.")
        # Connectivity guard (P2-1): a syntactically-valid query can still be a
        # CARTESIAN PRODUCT when two class-typed subjects are never joined (the
        # bridge key left unbound, a tautological FILTER standing in for the join —
        # the gt-03 shape). Detection is on the real rdflib algebra (fail-soft: a
        # None verdict = unanalyzable → don't touch). On a disconnect, do ONE
        # regenerate with generic connectivity feedback (NO layer-specific keys —
        # de-layering). If still disconnected, return as-is: there is no semantic
        # degrade channel here, and Phase 5 + the eval remain the backstop. The
        # tautological FILTER was already stripped in _finalize.
        try:
            disconnected = detect_disconnected_subjects(sparql)
        except Exception:  # noqa: BLE001 — detection must never break generation
            disconnected = None
        if disconnected is True:
            logger.info("phase4.disconnect_guard: query has unconnected class "
                        "subjects (cartesian risk) — regenerating once | query=%r",
                        sparql[:400])
            feedback = (
                "The previous query left two entity patterns UNCONNECTED, producing a "
                "cartesian product. Join every entity pattern on a shared key: reuse "
                "the SAME variable in the linked patterns (e.g. bind the bridge/foreign "
                "key variable in BOTH patterns), or add `FILTER(?a = ?b)` equating two "
                "DISTINCT key variables. Never use a self-equal `FILTER(?x = ?x)` — it "
                "is not a join. Use ONLY predicates present in the slice."
            )
            agent3 = self.agent_factory()
            regen = self._ask(agent3, slice_text, question, repair=feedback,
                              grounding_feedback=grounding_feedback)
            try:
                regen = self._finalize(regen, slice_text, question, grounding_feedback)
                # Keep the regenerate only if it actually reconnected; otherwise the
                # original is no worse and avoids a second cartesian variant.
                if detect_disconnected_subjects(regen) is not True:
                    logger.info("phase4.disconnect_guard: regenerate reconnected the "
                                "query — using it.")
                    return regen
                logger.info("phase4.disconnect_guard: regenerate STILL disconnected — "
                            "keeping original (Phase 5 is the backstop).")
            except SparqlSyntaxError:
                logger.info("phase4.disconnect_guard: regenerate failed to validate — "
                            "keeping original.")
        return sparql

    def _finalize(self, sparql: str, slice_text: str, question: str,
                  grounding_feedback: str) -> str:
        """Apply deterministic desugars + tautological-FILTER strip, validate, and
        run the existing single syntax-repair round. Raises ``SparqlSyntaxError`` if
        the repair also fails to parse (unchanged contract).
        """
        # Deterministic SHACL sh:in enforcement: when the question names a constrained
        # enum value and the property's class is bound, inject the FILTER the model
        # omitted. Replaces the old prose-based detect-and-regenerate guard.
        try:
            constraints = extract_enum_constraints(slice_text)
            sparql, injected = inject_enum_filters(sparql, question, constraints)
            if injected:
                logger.info("phase4.enum_filter: injected %d sh:in FILTER(s): %s",
                            len(injected), [i["value"] for i in injected])
        except Exception:  # noqa: BLE001 — enforcement must never break generation
            pass
        # Deterministic Ontop backstops (every mapped column is VARCHAR): rewrite a
        # bare `?x :flag false` to a string-tolerant FILTER, wrap bare numeric
        # aggregates `SUM(?v)` → `SUM(xsd:decimal(?v))`, move computed GROUP BY keys
        # to BIND, and drop tautological self-equality FILTERs. Applied before
        # validation so the returned/validated query is the safe form.
        sparql = _desugar_boolean_objects(sparql)
        sparql = _desugar_numeric_aggregates(sparql)
        sparql = _desugar_computed_groupby(sparql)
        sparql = _strip_tautological_filters(sparql)
        try:
            validate_sparql(sparql)
            return sparql
        except SparqlSyntaxError as e:
            logger.info("phase4.sparql_repair attempt — error=%s | query=%r",
                        e, sparql[:600])
            agent2 = self.agent_factory()
            repaired = self._ask(agent2, slice_text, question, repair=str(e),
                                 grounding_feedback=grounding_feedback)
            repaired = _desugar_boolean_objects(repaired)
            repaired = _desugar_numeric_aggregates(repaired)
            repaired = _desugar_computed_groupby(repaired)
            repaired = _strip_tautological_filters(repaired)
            try:
                validate_sparql(repaired)
            except SparqlSyntaxError as e2:
                logger.warning("phase4.sparql_repair FAILED — error=%s | query=%r",
                               e2, repaired[:600])
                raise
            return repaired
