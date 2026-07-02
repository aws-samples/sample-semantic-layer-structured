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


def _term_names_class(term: str, local: str) -> bool:
    """True iff ``term`` NAMES the class ``local`` — exact, inflected, OR a
    name-component / shared stem.

    Broader than :func:`_term_matches` (which is inflection-only) so a question
    head noun is recognised as an entity reference even when it is only PART of a
    compound class name, or shares a stem with it:
      * 'product'  names 'coverageproduct' / 'policyproduct'  (component)
      * 'hold'     names 'holding'                            (stem prefix)
      * 'party'    names 'party'                              (exact, via _term_matches)
    Used ONLY for the head-noun deferral guard — recognising that a colliding
    predicate term is actually the user naming an entity, not choosing between
    unrelated attribute owners. Kept conservative: requires a ≥4-char alphabetic
    overlap so short/generic fragments ('id', 'is') don't spuriously match.

    Args:
        term: A single question term (already lower-cased by the caller's tokenizer).
        local: A class local name, lower-cased.

    Returns:
        True when ``term`` is an entity-naming reference to ``local``.
    """
    if _term_matches(term, local):
        return True
    t = (term or "").strip().lower()
    if len(t) < 4:
        return False
    # Component match: the term appears as a substring of the (compound) class
    # name, e.g. 'product' in 'coverageproduct'. Compound ontology class names
    # are concatenated CamelCase lower-cased here, so substring is the right test.
    if t in local:
        return True
    # Shared-stem match: term is a prefix of the class name or vice versa, after
    # trimming a trailing inflection (hold ↔ holding). Guards the 'hold'→'holding'
    # case where neither is a clean substring of an inflected form of the other.
    stem = t[:-3] if t.endswith("ing") else t
    return len(stem) >= 4 and (local.startswith(stem) or stem.startswith(local[:4]))


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
        # Head-noun deferral: when the term ALSO names (or is a name-component of)
        # a class in the slice (e.g. 'product' → CoverageProduct/PolicyProduct,
        # 'hold' → Holding), the user is naming an ENTITY, not choosing between
        # sibling attribute-bearing classes. A predicate collision on a head-noun
        # term is not a real user-facing choice — the generator binds the
        # attribute on the head entity's class. Asking "which interpretation of
        # 'product'?" is the gt-07 over-clarify. Skip.
        # NOTE: use _term_names_class (substring/stem aware), NOT _term_matches
        # (inflection-only) — 'product' is NOT an inflection of 'coverageproduct'
        # but IS a name-component of it, and 'hold' stems into 'holding'.
        if any(_term_names_class(term, _local_name(c)) for c in classes):
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
            # Multi-path resolution — NEVER escalate a graph-traversal choice to
            # the user. The flat-KB metadata agent never surfaces join-path choice
            # (its SQL generator just picks a path); the VKG agent must match that
            # behaviour or it spuriously clarifies questions the metadata agent
            # answers (gt-03 "which interpretation of holding…party?", gt-07).
            #
            # Pick the SHORTEST path (fewest hops = the canonical bridge); break a
            # tie DETERMINISTICALLY by the lexicographically-smallest local-name
            # sequence. Record it in ``resolved`` so the generator + the Phase-5
            # grounding gate (which rejects truly wrong joins) own the final
            # decision. A genuine wrong guess degrades through grounding, not
            # through an un-actionable "pick a path" prompt.
            min_len = min(len(p) for p in paths)
            shortest = sorted(
                (p for p in paths if len(p) == min_len),
                key=lambda p: [_local_name(n) for n in p],
            )
            resolved[f"{_local_name(a)}…{_local_name(b)}"] = "→".join(
                _local_name(n) for n in shortest[0]
            )
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
        # Cap path length so a dense slice can't explode the enumeration. A
        # cutoff of 3 keeps only the plausible canonical bridge(s) (A→bridge→B);
        # distant 4–6-hop detours are never the intended join and only add noise
        # to the shortest-path / tie ranking in the caller.
        return list(nx.all_simple_paths(graph, a, b, cutoff=3))
    except (nx.NodeNotFound, nx.NetworkXNoPath):
        return []
