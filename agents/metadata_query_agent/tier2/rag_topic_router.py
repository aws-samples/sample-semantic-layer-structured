"""Phase 1 (RAG) — structured KB retrieval.

The router calls the namespace's Bedrock KB via ``retrieve_fn`` and returns the
de-duplicated table_ids ranked by score. ``column_id`` hits are kept on the
underlying structured payload (exposed via :pyattr:`last_structured`) so the
slice builder in Phase 2 can seed FK/glossary expansion off the column hits.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple


class RagTopicRouter:
    """Deduplicate structured KB retrieval candidates into a ranked table list."""

    def __init__(self, *, retrieve_fn: Callable[..., Dict[str, Any]],
                 kb_id_for: Callable[[str], str], top_k: int = 20,
                 score_floor: float = 0.10, min_candidates: int = 8) -> None:
        """Construct the router.

        Args:
            retrieve_fn: Callable returning ``{candidates, chunks}`` (typically
                :func:`agents.metadata_query_agent.main.retrieve_kb_context_structured`).
            kb_id_for: Maps a namespace id to its Bedrock Knowledge Base id.
            top_k: Forwarded to ``retrieve_fn`` as ``top_k`` kwarg.
            score_floor: Minimum KB relevance score (0-1) a candidate must clear
                to enter the slice. The KB routinely returns a long tail of
                weakly-related tables (scores < 0.05) that, when all assembled,
                overflow the slice token budget and force eviction of the
                genuinely-needed low-ranked table (the ``holding`` budget-eviction
                bug). Filtering the tail keeps the slice small enough to fit. Set
                to 0.0 to disable filtering.
            min_candidates: Always keep at least this many top-ranked candidates
                even if they fall below ``score_floor`` — a floor on the floor, so
                a question whose every match is weak still gets a slice to judge
                rather than an empty one.
        """
        self.retrieve = retrieve_fn
        self.kb_id_for = kb_id_for
        self.top_k = top_k
        self.score_floor = score_floor
        self.min_candidates = min_candidates
        self._last_structured: Dict[str, Any] = {}

    def find_candidates(self, *, question: str, namespace: str) -> List[str]:
        """Return unique table_ids ranked by Phase-1 KB retrieval score.

        Applies a relevance floor: candidates scoring below ``score_floor`` are
        dropped, but the top ``min_candidates`` are always retained regardless of
        score so a weakly-matched question still yields a non-empty slice. The
        de-duplicated, rank-ordered survivors are returned.
        """
        kb_id = self.kb_id_for(namespace)
        out = self.retrieve(user_query=question, kb_id=kb_id, top_k=self.top_k)
        self._last_structured = out
        seen: set = set()
        # (table_id, score) in rank order, de-duplicated on first (highest) hit.
        ranked: List[Tuple[str, float]] = []
        for cand in out.get('candidates', []):
            tid = cand.get('table_id')
            if tid and tid not in seen:
                seen.add(tid)
                ranked.append((tid, float(cand.get('score', 0.0) or 0.0)))
        # Keep every candidate clearing the floor; if too few pass, top up to
        # min_candidates from the highest-ranked of the rest (the list is already
        # in descending-score order from the retriever).
        kept = [tid for tid, score in ranked if score >= self.score_floor]
        if len(kept) < self.min_candidates:
            kept = [tid for tid, _ in ranked[:self.min_candidates]]
        return kept

    @property
    def last_structured(self) -> Dict[str, Any]:
        """Full structured payload from the most recent retrieval."""
        return self._last_structured

    def chunks_for(self, *, table_ids: List[str], namespace: str
                   ) -> Dict[str, str]:
        """Return ``{table_id: markdown_body}`` from the most recent retrieval.

        ``namespace`` is accepted for SliceBuilder protocol symmetry but
        ignored — the router caches the last KB call's payload and serves
        chunks from there. Tables that didn't surface in Phase 1 simply
        come back missing from the dict.
        """
        cached = self._last_structured.get('chunks_by_table', {}) or {}
        return {tid: cached[tid] for tid in table_ids if tid in cached}
