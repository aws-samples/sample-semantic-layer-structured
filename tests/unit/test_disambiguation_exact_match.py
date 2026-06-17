"""Phase-2 disambiguation — an EXACT table-name match is strong lexical evidence
that should not be discarded for a modest embedding (cosine) score.

The 2026-06-08 curated-layer review found "What is the total market value of all
active holdings, grouped by party?" forking into a question-level clarification
offering five candidate tables, even though both head nouns matched a table by
exact name (``holdings`` → ``holding``, ``party`` → ``party``). The cause: a
top retrieval score below ``DISAMBIG_SCORE_FLOOR`` set ``low_confidence`` which
overrode the clean exact mappings. A lexical exact match is a stronger signal
than cosine proximity, so it must win.
"""
from __future__ import annotations

from agents.metadata_query_agent.tier2.disambiguation import analyze_terms


def _structured(score, *table_ids):
    """Phase-1 structured payload, one candidate per table id at a fixed score."""
    return {
        "candidates": [{"table_id": tid, "score": score} for tid in table_ids],
        "chunks_by_table": {tid: "" for tid in table_ids},
    }


def test_low_score_does_not_override_exact_name_mappings() -> None:
    # All scores below the 0.4 floor, but both head nouns match a table by exact
    # name. The query must proceed CLEAR, not fork a low-confidence clarification.
    structured = _structured(
        0.31, "normalized.holding", "normalized.coverage", "normalized.party",
        "normalized.party_banking", "normalized.party_license",
    )
    out = analyze_terms(
        question="What is the total market value of all active holdings, grouped by party?",
        structured=structured,
    )
    assert out["status"] == "CLEAR"
    assert out["can_proceed"]
    assert out["mappings"]["holdings"]["table"] == "holding"
    assert out["mappings"]["party"]["table"] == "party"


def test_low_score_still_clarifies_without_any_exact_match() -> None:
    # No question term names a table by exact match → the low-confidence
    # heuristic still fires (we have nothing lexical to trust).
    structured = _structured(0.31, "normalized.alpha", "normalized.beta")
    out = analyze_terms(question="show me the important stuff", structured=structured)
    assert out["status"] == "LOW_CONFIDENCE"
    assert not out["can_proceed"]


def test_low_score_fuzzy_only_match_still_clarifies() -> None:
    # A term that matches ONLY by the token/substring fallback (not an exact
    # table name) is weak evidence; combined with a low score it should still
    # clarify rather than silently bind a fuzzy guess.
    structured = _structured(0.31, "normalized.policy_product")
    out = analyze_terms(question="tell me about the policy", structured=structured)
    # "policy" matches "policy_product" only by substring → not exact → low conf.
    assert out["status"] == "LOW_CONFIDENCE"
    assert not out["can_proceed"]


def test_genuine_term_ambiguity_still_clarifies_even_with_exact_elsewhere() -> None:
    # An exact match on one term must NOT suppress a genuine multi-table
    # ambiguity on a DIFFERENT term — that is a real clarification, not a
    # low-confidence false alarm.
    structured = _structured(
        0.31, "normalized.holding", "dbA.codes", "dbB.codes",
    )
    out = analyze_terms(question="holding codes", structured=structured)
    assert out["status"] == "AMBIGUOUS"
    assert any(a["term"] == "codes" for a in out["ambiguities"])


def test_irregular_plural_parties_matches_party_table() -> None:
    # Regression for the "How many parties are there?" infinite clarification
    # loop (session 4c8a50c7): party scored 0.34 (< 0.4 floor), but "parties"
    # is the plural of the `party` table. A naive rstrip('s') made "partie"
    # which never matched → low_confidence fired forever. With proper -ies->y
    # inflection the head noun is an EXACT name match → suppresses low conf.
    structured = _structured(
        0.34, "normalized.party", "normalized.govt_id_info", "normalized.address",
    )
    out = analyze_terms(question="How many parties are there?", structured=structured)
    assert out["status"] == "CLEAR"
    assert out["can_proceed"]
    assert out["mappings"]["parties"]["table"] == "party"


def test_irregular_plural_addresses_matches_address_table() -> None:
    # "addresses" -> "address" via the -es rule (rstrip('s') gives "addresse").
    structured = _structured(0.31, "normalized.address", "normalized.party")
    out = analyze_terms(question="how many addresses do we store", structured=structured)
    assert out["status"] == "CLEAR"
    assert out["can_proceed"]
    assert out["mappings"]["addresses"]["table"] == "address"


def test_confirmed_pick_suppresses_low_confidence_clarification() -> None:
    # The user already chose "party" on a prior clarification. Even with no
    # lexical match (a pronoun-only question) and a sub-floor score, a confirmed
    # pick is a confident binding → proceed, do NOT re-clarify. This is what
    # breaks the low-confidence loop that pruning rivals alone cannot
    # (picking never raises the table's cosine score above the floor).
    structured = _structured(0.20, "normalized.party", "normalized.relation")
    out = analyze_terms(
        question="how many of them are there",
        structured=structured,
        resolved_names={"party"},
    )
    assert out["status"] == "CLEAR"
    assert out["can_proceed"]


def test_confirmed_pick_resolves_ambiguity_between_confirmed_candidates() -> None:
    # A term that is genuinely ambiguous between two same-name tables — but the
    # user already picked one of them. The pick must bind the term CLEAR, not
    # re-surface the same ambiguity.
    structured = _structured(0.31, "dbA.codes", "dbB.codes")
    out = analyze_terms(
        question="show me the codes",
        structured=structured,
        resolved_names={"codes"},
    )
    assert out["status"] == "CLEAR"
    assert out["can_proceed"]
    assert out["mappings"]["codes"]["source"] == "clarification"


def test_phrase_match_resolves_single_token_product_ambiguity() -> None:
    # "products" alone substring-matches coverage_product / policy_product /
    # invest_product → a spurious 3-way clarification. The adjacent phrase
    # "coverage products" names coverage_product uniquely, so the multi-token
    # pre-pass must bind it CLEAR (regression for the 2026-06-11 over-clarify).
    structured = _structured(
        0.6, "normalized.coverage_product", "normalized.policy_product",
        "normalized.invest_product", "normalized.coverage", "normalized.party",
    )
    out = analyze_terms(
        question="List the top 10 coverage products by name.",
        structured=structured,
    )
    assert out["status"] == "CLEAR"
    assert out["can_proceed"]
    assert out["mappings"]["products"]["table"] == "coverage_product"
    assert out["mappings"]["products"]["source"] == "phrase"


def test_phrase_match_resolves_financial_activity() -> None:
    # "activity" alone matches financial_/holding_/subaccount_/loan_activity →
    # spurious clarification. "financial activity" (hyphen normalized to a token
    # split) names financial_activity uniquely.
    structured = _structured(
        0.6, "normalized.financial_activity", "normalized.holding_activity",
        "normalized.subaccount_activity", "normalized.loan_activity",
        "normalized.party",
    )
    out = analyze_terms(
        question="What was the total financial activity amount per month in 2025?",
        structured=structured,
    )
    assert out["status"] == "CLEAR"
    assert out["can_proceed"]
    assert out["mappings"]["activity"]["table"] == "financial_activity"


def test_fuzzy_only_multimatch_defers_to_top_score_not_clarify() -> None:
    # "policies" (a generic head noun) substring-matches policy_product AND
    # policy_loan_summary — a purely-FUZZY multi-match (no table named "policy").
    # This must NOT fire a clarification; defer to Phase-1 ranking and bind the
    # top-scored candidate. Regression for the 2026-06-11 over-clarify on common
    # head nouns (policies / participants / hold).
    structured = {
        "candidates": [
            {"table_id": "normalized.policy_product", "score": 0.44},
            {"table_id": "normalized.policy_loan_summary", "score": 0.12},
            {"table_id": "normalized.coverage", "score": 0.34},
        ],
        "chunks_by_table": {},
    }
    out = analyze_terms(
        question="Show me policies where the insured party is also the policyholder.",
        structured=structured,
    )
    assert out["status"] == "CLEAR"
    assert out["can_proceed"]
    assert out["mappings"]["policies"]["table"] == "policy_product"  # top score
    assert out["mappings"]["policies"]["source"] == "fuzzy_top_score"


def test_exact_name_multimatch_across_dbs_still_clarifies() -> None:
    # Contrast: an EXACT table-name match under two databases is a genuine
    # federated-name collision and MUST still clarify (the fuzzy bypass above
    # must not swallow it).
    structured = {
        "candidates": [
            {"table_id": "dbA.codes", "score": 0.5},
            {"table_id": "dbB.codes", "score": 0.5},
        ],
        "chunks_by_table": {},
    }
    out = analyze_terms(question="show me the codes", structured=structured)
    assert out["status"] == "AMBIGUOUS"
    assert any(a["term"] == "codes" for a in out["ambiguities"])
