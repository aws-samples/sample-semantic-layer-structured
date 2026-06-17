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

logger = logging.getLogger(__name__)

_GEN_PROMPT = (
    "You write SPARQL 1.1 SELECT queries against the provided ontology slice. "
    "Output ONLY the SPARQL query — no commentary, no markdown fences, no "
    "prefixes the slice doesn't already declare.\n\n"
    "# Aggregation + grouping (CRITICAL — the query is translated to SQL by Ontop)\n"
    "The SPARQL is reformulated to Athena SQL by Ontop, which does NOT support "
    "re-using a SELECT projection expression inside GROUP BY / ORDER BY. So NEVER "
    "write a computed expression directly in GROUP BY or ORDER BY (e.g. "
    "`GROUP BY (SUBSTR(?d,1,7))` or `ORDER BY (YEAR(?d))` fails to translate). "
    "Instead, COMPUTE the value once with BIND in the WHERE clause and bind it to "
    "a variable, then GROUP BY / ORDER BY / SELECT that VARIABLE. Pattern for a "
    "per-month bucket over a date/timestamp string column:\n"
    "  SELECT ?month (SUM(?amount) AS ?total)\n"
    "  WHERE {\n"
    "    ?x a ex:SomeClass ; ex:someDate ?d ; ex:someAmount ?amount .\n"
    "    BIND(SUBSTR(STR(?d), 1, 7) AS ?month)\n"
    "    FILTER(SUBSTR(STR(?d), 1, 4) = \"2024\")\n"
    "  }\n"
    "  GROUP BY ?month ORDER BY ?month\n"
    "Every variable in GROUP BY / ORDER BY must be a plain variable that is either "
    "bound by a triple pattern or by a BIND — not a bare function call. Apply the "
    "same rule to any derived grouping key (year, month, category bucket, rounded "
    "value): BIND it first, then group/order/select the variable.\n\n"
    "# Boolean / flag columns (CRITICAL — every mapped column is a STRING in Ontop)\n"
    "The relational columns are exposed to Ontop as VARCHAR, so an UNQUOTED boolean "
    "object never matches: `?x :is_deleted false` (xsd:boolean) compares against a "
    "string column and silently returns ~0 rows (e.g. a COUNT collapses to 1 "
    "instead of the true total). NEVER write an unquoted boolean (true/false) as a "
    "triple object or in a FILTER. To filter a soft-delete / active flag, bind the "
    "column and compare it to the STRING the column actually stores — and be "
    "tolerant of the stored form:\n"
    "  ?x :is_deleted ?del . FILTER(LCASE(STR(?del)) IN (\"false\", \"0\", \"f\"))\n"
    "If the question does NOT explicitly ask to exclude deleted/inactive rows, "
    "OMIT the soft-delete filter entirely rather than risk an over-restrictive "
    "string comparison — a plain `?party a :Party` COUNT is correct for "
    "\"how many parties are there\". Only add the flag filter when the question "
    "explicitly says 'active', 'non-deleted', 'current', etc."
)

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


def _desugar_boolean_objects(sparql: str) -> str:
    """Rewrite unquoted boolean triple-objects into string-tolerant FILTERs.

    DETERMINISTIC backstop for the LLM (the prompt asks it not to emit unquoted
    booleans, but it sometimes still does — e.g. `?p :is_deleted false`). Because
    Ontop maps every relational column to VARCHAR, a SPARQL `xsd:boolean` object
    never matches the stored string ("false"/"0"/"f"), silently collapsing a
    COUNT to ~0/1 instead of the true total. For each occurrence we:
      1. bind the predicate's value to a fresh variable on the SAME subject, and
      2. append a FILTER comparing the lower-cased string form to the matching
         truthy/falsy string set.
    The terminator after the object is preserved. Fresh var names are positional
    (``?_b0``, ``?_b1``…) so repeated predicates don't collide. No-op when there
    is no bare boolean object. Fail-soft: returns the input unchanged on any
    regex/format surprise (the prompt guidance remains the first line of defence).

    Args:
        sparql: The generated SPARQL query text.

    Returns:
        The query with boolean-object triples rewritten, plus appended FILTERs.
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

        rewritten = _BOOL_OBJ_RE.sub(_repl, sparql)
        if not filters:
            return sparql
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


def _strip_fences(text: str) -> str:
    """Strip Markdown code fences + surrounding whitespace from LLM output.

    Models frequently wrap SPARQL in ```` ```sparql … ``` ```` despite being
    told not to; rdflib ``parseQuery`` rejects the fences as a syntax error,
    which used to spuriously trip the Phase-4 repair/degrade path.
    """
    out = text.strip()
    if out.startswith("```"):
        # Drop the opening fence line (``` or ```sparql) and a trailing fence.
        out = re.sub(r"^```[a-zA-Z]*\n?", "", out)
        out = re.sub(r"\n?```\s*$", "", out)
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
        # Deterministic boolean-object backstop (Ontop maps columns to VARCHAR, so
        # a bare `?x :flag false` matches ~0 rows). Rewrite before validation so
        # the returned/validated query is the safe form.
        sparql = _desugar_boolean_objects(sparql)
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
            try:
                validate_sparql(repaired)
            except SparqlSyntaxError as e2:
                logger.warning("phase4.sparql_repair FAILED — error=%s | query=%r",
                               e2, repaired[:600])
                raise
            return repaired
