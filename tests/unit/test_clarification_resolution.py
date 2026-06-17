"""Unit tests for ``agents.shared.clarification`` + Phase-1 candidate pruning.

Covers the resolution-reply matcher (exact id, exact label, whole-word local
name, zero/ambiguous → None), the pending-record builder + loader, and
``apply_clarification_resolution``'s candidate pruning for both RAG
(``database.table``) and VKG (IRI) candidate-id shapes.

These exercise the bug from the screenshot: a follow-up "party_banking" reply to
a "Which interpretation of 'party'?" clarification must resolve to a single
table and prune the rival so Phase 2 no longer re-fires the clarification.
"""
from __future__ import annotations

import pytest

import json

from agents.shared.clarification import (
    CLARIFICATION_RECORD_MAX_BYTES,
    CLARIFICATION_TOTALS_KEY,
    ClarificationResolution,
    ResolvedChoice,
    build_pending_clarification,
    load_pending_clarification,
    local_name,
    resolve_clarification_reply,
)
from agents.shared.tier2_graph import (
    WorkflowContext,
    apply_clarification_resolution,
)


# --- local_name normalisation --------------------------------------------
@pytest.mark.parametrize("identifier,expected", [
    ("normalized.party_banking", "party_banking"),       # RAG db.table
    ("party_banking", "party_banking"),                  # bare option id
    ("http://example.org/onto#EmailMessage", "emailmessage"),  # IRI fragment
    ("http://example.org/onto/EmailCampaign", "emailcampaign"),  # IRI path
    ("", ""),
])
def test_local_name(identifier: str, expected: str) -> None:
    assert local_name(identifier) == expected


# --- build_pending_clarification ------------------------------------------
def test_build_pending_clarification_packs_question_and_options() -> None:
    payload = {
        "needs_clarification": True,
        "clarification_question": "Which interpretation of 'party' do you mean?",
        "options": [
            {"id": "party_license", "label": "party_license (database: normalized)"},
            {"id": "party_banking", "label": "party_banking (database: normalized)"},
        ],
    }
    record = build_pending_clarification(
        original_question="List the top 5 party types and their descriptions.",
        payload=payload,
    )
    assert record["original_question"].startswith("List the top 5 party types")
    assert [o["id"] for o in record["options"]] == ["party_license", "party_banking"]


def test_build_pending_clarification_drops_idless_options() -> None:
    record = build_pending_clarification(
        original_question="q",
        payload={"options": [{"label": "no id"}, {"id": "x", "label": "X"}]},
    )
    assert [o["id"] for o in record["options"]] == ["x"]


# --- load_pending_clarification -------------------------------------------
def _clarify_record() -> dict:
    return {
        "original_question": "List the top 5 party types and their descriptions.",
        "options": [
            {"id": "party_license", "label": "party_license (database: normalized)"},
            {"id": "party_banking", "label": "party_banking (database: normalized)"},
        ],
    }


def test_load_pending_returns_record_from_latest_assistant_turn() -> None:
    history = [
        {"role": "user", "text": "List the top 5 party types and their descriptions."},
        {"role": "assistant", "text": "Which interpretation of 'party'?",
         "totals": {CLARIFICATION_TOTALS_KEY: _clarify_record()}},
    ]
    record = load_pending_clarification(history)
    assert record is not None
    assert record["original_question"].startswith("List the top 5 party types")


def test_load_pending_none_when_latest_assistant_turn_is_normal_answer() -> None:
    # A normal answered turn after the clarification ends the clarification flow.
    history = [
        {"role": "assistant", "text": "Which interpretation?",
         "totals": {CLARIFICATION_TOTALS_KEY: _clarify_record()}},
        {"role": "user", "text": "party_banking"},
        {"role": "assistant", "text": "There are 5.",
         "totals": {"sql": "SELECT 1", "rowCount": 5}},
    ]
    assert load_pending_clarification(history) is None


def test_load_pending_none_on_empty_history() -> None:
    assert load_pending_clarification([]) is None
    assert load_pending_clarification(None) is None


# --- resolve_clarification_reply ------------------------------------------
def test_resolve_exact_id_match() -> None:
    res = resolve_clarification_reply(reply="party_banking", pending=_clarify_record())
    assert isinstance(res, ClarificationResolution)
    assert res.chosen_ids == ["party_banking"]
    assert res.rival_ids == ["party_license"]
    assert res.original_question.startswith("List the top 5 party types")


def test_resolve_exact_label_match() -> None:
    res = resolve_clarification_reply(
        reply="party_banking (database: normalized)", pending=_clarify_record())
    assert res is not None
    assert res.chosen_ids == ["party_banking"]


def test_resolve_whole_word_local_name_in_sentence() -> None:
    res = resolve_clarification_reply(
        reply="I mean party_banking please", pending=_clarify_record())
    assert res is not None
    assert res.chosen_ids == ["party_banking"]


def test_resolve_none_when_reply_matches_no_option() -> None:
    res = resolve_clarification_reply(reply="something else", pending=_clarify_record())
    assert res is None


def test_resolve_none_when_reply_matches_multiple_options() -> None:
    # A reply naming BOTH options is ambiguous — must not force-pick one.
    res = resolve_clarification_reply(
        reply="party_license or party_banking?", pending=_clarify_record())
    assert res is None


def test_resolve_none_when_no_pending() -> None:
    assert resolve_clarification_reply(reply="party_banking", pending=None) is None
    assert resolve_clarification_reply(reply="party_banking", pending={}) is None


# --- apply_clarification_resolution (Phase 1 pruning) ---------------------
def test_apply_prunes_rival_rag_table_candidates() -> None:
    ctx = WorkflowContext(question="q", namespace="ns")
    ctx.candidates = ["normalized.party_license", "normalized.party_banking",
                      "normalized.address"]
    ctx.clarification_resolution = ClarificationResolution(
        original_question="q", chosen_ids=["party_banking"],
        rival_ids=["party_license"],
    )
    apply_clarification_resolution(ctx)
    # party_license (a rival) pruned; chosen + unrelated candidates kept.
    assert ctx.candidates == ["normalized.party_banking", "normalized.address"]


def test_apply_prunes_rival_vkg_iri_candidates() -> None:
    ctx = WorkflowContext(question="q", namespace="ns")
    ctx.candidates = ["http://ex.org/o#EmailMessage",
                      "http://ex.org/o#EmailCampaign"]
    ctx.clarification_resolution = ClarificationResolution(
        original_question="q", chosen_ids=["EmailMessage"],
        rival_ids=["EmailCampaign"],
    )
    apply_clarification_resolution(ctx)
    assert ctx.candidates == ["http://ex.org/o#EmailMessage"]


def test_apply_seeds_chosen_table_absent_from_retrieval() -> None:
    # Real failure (nb2 2026-06-12): the clarified re-run question is the bare
    # original ("How many are there?"), which has no noun — so Phase 1 retrieval
    # surfaces party-unrelated tables and `party` (the user's choice) is absent.
    # Pruning alone leaves the chosen table missing → degrade, even though
    # "How many parties are there?" asked directly succeeds. apply_* must SEED
    # the chosen table (reconstructing db from a sibling candidate).
    ctx = WorkflowContext(question="How many are there?", namespace="ns")
    ctx.candidates = ["normalized.address", "normalized.relation"]  # no party
    ctx.clarification_resolution = ClarificationResolution(
        original_question="How many are there?", chosen_ids=["party"],
        rival_ids=["address", "relation"],
    )
    apply_clarification_resolution(ctx)
    # party seeded with the normalized.* db prefix, leading the order.
    assert "normalized.party" in ctx.candidates
    assert ctx.candidates[0] == "normalized.party"


def test_apply_seeds_chosen_vkg_iri_absent_from_retrieval() -> None:
    # Real failure (nb6 2026-06-14): on the VKG path the chosen option id is a
    # full class IRI. The bare original re-run ("How many are there?" → "party")
    # left Party absent from retrieval (only Relation, which carries party_*
    # columns, surfaced), so the agent counted Relation. The seed must inject the
    # chosen CLASS as a real IRI (verbatim) — NOT a bare "party" token (which the
    # slice CONSTRUCT can't fetch) and NOT a "db.party" reconstruction.
    party_iri = "http://ex.org/ontology/L/Party"
    relation_iri = "http://ex.org/ontology/L/Relation"
    ctx = WorkflowContext(question="How many are there?", namespace="ns")
    ctx.candidates = [relation_iri]  # Party absent from retrieval
    ctx.clarification_resolution = ClarificationResolution(
        original_question="How many are there?",
        chosen_ids=[party_iri],          # full IRI option id (VKG)
        rival_ids=[relation_iri],
    )
    apply_clarification_resolution(ctx)
    # The chosen Party IRI is seeded VERBATIM and leads the order; no bare
    # "party" token and no "db.party" appears.
    assert party_iri in ctx.candidates
    assert ctx.candidates[0] == party_iri
    assert "party" not in ctx.candidates  # not the un-fetchable bare token
    assert not any(c.endswith(".party") for c in ctx.candidates)


def test_apply_noop_without_resolution() -> None:
    ctx = WorkflowContext(question="q", namespace="ns")
    ctx.candidates = ["normalized.party_license", "normalized.party_banking"]
    apply_clarification_resolution(ctx)  # clarification_resolution is None
    assert ctx.candidates == ["normalized.party_license", "normalized.party_banking"]


def test_apply_seeds_choice_when_pruning_would_empty_candidates() -> None:
    # Fail-soft AND correct: pruning the only candidate (a rejected rival) would
    # strand the query, so the prune is skipped — but the user's explicit choice
    # (party_banking, which retrieval never surfaced) is then SEEDED and leads
    # the order, so the slice is built from the chosen table rather than the
    # rejected rival. (Previously this kept only the rejected rival.)
    ctx = WorkflowContext(question="q", namespace="ns")
    ctx.candidates = ["normalized.party_license"]
    ctx.clarification_resolution = ClarificationResolution(
        original_question="q", chosen_ids=["party_banking"],
        rival_ids=["party_license"],
    )
    apply_clarification_resolution(ctx)
    assert ctx.candidates[0] == "normalized.party_banking"
    assert "normalized.party_banking" in ctx.candidates


# --- Multi-ambiguity (accumulated) resolution -----------------------------
# A question with TWO independent ambiguities (a question-level table-family
# choice AND a term-level table choice) must converge: resolving the 2nd
# clarification must NOT forget the 1st. The pending record carries a
# ``resolved`` accumulation; the resolver surfaces it as
# ``ClarificationResolution.prior`` and the Phase-1 prune drops ALL accumulated
# rivals at once.


def _two_ambiguity_pending() -> dict:
    """Pending record on turn 2: 1st ambiguity (holding) already resolved,
    2nd ambiguity (party) being asked now."""
    return {
        "original_question": "What is the total market value of all active "
                             "holdings, grouped by party?",
        "options": [
            {"id": "party_banking", "label": "party_banking (database: normalized)"},
            {"id": "party_license", "label": "party_license (database: normalized)"},
        ],
        "terms": ["party"],
        "resolved": [
            {"chosen_id": "holding",
             "rival_ids": ["coverage", "holding_projection",
                           "holding_subaccount", "party"],
             "terms": ["holding"]},
        ],
    }


def test_resolve_surfaces_prior_accumulation() -> None:
    res = resolve_clarification_reply(
        reply="party_banking", pending=_two_ambiguity_pending())
    assert res is not None
    assert res.chosen_ids == ["party_banking"]
    assert res.rival_ids == ["party_license"]
    # The earlier 'holding' resolution is carried forward as prior.
    assert len(res.prior) == 1
    assert res.prior[0].chosen_id == "holding"
    assert "coverage" in res.prior[0].rival_ids


def test_build_pending_carries_prior_resolutions() -> None:
    prior = [ResolvedChoice(chosen_id="holding",
                            rival_ids=["coverage", "party"], terms=["holding"])]
    record = build_pending_clarification(
        original_question="q",
        payload={"options": [{"id": "party_banking", "label": "PB"},
                             {"id": "party_license", "label": "PL"}]},
        prior=prior,
    )
    assert [r["chosen_id"] for r in record["resolved"]] == ["holding"]
    # Historical entries store only chosen/rivals/terms — never labels (size).
    assert set(record["resolved"][0].keys()) == {"chosen_id", "rival_ids", "terms"}


def test_apply_prunes_all_accumulated_rivals() -> None:
    ctx = WorkflowContext(question="q", namespace="ns")
    ctx.candidates = [
        "normalized.holding", "normalized.coverage",
        "normalized.holding_projection", "normalized.holding_subaccount",
        "normalized.party", "normalized.party_banking", "normalized.party_license",
    ]
    ctx.clarification_resolution = ClarificationResolution(
        original_question="q", chosen_ids=["party_banking"],
        rival_ids=["party_license"],
        prior=[ResolvedChoice(
            chosen_id="holding",
            rival_ids=["coverage", "holding_projection",
                       "holding_subaccount", "party"],
            terms=["holding"])],
    )
    apply_clarification_resolution(ctx)
    # Both the current rival (party_license) AND all accumulated rivals
    # (coverage, holding_projection, holding_subaccount, party) are dropped;
    # the two chosen tables survive.
    assert ctx.candidates == ["normalized.holding", "normalized.party_banking"]


def test_apply_prunes_accumulated_rivals_vkg_iri() -> None:
    ctx = WorkflowContext(question="q", namespace="ns")
    ctx.candidates = ["http://ex.org/o#EmailMessage",
                      "http://ex.org/o#EmailCampaign",
                      "http://ex.org/o#Contact"]
    ctx.clarification_resolution = ClarificationResolution(
        original_question="q", chosen_ids=["EmailMessage"],
        rival_ids=["EmailCampaign"],
        prior=[ResolvedChoice(chosen_id="Contact", rival_ids=["Lead"],
                              terms=["contact"])],
    )
    apply_clarification_resolution(ctx)
    # EmailCampaign (current rival) dropped; Contact (a prior CHOSEN) kept.
    assert ctx.candidates == ["http://ex.org/o#EmailMessage",
                              "http://ex.org/o#Contact"]


def test_record_size_bound_with_many_resolutions() -> None:
    # A long clarification chain must stay within the DDB-safe byte bound.
    prior = [ResolvedChoice(chosen_id=f"t{i}",
                            rival_ids=[f"r{i}a", f"r{i}b", f"r{i}c"],
                            terms=[f"term{i}"]) for i in range(200)]
    record = build_pending_clarification(
        original_question="q" * 500,
        payload={"options": [{"id": "x", "label": "X" * 200}]},
        prior=prior,
    )
    assert len(json.dumps(record).encode("utf-8")) < CLARIFICATION_RECORD_MAX_BYTES
