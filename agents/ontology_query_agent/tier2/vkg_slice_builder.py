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
        """Ask the judge whether ``slice_text`` covers ``question``."""
        verdict = self.judge({"slice": slice_text, "question": question})
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
