"""Phase 2 (VKG): SPARQL CONSTRUCT slice + judge loop + centrality truncation.

Centrality is computed on the slice subgraph (NOT the full ontology) so cost
stays bounded by slice size. Weighted degree = ``in_deg + out_deg``, plus a
+0.5 bonus for direct neighbors of Phase-1 candidates and +0.25 for nodes on
the shortest path between candidate pairs.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

import networkx as nx
from rdflib import Graph, URIRef
from rdflib.namespace import Namespace, OWL, RDF, RDFS

# SHACL namespace — used to walk shape closures so sh:in enum constraints
# survive centrality truncation (see _truncate_by_centrality).
SH = Namespace("http://www.w3.org/ns/shacl#")

logger = logging.getLogger(__name__)


class VkgSliceBuilder:
    """Phase 2 slice builder for VKG mode (``SliceBuilder`` protocol)."""

    def __init__(self, *, neptune,
                 judge_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
                 token_counter: Callable[[str], int],
                 budget: int, n_hops: int) -> None:
        """Initialize the builder.

        Args:
            neptune: Object with ``construct(candidates, n_hops, namespace)``
                returning an ``rdflib.Graph``.
            judge_fn: Callable invoked with ``{"slice", "question"}`` that
                returns ``{"sufficient": bool, "missing": list[str]}``.
            token_counter: Callable approximating tokens for a TTL string.
            budget: Soft token budget for the slice serialization.
            n_hops: SPARQL CONSTRUCT hop radius around each candidate.
        """
        self.neptune = neptune
        self.judge = judge_fn
        self.tokens = token_counter
        self.budget = budget
        self.n_hops = n_hops
        self._candidates: List[str] = []
        # Accumulated judge token usage across is_sufficient() calls; the
        # Phase 3 node rolls this into the workflow's running total (mirrors
        # RagSliceBuilder.judge_usage so the shared phase factory reads it).
        self.judge_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}

    def build(self, *, candidates: List[str], namespace: str) -> str:
        """Return an initial TTL slice for the Phase-1 ``candidates``."""
        self._candidates = list(candidates)
        graph: Graph = self.neptune.construct(
            candidates=candidates, n_hops=self.n_hops, namespace=namespace,
        )
        return self._fit_to_budget(graph)

    def expand(self, *, slice_text: str, missing: List[str]) -> str:
        """Re-construct around the union of prior candidates and ``missing``."""
        self._candidates = list(set(self._candidates) | set(missing))
        graph: Graph = self.neptune.construct(
            candidates=self._candidates, n_hops=self.n_hops, namespace="-",
        )
        return self._fit_to_budget(graph)

    def is_sufficient(self, *, slice_text: str, question: str
                      ) -> Tuple[bool, Optional[List[str]]]:
        """Ask the judge whether ``slice_text`` covers ``question``.

        The judge is handed a FLATTENED class/property/comment projection of the
        slice (``flatten_slice_for_judge``) rather than the raw Turtle the builder
        returns. Reasoning "is the property the question needs present?" over a
        compact ``Class: [prop (range) — comment]`` view is far more reliable for the
        judge LLM than over full-IRI triple syntax (the raw TTL drove the gt-00/01/04
        false-negative degrades). This is JUDGE-ONLY — the SPARQL generator still
        receives the raw TTL (it must copy full angle-bracketed IRIs verbatim), so the
        flattening never reaches generation. On a parse failure we fall back to the
        raw ``slice_text`` so the judge still sees something.
        """
        judge_view = flatten_slice_for_judge(slice_text) or slice_text
        verdict = self.judge({"slice": judge_view, "question": question})
        usage = verdict.get("usage") or {}
        # Accumulate every usage key the judge reports — including the cache
        # components Bedrock folds into totalTokens — so the running total stays
        # consistent with the in/out breakdown.
        for key, value in usage.items():
            self.judge_usage[key] = self.judge_usage.get(key, 0) + int(value or 0)
        return bool(verdict.get("sufficient")), verdict.get("missing")

    def _fit_to_budget(self, graph: Graph) -> str:
        """Serialize ``graph`` to TTL, truncating by centrality when oversize."""
        ttl = graph.serialize(format="turtle")
        if self.tokens(ttl) <= self.budget:
            return ttl
        truncated = _truncate_by_centrality(
            graph, candidates=self._candidates, budget_chars=self.budget * 4,
        )
        return truncated.serialize(format="turtle")


def _local_name(iri: str) -> str:
    """Return the local name of an IRI (after the last ``/`` or ``#``)."""
    s = str(iri)
    for sep in ("#", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    return s


def flatten_slice_for_judge(slice_text: str) -> str:
    """Project a Turtle slice into a compact class/property/comment view for the judge.

    The Phase-2 slice is raw Turtle (full IRIs, prefixes, triple syntax). The slice
    judge — an LLM deciding "are the classes/properties this question needs present?"
    — reasons far more reliably over a flattened view than over triple syntax, so this
    builds one generic, LAYER-AGNOSTIC block per class::

        ClassLocalName  <rdfs:comment of the class>
          - propLocalName (range: RangeLocalName)  <rdfs:comment of the property>

    It walks ONLY generic RDF/RDFS/OWL predicates (``rdf:type owl:Class`` /
    ``owl:DatatypeProperty`` / ``owl:ObjectProperty`` / ``rdf:Property``, plus
    ``rdfs:domain`` / ``rdfs:range`` / ``rdfs:comment``). No layer-specific IRI or
    predicate is matched by name, so this generalizes to every semantic layer. The
    FULL IRIs are deliberately dropped here — this view is for the judge only; the
    SPARQL generator still receives the raw Turtle (it must copy full IRIs verbatim).

    Args:
        slice_text: The serialized Turtle slice.

    Returns:
        The flattened text, or ``""`` if the Turtle cannot be parsed (caller falls
        back to the raw slice).
    """
    if not slice_text or not slice_text.strip():
        return ""
    g = Graph()
    try:
        g.parse(data=slice_text, format="turtle")
    except Exception:  # noqa: BLE001 — malformed slice → caller falls back to raw
        return ""

    def _comment(subj: URIRef) -> str:
        for _, _, o in g.triples((subj, RDFS.comment, None)):
            text = str(o).strip().replace("\n", " ")
            return f"  — {text}" if text else ""
        return ""

    # Collect class IRIs (owl:Class). Properties are grouped under their rdfs:domain
    # class; properties with no domain are listed under an "(unscoped)" bucket so the
    # judge still sees them.
    class_iris = {s for s in g.subjects(RDF.type, OWL.Class) if isinstance(s, URIRef)}
    prop_types = (OWL.DatatypeProperty, OWL.ObjectProperty, RDF.Property)
    prop_iris: set = set()
    for pt in prop_types:
        prop_iris |= {s for s in g.subjects(RDF.type, pt) if isinstance(s, URIRef)}

    by_class: Dict[str, List[str]] = {str(c): [] for c in class_iris}
    unscoped: List[str] = []
    for p in sorted(prop_iris, key=lambda x: _local_name(x).lower()):
        domains = [str(d) for d in g.objects(p, RDFS.domain) if isinstance(d, URIRef)]
        ranges = [_local_name(r) for r in g.objects(p, RDFS.range) if isinstance(r, URIRef)]
        rng = f" (range: {', '.join(ranges)})" if ranges else ""
        line = f"  - {_local_name(p)}{rng}{_comment(p)}"
        targets = [d for d in domains if d in by_class] or None
        if targets:
            for d in targets:
                by_class[d].append(line)
        else:
            unscoped.append(line)

    blocks: List[str] = []
    for c in sorted(by_class, key=lambda x: _local_name(x).lower()):
        header = f"{_local_name(c)}{_comment(URIRef(c))}"
        body = "\n".join(by_class[c]) if by_class[c] else "  (no datatype/object properties in slice)"
        blocks.append(f"{header}\n{body}")
    if unscoped:
        blocks.append("(properties not scoped to a class in this slice)\n" + "\n".join(unscoped))
    return "\n\n".join(blocks)


def _truncate_by_centrality(graph: Graph, *, candidates: List[str],
                            budget_chars: int) -> Graph:
    """Drop lowest-centrality nodes until the TTL fits under ``budget_chars``.

    Centrality is ``in_degree + out_degree`` with a +0.5 bonus for direct
    neighbors of any ``candidate`` and +0.25 for nodes on the shortest path
    between candidate pairs.

    The Phase-1 ``candidates`` (the classes/properties the question actually
    names) plus their OWN property IRIs (which nest as ``{classIri}/{prop}``) and
    their direct domain/range neighbors are **force-kept** regardless of budget —
    a centrality-only prefix could otherwise rank the question's own class out of
    a large 40-class ontology, leaving the slice judge to reject an answerable
    question forever (the VKG analog of the RAG _fit eviction bug).
    """
    g = nx.DiGraph()
    for s, p, o in graph:
        g.add_edge(str(s), str(o), pred=str(p))

    cand_set = set(candidates)
    all_nodes = set(g.nodes)
    cand_in_graph = cand_set & all_nodes
    # Force-keep: each candidate, its OWN property IRIs ({cand}/{prop}, present in
    # the graph), and its direct domain/range neighbors. These never get evicted.
    force_keep: set = set(cand_in_graph)
    for c in cand_in_graph:
        force_keep.update(g.successors(c))
        force_keep.update(g.predecessors(c))
        force_keep.update(n for n in all_nodes if n.startswith(f"{c}/"))

    # Force-keep the SHACL shape closure for every force-kept class so sh:in enum
    # constraints survive truncation. The shape node, its sh:property node, the
    # sh:path target, and the rdf:first/rdf:rest list-spine nodes (which are
    # low-degree leaves) would otherwise rank out of a large over-budget slice and
    # the edge filter would drop the spine when any list node is cut — leaving the
    # query side with no enum values. We walk the ORIGINAL rdflib ``graph`` (not the
    # nx ``g``) so we can follow the RDF-list spine via rdf:first/rdf:rest. Every
    # node is added as its ``str()`` form to match what _serialize_keep compares
    # (``str(s)``/``str(o)``), which also covers blank-node list spine nodes.
    def _list_nodes(g_rdf: Graph, head: Any) -> List[Any]:
        """Return every node on an RDF-list spine plus its rdf:first values.

        Args:
            g_rdf: the rdflib graph holding the list triples.
            head: the list head node (the object of sh:in).
        Returns:
            A list of spine nodes (rdf:rest chain) and their rdf:first values.
        """
        seen: List[Any] = []
        node = head
        while node is not None and node != RDF.nil:
            seen.append(node)
            first = g_rdf.value(node, RDF.first)
            if first is not None:
                seen.append(first)  # the value literal (or IRI) on this list cell
            node = g_rdf.value(node, RDF.rest)
            if node is not None:
                seen.append(node)
        return seen

    shape_keep: set = set()
    for cls in list(force_keep):
        cls_ref = URIRef(cls)
        for shape in graph.subjects(SH.targetClass, cls_ref):
            shape_keep.add(str(shape))
            # Keep the rdf:type objects of the shape (e.g. sh:NodeShape) so the
            # ``shape a sh:NodeShape`` triple survives the both-endpoints edge filter.
            for shape_type in graph.objects(shape, RDF.type):
                shape_keep.add(str(shape_type))
            for pshape in graph.objects(shape, SH.property):
                shape_keep.add(str(pshape))
                path = graph.value(pshape, SH.path)
                if path is not None:
                    shape_keep.add(str(path))
                lst = graph.value(pshape, SH["in"])
                if lst is not None:
                    for n in _list_nodes(graph, lst):
                        shape_keep.add(str(n))
    force_keep |= shape_keep

    scores: Dict[str, float] = {}
    neighbor_bonus: set = set()
    for c in cand_in_graph:
        neighbor_bonus.update(g.successors(c))
        neighbor_bonus.update(g.predecessors(c))
    for n in g.nodes:
        scores[n] = g.in_degree(n) + g.out_degree(n)
        if n in neighbor_bonus:
            scores[n] += 0.5

    # Shortest-path bonus between candidate pairs — compute the undirected view
    # ONCE (not per pair), and bound the work so a large schema graph can't
    # explode the O(pairs) loop.
    if 2 <= len(cand_in_graph) <= 12:
        ug = g.to_undirected()
        cand_list = list(cand_in_graph)
        for i, a in enumerate(cand_list):
            for b in cand_list[i + 1:]:
                try:
                    for n in nx.shortest_path(ug, a, b):
                        scores[n] = scores.get(n, 0) + 0.25
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue

    # Nodes ranked best-first, EXCLUDING the force-kept set (those are always in
    # ``keep``; the binary search only chooses how many of the REMAINING nodes
    # also fit). We keep a prefix of this ranking; binary-search the prefix length
    # so we serialize O(log n) times, not once per dropped node (the latter hung
    # on a ~650KB ontology graph — every iteration re-serialized the whole graph).
    ranked = [n for n in sorted(scores, key=lambda n: -scores[n])
              if n not in force_keep]

    def _keep_for(k: int) -> set:
        return force_keep | set(ranked[:k])

    def _serialize_keep(keep: set) -> str:
        out = Graph()
        for prefix, ns in graph.namespaces():
            out.bind(prefix, ns)
        for s, p, o in graph:
            if str(s) not in keep:
                continue
            if isinstance(o, URIRef) and str(o) not in keep:
                continue
            out.add((s, p, o))
        return out.serialize(format="turtle")

    # Largest prefix length whose TTL (force-kept set + prefix) fits the budget.
    lo, hi, best_k = 0, len(ranked), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(_serialize_keep(_keep_for(mid))) <= budget_chars:
            best_k, lo = mid, mid + 1
        else:
            hi = mid - 1

    keep = _keep_for(best_k)
    out = Graph()
    for prefix, ns in graph.namespaces():
        out.bind(prefix, ns)
    for s, p, o in graph:
        if str(s) not in keep:
            continue
        if isinstance(o, URIRef) and str(o) not in keep:
            continue
        out.add((s, p, o))
    return out
