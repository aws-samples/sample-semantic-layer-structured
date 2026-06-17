"""Phase-2 memory recall — ``analyze_terms`` consults a recall resolver to
settle an ambiguous term from a user's prior-session lessons before clarifying.
"""
from __future__ import annotations

from agents.metadata_query_agent.tier2.disambiguation import analyze_terms


def _structured(*table_ids):
    """Phase-1 structured payload with one candidate per table id, equal score."""
    return {
        "candidates": [{"table_id": tid, "score": 0.9} for tid in table_ids],
        "chunks_by_table": {tid: "" for tid in table_ids},
    }


def test_ambiguous_term_without_recall_stays_ambiguous() -> None:
    # Bare name "codes" appears under two databases → AMBIGUOUS, no resolver.
    structured = _structured("dbA.codes", "dbB.codes")
    out = analyze_terms(question="how many codes", structured=structured)
    assert out["status"] == "AMBIGUOUS"
    assert not out["can_proceed"]


def test_recall_resolves_ambiguous_term() -> None:
    structured = _structured("dbA.codes", "dbB.codes")

    # Resolver picks dbA.codes for the term "codes" (as a prior session did).
    def resolver(term, candidate_ids):
        assert term == "codes"
        assert set(candidate_ids) == {"dbA.codes", "dbB.codes"}
        return "dbA.codes"

    out = analyze_terms(question="how many codes", structured=structured,
                        recall_resolver=resolver)
    assert out["status"] == "CLEAR"
    assert out["can_proceed"]
    assert out["mappings"]["codes"]["table"] == "codes"
    assert out["mappings"]["codes"]["database"] == "dbA"
    assert out["mappings"]["codes"]["source"] == "memory"


def test_recall_miss_falls_through_to_clarification() -> None:
    structured = _structured("dbA.codes", "dbB.codes")
    # Resolver returns None (memory silent) → term stays ambiguous.
    out = analyze_terms(question="how many codes", structured=structured,
                        recall_resolver=lambda t, c: None)
    assert out["status"] == "AMBIGUOUS"


def test_recall_exception_is_swallowed() -> None:
    structured = _structured("dbA.codes", "dbB.codes")

    def boom(term, candidate_ids):
        raise RuntimeError("recall blew up")

    # A resolver error must not break Phase 2 — fall through to AMBIGUOUS.
    out = analyze_terms(question="how many codes", structured=structured,
                        recall_resolver=boom)
    assert out["status"] == "AMBIGUOUS"
