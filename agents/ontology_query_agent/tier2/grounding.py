"""Phase 5 grounding gate (VKG) — assert generated SPARQL is grounded in the slice.

The SPARQL analog of the RAG ``grounding.check_grounding``. It carries the same
two post-deploy lessons the RAG gate learned (see the design doc §0):

1. **Grounding is _qualified_, not flat set-membership.** A predicate IRI valid
   on class A is NOT automatically valid when used against class B. The RAG gate
   resolved each column's alias to its real table and checked it against THAT
   table's columns; the VKG gate parses each BGP triple's subject→predicate
   pairing and checks the predicate against its triple's subject class via the
   slice's ``rdfs:domain`` edges. Where the subject class can't be resolved
   (an unbound var with no ``rdf:type``), it falls back to slice-wide membership
   — best-effort and ambiguous by construction, exactly like an unqualified SQL
   column.

2. **A grounding miss routes via a hybrid back-edge.** Unlike SQL (a
   hallucinated column can't be conjured by widening the slice), VKG's slice is
   a Neptune CONSTRUCT bounded by ``n_hops`` — so a genuinely-existing predicate
   can legitimately sit *outside* the slice. :func:`classify_missing` splits the
   misses into an ``expand`` bucket (real-but-out-of-slice → Phase 3) and a
   ``regenerate`` bucket (hallucinated / misused → Phase 4).

On an ambiguous/failed parse (property paths, ``SERVICE``, complex ``VALUES``),
the gate returns ``[]`` — "ungrounded-but-unactionable → degrade, don't loop" —
mirroring the RAG gate's post-Phase-4 guard.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from rdflib import Graph, URIRef
from rdflib.namespace import RDF, RDFS
from rdflib.paths import Path
from rdflib.plugins.sparql import prepareQuery
from rdflib.plugins.sparql.parserutils import CompValue

logger = logging.getLogger(__name__)

# Marker joining a misused predicate to the class it was (wrongly) applied to,
# in a missing tag: ``property:<iri> on <class_iri>``. Chosen so the leading
# ``<iri>`` (which always contains ``://``) is still recoverable by splitting on
# the first ``:`` after the kind prefix.
_ON = " on "


def _slice_graph(slice_graph_or_text: Any) -> Graph:
    """Return an ``rdflib.Graph`` from either a Graph or a Turtle string."""
    if isinstance(slice_graph_or_text, Graph):
        return slice_graph_or_text
    g = Graph()
    if slice_graph_or_text:
        try:
            g.parse(data=slice_graph_or_text, format="turtle")
        except Exception as exc:  # noqa: BLE001 — a malformed slice grounds nothing
            logger.warning("grounding: slice TTL parse failed: %s", exc)
    return g


def _slice_lookups(graph: Graph) -> Tuple[Set[str], Dict[str, Set[str]], Dict[str, Set[str]]]:
    """Parse the slice graph into the grounding lookups.

    Returns ``(all_iris, domain_by_pred, subclass_parents)``:
      * ``all_iris`` — every IRI appearing anywhere in the slice (subject /
        predicate / object). Used for slice-wide membership (the fallback when a
        predicate's subject class is unresolvable, analogous to an unqualified
        SQL column).
      * ``domain_by_pred`` — ``{predicate_iri: {class_iri, ...}}`` from
        ``?p rdfs:domain ?c`` edges. Drives the qualified (triple-context)
        check. When the centrality truncation drops a predicate's domain edge,
        the predicate simply won't appear here and the gate degrades to
        slice-wide membership for it (false-negative-resistant, not a crash).
      * ``subclass_parents`` — ``{class_iri: {parent_iri, ...}}`` from
        ``?c rdfs:subClassOf ?p`` so a predicate whose domain is a superclass of
        the subject's class still grounds.
    """
    all_iris: Set[str] = set()
    domain_by_pred: Dict[str, Set[str]] = {}
    subclass_parents: Dict[str, Set[str]] = {}
    for s, p, o in graph:
        if isinstance(s, URIRef):
            all_iris.add(str(s))
        if isinstance(p, URIRef):
            all_iris.add(str(p))
        if isinstance(o, URIRef):
            all_iris.add(str(o))
        if p == RDFS.domain and isinstance(s, URIRef) and isinstance(o, URIRef):
            domain_by_pred.setdefault(str(s), set()).add(str(o))
        if p == RDFS.subClassOf and isinstance(s, URIRef) and isinstance(o, URIRef):
            subclass_parents.setdefault(str(s), set()).add(str(o))
    return all_iris, domain_by_pred, subclass_parents


def _prefix_header(graph: Graph) -> str:
    """Build ``PREFIX`` declarations for every namespace bound in the slice.

    Prepended to the query before parsing so prefixed names the generator used
    (relying on the slice's declared prefixes) resolve to full IRIs. Query-local
    declarations come after these, so a query that redeclares a prefix wins.
    """
    lines = []
    for prefix, ns in graph.namespaces():
        if prefix:  # skip the default ('') prefix to avoid a bare ``PREFIX :``
            lines.append(f"PREFIX {prefix}: <{ns}>")
    return "\n".join(lines)


def _walk_bgp_triples(node: Any, out: List[Tuple[Any, Any, Any]]) -> None:
    """Recursively collect every BGP triple in a parsed SPARQL algebra tree."""
    if isinstance(node, CompValue):
        if node.name == "BGP":
            out.extend(node.get("triples", []) or [])
        for value in node.values():
            _walk_bgp_triples(value, out)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _walk_bgp_triples(value, out)


def extract_sparql_iris(sparql: str, *, prefixes: Optional[Dict[str, str]] = None
                        ) -> Optional[Dict[str, Any]]:
    """Parse ``sparql`` and return its IRI sets + triple structure.

    Returns ``None`` when the query is unparseable or uses constructs the gate
    can't reason about safely (property paths, ``SERVICE``) — the caller treats
    that as "ungrounded-but-unactionable → degrade".

    Args:
        sparql: The SPARQL query (already syntax-validated by Phase 4).
        prefixes: ``{prefix: namespace}`` from the slice; emitted as ``PREFIX``
            lines before the query so prefixed names resolve.

    Returns:
        ``{"predicates": {iri, ...}, "classes": {iri, ...},
           "triples": [(subject_class_iri_or_None, predicate_iri), ...]}`` —
        the analog of the RAG gate's ``qualified`` list. ``triples`` pairs each
        non-type predicate with its subject's resolved class (from the same
        subject's ``rdf:type`` assertion) or ``None`` when unresolvable.
    """
    header = ""
    if prefixes:
        header = "\n".join(
            f"PREFIX {p}: <{ns}>" for p, ns in prefixes.items() if p
        )
    query_text = f"{header}\n{sparql}" if header else sparql
    try:
        prepared = prepareQuery(query_text)
    except Exception as exc:  # noqa: BLE001 — rdflib raises a grab-bag of types
        logger.info("grounding: SPARQL parse failed (degrade): %s", exc)
        return None

    triples: List[Tuple[Any, Any, Any]] = []
    _walk_bgp_triples(prepared.algebra, triples)

    # A property path predicate is a Path, not a URIRef — we can't pin it to a
    # single predicate IRI, so we degrade rather than risk a false reject.
    for _s, p, _o in triples:
        if isinstance(p, Path):
            logger.info("grounding: property path present (degrade)")
            return None

    # First pass: resolve each subject variable/IRI to its asserted class via
    # rdf:type triples on the same subject.
    subject_class: Dict[Any, str] = {}
    classes: Set[str] = set()
    for s, p, o in triples:
        if p == RDF.type and isinstance(o, URIRef):
            classes.add(str(o))
            subject_class[s] = str(o)

    # Second pass: collect predicate IRIs paired with their subject's class.
    predicates: Set[str] = set()
    pairs: List[Tuple[Optional[str], str]] = []
    for s, p, o in triples:
        if not isinstance(p, URIRef) or p == RDF.type:
            continue
        predicates.add(str(p))
        pairs.append((subject_class.get(s), str(p)))

    return {"predicates": predicates, "classes": classes, "triples": pairs}


def check_grounding(*, sparql: str, slice_graph_or_text: Any) -> List[str]:
    """Return the IRIs in ``sparql`` that are not grounded in the slice.

    An empty list means the SPARQL is fully grounded. A non-empty list contains
    tagged identifiers the Phase 5 node feeds into :func:`classify_missing`:

      * ``class:<iri>``            — a class IRI absent from the slice.
      * ``property:<iri>``         — a predicate IRI absent from the slice.
      * ``property:<iri> on <class_iri>`` — a predicate that EXISTS in the slice
        but whose ``rdfs:domain`` does not admit the triple's subject class
        (the §0.2 "valid on A, used on B" miss). Always a generation error.

    Grounding is **triple-context-aware**: a predicate is grounded only if its
    ``rdfs:domain`` (or, when no domain edge survives in the slice, slice-wide
    membership) admits the subject's class. On an unparseable / property-path /
    federated query, returns ``[]`` (degrade, don't loop).

    Args:
        sparql: The generated SPARQL.
        slice_graph_or_text: The Phase 3 slice as an ``rdflib.Graph`` or Turtle.
    """
    graph = _slice_graph(slice_graph_or_text)
    all_iris, domain_by_pred, subclass_parents = _slice_lookups(graph)
    prefixes = {p: str(ns) for p, ns in graph.namespaces() if p}

    extracted = extract_sparql_iris(sparql, prefixes=prefixes)
    if extracted is None:
        return []

    missing: List[str] = []
    seen: Set[str] = set()

    # Classes used in rdf:type must exist in the slice.
    for cls in sorted(extracted["classes"]):
        if cls not in all_iris:
            label = f"class:{cls}"
            if label not in seen:
                seen.add(label)
                missing.append(label)

    # Predicates: qualified (triple-context) check.
    for subject_cls, pred in extracted["triples"]:
        if pred not in all_iris:
            label = f"property:{pred}"
            if label not in seen:
                seen.add(label)
                missing.append(label)
            continue
        # Predicate exists in the slice. If we know both the subject's class and
        # the predicate's domain, the domain (or an ancestor of the subject
        # class) must admit it — otherwise it's a "valid on A, used on B" miss.
        domains = domain_by_pred.get(pred)
        if subject_cls and domains:
            admissible = set(domains)
            if not _class_admitted(subject_cls, admissible, subclass_parents):
                label = f"property:{pred}{_ON}{subject_cls}"
                if label not in seen:
                    seen.add(label)
                    missing.append(label)

    return missing


def _class_admitted(subject_cls: str, domains: Set[str],
                    subclass_parents: Dict[str, Set[str]]) -> bool:
    """Return True iff ``subject_cls`` or one of its ancestors is in ``domains``."""
    if subject_cls in domains:
        return True
    # Walk the subClassOf chain (bounded by the slice's edges; cycle-safe).
    seen: Set[str] = set()
    frontier = list(subclass_parents.get(subject_cls, set()))
    while frontier:
        cls = frontier.pop()
        if cls in domains:
            return True
        if cls in seen:
            continue
        seen.add(cls)
        frontier.extend(subclass_parents.get(cls, set()))
    return False


def _iri_of(tag: str) -> str:
    """Recover the offending IRI from a missing tag (``kind:<iri>[ on <cls>]``)."""
    # Strip the ``class:`` / ``property:`` kind prefix (split on first ':').
    rest = tag.split(":", 1)[1] if ":" in tag else tag
    # Drop a trailing `` on <class_iri>`` qualifier if present.
    if _ON in rest:
        rest = rest.split(_ON, 1)[0]
    return rest.strip()


def classify_missing(missing: List[str], *, candidates: List[str],
                     neptune_probe: Optional[Callable[[str], bool]] = None
                     ) -> Dict[str, List[str]]:
    """Split missing IRIs into ``expand`` (→ Phase 3) and ``regenerate`` (→ Phase 4).

    The §0.1 hybrid classifier:

      * An IRI that exists in the ontology but is out-of-slice — it was a Phase 1
        candidate, or a cheap bounded Neptune ``ASK`` (``neptune_probe``)
        confirms it — goes to the **expand** bucket (widen the slice).
      * A hallucinated IRI, or a misused-but-existing predicate
        (``property:<iri> on <class>``), goes to the **regenerate** bucket
        (rewrite the query). A misused predicate can't be fixed by expanding —
        the predicate already exists.

    With no probe available, anything not already a Phase 1 candidate goes to
    **regenerate** — the safe, non-spinning default (never loops the slice
    builder forever on a hallucination).

    Args:
        missing: The tags from :func:`check_grounding`.
        candidates: The Phase 1 candidate IRIs (the cheap out-of-slice signal).
        neptune_probe: Optional ``(iri) -> bool`` bounded existence check.

    Returns:
        ``{"expand": [iri, ...], "regenerate": [iri, ...]}`` (deduped, IRIs only).
    """
    candidate_set = set(candidates or [])
    expand: List[str] = []
    regenerate: List[str] = []
    seen_expand: Set[str] = set()
    seen_regen: Set[str] = set()

    for tag in missing:
        # A misused-but-existing predicate is a generation error — regenerate,
        # never expand (the predicate is already in the slice).
        if _ON in tag:
            iri = _iri_of(tag)
            if iri not in seen_regen:
                seen_regen.add(iri)
                regenerate.append(iri)
            continue
        iri = _iri_of(tag)
        out_of_slice = iri in candidate_set
        if not out_of_slice and neptune_probe is not None:
            try:
                out_of_slice = bool(neptune_probe(iri))
            except Exception as exc:  # noqa: BLE001 — a probe failure is non-fatal
                logger.warning("grounding: neptune_probe failed for %s: %s", iri, exc)
                out_of_slice = False
        if out_of_slice:
            if iri not in seen_expand:
                seen_expand.add(iri)
                expand.append(iri)
        else:
            if iri not in seen_regen:
                seen_regen.add(iri)
                regenerate.append(iri)

    return {"expand": expand, "regenerate": regenerate}
