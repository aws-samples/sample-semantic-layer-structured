"""Parse SHACL ``sh:in`` enum constraints from a Phase-3 ontology slice.

The query-side injector consumes the returned map to enforce closed value sets
on generated SQL/SPARQL. Mirrors the grounding gate's fail-soft contract: a
malformed slice enforces nothing (returns ``{}``) rather than raising.
"""

import logging
import re
from typing import Optional

from rdflib import Graph, Literal, Variable
from rdflib.collection import Collection
from rdflib.namespace import RDF, Namespace
from rdflib.plugins.sparql import prepareQuery

from .grounding import _slice_graph, _walk_bgp_triples, extract_sparql_iris

# SHACL vocabulary. Use ``SH["in"]`` rather than ``SH.in`` because ``in`` is a
# reserved Python keyword and attribute access would be a syntax error.
SH = Namespace("http://www.w3.org/ns/shacl#")

_LOGGER = logging.getLogger(__name__)


def extract_enum_constraints(slice_graph_or_text) -> dict:
    """Parse SHACL sh:in enum constraints from an ontology slice.

    Reads each sh:NodeShape with a sh:targetClass and a sh:property whose
    sh:path/sh:in document a closed value set, returning a map the query-side
    injector consumes. Fail-soft: returns {} on any parse error (mirrors the
    grounding gate's contract — a malformed slice enforces nothing).

    Args:
        slice_graph_or_text: the Phase-3 slice as an rdflib.Graph or Turtle text.
    Returns:
        {property_iri: {"class": class_iri, "values": [str, ...]}} — values in
        list order; {} when the slice has no parseable sh:in shape.
    """
    try:
        g: Graph = _slice_graph(slice_graph_or_text)
        out: dict = {}
        # Each closed value set is documented by a NodeShape bound to a class.
        for shape in g.subjects(RDF.type, SH.NodeShape):
            cls = g.value(shape, SH.targetClass)
            if cls is None:
                continue
            # A shape may carry several property shapes; only those with both a
            # path and an sh:in list describe an enum constraint.
            for pshape in g.objects(shape, SH.property):
                path = g.value(pshape, SH.path)
                lst = g.value(pshape, SH["in"])  # SH["in"]: "in" is a keyword.
                if path is None or lst is None:
                    continue
                # Collection iterates the RDF list in document order; keep only
                # literal members (the enum's allowed values).
                members = [
                    str(m) for m in Collection(g, lst) if isinstance(m, Literal)
                ]
                if members:
                    out[str(path)] = {"class": str(cls), "values": members}
        return out
    except Exception:  # noqa: BLE001 — a malformed slice enforces nothing
        _LOGGER.warning("extract_enum_constraints: unparseable slice", exc_info=True)
        return {}


# Modifier keywords that, if present, terminate the WHERE group — a deterministic
# FILTER must be injected BEFORE the first of these (and after the WHERE block's
# closing brace). Matched case-insensitively with word boundaries.
_MODIFIER_RE = re.compile(
    r"\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bOFFSET\b|\bHAVING\b",
    re.IGNORECASE,
)


def _object_var_for_predicate(sparql: str, prop_iri: str) -> Optional[str]:
    """Return the object variable name bound by ``prop_iri`` in a BGP triple.

    Re-walks the parsed SPARQL algebra (reusing grounding's ``_walk_bgp_triples``)
    because :func:`extract_sparql_iris` does not surface triple objects. For each
    BGP triple ``(s, p, o)`` whose predicate IRI equals ``prop_iri`` and whose
    object is an rdflib ``Variable``, returns the variable's name — WITHOUT the
    leading ``?`` (rdflib ``Variable.__str__`` yields e.g. ``"status"``), so the
    name agrees with the ``?{var}`` rendered into the FILTER and the idempotency
    regex.

    Args:
        sparql: The SPARQL query text (already syntax-validated upstream).
        prop_iri: The full predicate IRI to locate.

    Returns:
        The object variable name (no ``?``), or ``None`` when the predicate is
        absent, its object is not a variable, or the query fails to parse.
    """
    try:
        prepared = prepareQuery(sparql)
        triples: list = []
        _walk_bgp_triples(prepared.algebra, triples)
        for _s, p, o in triples:
            if str(p) == prop_iri and isinstance(o, Variable):
                return str(o)
        return None
    except Exception:  # noqa: BLE001 — fail-soft: an unparseable query injects nothing
        return None


def inject_enum_filters(sparql: str, question: str, constraints: dict) -> tuple:
    """Deterministically inject SHACL ``sh:in`` value FILTERs into SPARQL.

    For each enum constraint whose class is bound and whose property is used in
    the query, if the question names EXACTLY ONE of the enum's allowed values,
    appends a ``FILTER(?var = "Value")`` inside the WHERE group. This enforces a
    closed value set the generator may have omitted — without an LLM round-trip.

    Conservative by construction (never injects on ambiguity):
      * nested sub-SELECTs are skipped wholesale (variable scoping is unsafe to
        reason about with the simple closing-brace placement used here);
      * a constraint whose class is not bound, or whose property is not used, is
        skipped;
      * zero or ≥2 named values for one property → skipped (ambiguous intent);
      * an existing ``FILTER(?var = ...)`` on the same variable → skipped
        (idempotent — never double-injects).

    Fail-soft: any internal error returns ``(sparql, [])`` unchanged.

    Args:
        sparql: The generated SPARQL query.
        question: The user's natural-language question (matched case-folded,
            tokenized on ``[a-z0-9]+``).
        constraints: The map from :func:`extract_enum_constraints`,
            ``{property_iri: {"class": class_iri, "values": [str, ...]}}``.

    Returns:
        ``(modified_sparql, injected)`` where ``injected`` is a list of
        ``{"prop": property_iri, "var": var_name, "value": matched_value}`` dicts
        (empty when nothing was injected, in which case ``modified_sparql`` is the
        input unchanged).
    """
    try:
        # Guard nested SELECT: a sub-select inside WHERE has its own variable
        # scope; the brace-placement heuristic below would land the FILTER in the
        # wrong group. Simple + safe: any second SELECT means skip entirely.
        if sparql.upper().count("SELECT") > 1:
            return (sparql, [])

        info = extract_sparql_iris(sparql)
        if info is None:
            return (sparql, [])

        bound_classes = info["classes"]
        used_predicates = info["predicates"]
        # Tokenize the question into lowercase alphanumeric tokens for value match.
        tokens = set(re.findall(r"[a-z0-9]+", question.lower()))

        collected: list = []
        # Sorted for deterministic injection order across runs.
        for prop_iri, c in sorted(constraints.items()):
            if c["class"] not in bound_classes:
                continue
            if prop_iri not in used_predicates:
                continue
            named = [v for v in c["values"] if v.lower() in tokens]
            if len(named) != 1:  # zero or ambiguous (≥2) → don't inject.
                continue
            var = _object_var_for_predicate(sparql, prop_iri)
            if var is None:
                continue
            # Idempotency: skip if a value FILTER on this variable already exists.
            if re.search(rf'FILTER\s*\(\s*\?{re.escape(var)}\s*=', sparql):
                continue
            collected.append({"prop": prop_iri, "var": var, "value": named[0]})

        if not collected:
            return (sparql, [])

        filter_text = "".join(
            f' FILTER(?{c["var"]} = "{c["value"]}")' for c in collected
        )

        # Insertion point: the ``}`` that closes the WHERE group, BEFORE any
        # trailing solution modifier (GROUP BY / ORDER BY / LIMIT / ...).
        first_brace = sparql.find("{")
        mod_match = _MODIFIER_RE.search(sparql, first_brace + 1) if first_brace != -1 else None
        mod_pos = mod_match.start() if mod_match else len(sparql)
        close = sparql.rfind("}", 0, mod_pos)
        if close == -1:  # no closing brace before the modifier → can't place safely.
            return (sparql, [])

        modified = sparql[:close] + filter_text + " " + sparql[close:]
        return (modified, collected)
    except Exception:  # noqa: BLE001 — fail-soft: never break query generation
        _LOGGER.warning("inject_enum_filters: injection failed", exc_info=True)
        return (sparql, [])
