"""Phase 3b (VKG): slice-level disambiguation guard.

The VKG analog of the RAG ``slice_disambiguation``. Phase 2 disambiguation is
*term-level* (a word maps to >1 candidate class IRI). But the Phase 3
judge-expand loop and the Phase 5 grounding back-edge both *grow* the slice
afterward, which can introduce ambiguity Phase 2 never saw:

  * **property collision** — a predicate the question references is reachable
    from (has ``rdfs:domain`` on) >1 class in the slice: which class's instances
    did the user mean?
  * **multiple class-paths** — >1 distinct path connects the question's anchor
    classes through the slice's domain/range + subClassOf edges: which traversal
    did the user intend?

Placed on the ``Phase 3 → Phase 4`` edge, this guard re-runs on the initial
slice AND on every re-expansion. It resolves heuristically where the slice's own
subClassOf / domain-range edges disambiguate (e.g. a predicate whose competing
domain classes form a subclass chain → pick the most specific), and only
escalates genuinely unresolvable cases to a ``needs_clarification`` payload
(reusing the shared :func:`build_clarification` so the frontend shape is
byte-identical to the RAG agent's).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

import networkx as nx
from rdflib import Graph, URIRef
from rdflib.namespace import RDF, RDFS

try:
    from agents.shared.disambiguation_common import (
        _query_terms,
        inflection_variants,
    )
except ImportError:  # container path: agents/ is on PYTHONPATH
    from shared.disambiguation_common import (  # type: ignore
        _query_terms,
        inflection_variants,
    )

RDF_TYPE = RDF.type


def _local_name(iri: str) -> str:
    """Return the lower-cased local name of ``iri`` (after the last ``/`` / ``#``)."""
    tail = iri.rstrip("/#")
    for sep in ("#", "/"):
        if sep in tail:
            tail = tail.rsplit(sep, 1)[1]
    # Some demo ontologies use ``ex:Customer`` style IRIs with no slash/hash.
    if ":" in tail and "://" not in tail:
        tail = tail.rsplit(":", 1)[1]
    return tail.lower()


def parse_slice_graph(slice_graph_or_text: Any) -> Graph:
    """Return an ``rdflib.Graph`` from either a Graph or a Turtle string."""
    if isinstance(slice_graph_or_text, Graph):
        return slice_graph_or_text
    g = Graph()
    if slice_graph_or_text:
        try:
            g.parse(data=slice_graph_or_text, format="turtle")
        except Exception:  # noqa: BLE001 — a malformed slice yields no ambiguity
            return Graph()
    return g


def _term_matches(term: str, local: str) -> bool:
    """True iff ``term`` matches ``local`` (across number inflection).

    Uses :func:`inflection_variants` so an irregular plural like ``policies``
    matches the ``Policy`` class — a naive ``rstrip('s')`` produced ``policie``
    and silently missed.
    """
    return local in inflection_variants(term)


def _is_descendant(cls: str, ancestor: str, parents: Dict[str, Set[str]]) -> bool:
    """True iff ``cls`` is a (transitive) subclass of ``ancestor`` per ``parents``."""
    seen: Set[str] = set()
    frontier = list(parents.get(cls, set()))
    while frontier:
        cur = frontier.pop()
        if cur == ancestor:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        frontier.extend(parents.get(cur, set()))
    return False


def _most_specific(classes: List[str], parents: Dict[str, Set[str]]) -> Optional[str]:
    """Return the single most-specific class if the set is a subclass chain.

    A class is "most specific" when every other class in the set is one of its
    ancestors. Returns ``None`` when the classes don't collapse to one leaf
    (i.e. they're genuinely competing, not a refinement chain).
    """
    for candidate in classes:
        others = [c for c in classes if c != candidate]
        if all(_is_descendant(candidate, other, parents) for other in others):
            return candidate
    return None


def find_slice_ambiguities(*, question: str, slice_graph: Any) -> Dict[str, Any]:
    """Detect slice-level ambiguity a term-level pass cannot see.

    Args:
        question: The natural-language user question.
        slice_graph: The Phase 3 slice as an ``rdflib.Graph`` or Turtle string.

    Returns:
        ``{"ambiguous": bool, "items": [...], "resolved": {term: iri}}``.
        ``items`` lists unresolved collisions (each with ``term`` + ``matches``
        of ``{table, database, column}`` where ``table`` is a class local name);
        ``resolved`` records collisions the subClassOf / path structure
        disambiguated so the Phase 4 prompt can pin the binding.
    """
    graph = parse_slice_graph(slice_graph)
    terms = set(_query_terms(question))

    # Build lookups from the slice graph.
    domain_by_pred: Dict[str, Set[str]] = {}        # pred IRI -> {class IRI}
    range_by_pred: Dict[str, Set[str]] = {}         # pred IRI -> {class IRI}
    parents: Dict[str, Set[str]] = {}               # class IRI -> {parent IRI}
    classes: Set[str] = set()
    for s, p, o in graph:
        if p == RDFS.domain and isinstance(s, URIRef) and isinstance(o, URIRef):
            domain_by_pred.setdefault(str(s), set()).add(str(o))
            classes.add(str(o))
        elif p == RDFS.range and isinstance(s, URIRef) and isinstance(o, URIRef):
            range_by_pred.setdefault(str(s), set()).add(str(o))
            classes.add(str(o))
        elif p == RDFS.subClassOf and isinstance(s, URIRef) and isinstance(o, URIRef):
            parents.setdefault(str(s), set()).add(str(o))
            classes.add(str(s))
            classes.add(str(o))
        elif p == RDF_TYPE and isinstance(s, URIRef):
            classes.add(str(s))

    items: List[Dict[str, Any]] = []
    resolved: Dict[str, str] = {}

    # Generic descriptive-attribute terms that EVERY entity carries (a
    # name/label/description). When the colliding predicate is one of these, the
    # user is NOT choosing between entities — they named the head entity
    # elsewhere in the question (e.g. "the policyholder's NAME", "coverage
    # products by NAME") and "name" is just the attribute to return. Clarifying
    # "which interpretation of 'name'?" is un-actionable. Kept deliberately
    # NARROW: only the universal human-readable-label attributes — NOT
    # entity-discriminating measures like "amount"/"date"/"type", which CAN be a
    # genuine cross-entity choice (e.g. Order.amount vs Payment.amount) and must
    # still clarify when their domains are unrelated.
    _GENERIC_ATTRS = {
        "name", "names", "label", "labels", "description", "descriptions",
    }

    # --- property collisions: a question-term predicate with domain on >1 class.
    for term in terms:
        owners: Set[str] = set()
        for pred, domains in domain_by_pred.items():
            if _term_matches(term, _local_name(pred)) and len(domains) > 1:
                owners |= domains
        if len(owners) <= 1:
            continue
        # Heuristic: if the competing domain classes form a subclass chain, the
        # most-specific class is the intended one — resolve without asking.
        # (Runs BEFORE the generic-attr skip so a resolvable chain is still
        # recorded in ``resolved`` for the generator.)
        leaf = _most_specific(sorted(owners), parents)
        if leaf is not None:
            resolved[term] = leaf
            continue
        # Generic-attribute deferral: a name/label/description spread across
        # UNRELATED classes is a descriptive column, not an entity choice — do
        # NOT escalate. The SPARQL generator selects the attribute on whichever
        # entity class the question's real head noun resolved to.
        if term.lower() in _GENERIC_ATTRS:
            continue
        matches = [{"table": _local_name(c), "database": "", "column": term}
                   for c in sorted(owners)]
        items.append({"term": term, "matches": matches})

    # --- multiple class-paths between the question's anchor classes.
    anchors = sorted(
        c for c in classes
        if any(_term_matches(t, _local_name(c)) for t in terms)
    )
    if len(anchors) >= 2:
        undirected = _class_adjacency(domain_by_pred, range_by_pred, parents)
        a, b = anchors[0], anchors[1]
        paths = _simple_paths(undirected, a, b)
        if len(paths) > 1:
            matches = [
                {"table": "→".join(_local_name(n) for n in path),
                 "database": "", "column": ""}
                for path in paths
            ]
            items.append({"term": f"{_local_name(a)}…{_local_name(b)}",
                          "matches": matches})
        # Exactly one path is unambiguous — nothing to record (the generator
        # will discover the lone path itself); zero paths means the anchors are
        # disconnected, also not a *choice* to surface.

    return {"ambiguous": bool(items), "items": items, "resolved": resolved}


def _class_adjacency(domain_by_pred: Dict[str, Set[str]],
                     range_by_pred: Dict[str, Set[str]],
                     parents: Dict[str, Set[str]]) -> "nx.Graph":
    """Build an undirected class graph from domain↔range pairings + subClassOf.

    Two classes are adjacent when some predicate has one as ``rdfs:domain`` and
    the other as ``rdfs:range`` (a traversable edge), or when one is a direct
    ``rdfs:subClassOf`` of the other.
    """
    g = nx.Graph()
    for pred, domains in domain_by_pred.items():
        ranges = range_by_pred.get(pred, set())
        for d in domains:
            for r in ranges:
                if d != r:
                    g.add_edge(d, r)
    for cls, ps in parents.items():
        for p in ps:
            g.add_edge(cls, p)
    return g


def _simple_paths(graph: "nx.Graph", a: str, b: str) -> List[List[str]]:
    """Return up to a bounded number of simple paths between ``a`` and ``b``."""
    if a not in graph or b not in graph:
        return []
    try:
        # Cap path length so a dense slice can't explode the enumeration.
        return list(nx.all_simple_paths(graph, a, b, cutoff=6))
    except (nx.NodeNotFound, nx.NetworkXNoPath):
        return []
