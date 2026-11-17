"""Recall Phase 1 (Hybrid): KNN topic routing over the semantic-layer KB.

Copy-and-own of the RAG agent's ``rag_topic_router`` recall phase, re-shaped for
the Hybrid agent's vector-search seam. Where the RAG router forwarded a
structured ``retrieve_fn`` that returned pre-ranked ``candidates``, the Hybrid
router embeds the question itself and runs a KNN search, so the two injected
callables it depends on are:

* ``embed(question) -> List[float]`` — turns the NL question into a query vector.
* ``knn_search(*, vector, k) -> List[Dict]`` — returns the nearest KB neighbours,
  each a dict carrying an ``id`` (the ``{database}.{table}`` candidate id).

Both are injected so the router is unit-testable without Bedrock / a vector store.
This module depends on the standard library ONLY — no shared or sibling-agent
imports (the Hybrid copy-and-own isolation rule).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List


class TopicRouter:
    """Embed the question, KNN-search the KB, return ranked candidate table ids."""

    def __init__(self, *,
                 knn_search: Callable[..., List[Dict[str, Any]]],
                 embed: Callable[[str], List[float]],
                 k: int = 20) -> None:
        """Construct the router.

        Args:
            knn_search: Callable ``knn_search(*, vector, k) -> [{id, ...}]`` that
                returns the ``k`` nearest KB neighbours to ``vector`` in
                descending-similarity order. Each hit is a dict carrying at least
                an ``id`` key (the ``{database}.{table}`` candidate id). Injected
                so the router needs no live vector store under test.
            embed: Callable ``embed(question) -> List[float]`` that turns the NL
                question into the query vector fed to ``knn_search``. Injected so
                the router needs no live embedding model under test.
            k: Number of nearest neighbours to request from ``knn_search``.
        """
        self.knn_search = knn_search
        self.embed = embed
        self.k = k

    def find_candidates(self, *, question: str, namespace: str) -> List[str]:
        """Return unique candidate table ids ranked by KNN similarity.

        Embeds ``question`` into a query vector, runs the injected KNN search, and
        returns the hit ids de-duplicated on first (highest-ranked) occurrence so
        the router's relevance order is preserved for the downstream slice builder.

        Args:
            question: The natural-language user question to recall tables for.
            namespace: Semantic-layer namespace. Accepted for SliceBuilder-protocol
                symmetry; namespace scoping is the injected ``knn_search``'s job
                (it closes over the namespace's index), so it is NOT forwarded here
                — mirroring the RAG router's ``chunks_for`` namespace handling.

        Returns:
            De-duplicated candidate ``{database}.{table}`` ids in KNN rank order.
        """
        vector = self.embed(question)
        hits = self.knn_search(vector=vector, k=self.k) or []
        seen: set = set()
        candidates: List[str] = []
        # Preserve KNN rank order; keep only the first (highest-ranked) occurrence
        # of each id so a table surfacing on multiple column hits is not repeated.
        for hit in hits:
            candidate_id = hit.get("id")
            if candidate_id and candidate_id not in seen:
                seen.add(candidate_id)
                candidates.append(candidate_id)
        return candidates
