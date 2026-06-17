"""Unit tests for the Phase 2 slice-sufficiency judge."""
from unittest.mock import MagicMock

from agents.metadata_query_agent.query_prompts import JUDGE_PROMPT as RAG_JUDGE_PROMPT
from agents.ontology_query_agent.tier2.slice_judge import (
    _JUDGE_PROMPT,
    build_slice_judge,
)


def test_rag_judge_accepts_label_column_without_lookup_table():
    # Regression guard (session ab062000): "top party types and their
    # human-readable descriptions" is answerable via party.party_type alone
    # (its values ARE readable: Organization/Individual). The judge prompt must
    # NOT demand a separate type_codes lookup when a "label" semantic_role
    # column is present — that over-rejection looped to phase3_max_rounds.
    p = RAG_JUDGE_PROMPT.lower()
    # The prompt must recognise the "label" semantic_role as a satisfying
    # human-readable form, and must explicitly call demanding a lookup in that
    # case over-rejection.
    assert "semantic_role" in RAG_JUDGE_PROMPT
    assert "label" in p
    assert "over-rejection" in p


def test_vkg_judge_prompt_has_relationship_connectivity_hardening():
    # The VKG judge must reason about connectivity between related entities and
    # must NOT pass a slice the generator could only satisfy by inventing a
    # predicate/role. BUT it must FIRST try derivation/self-join (the dominant
    # false-negative, gt-00/gt-01): a role derivable from a sibling property
    # (coverage_type) or a relationship expressible by self-joining an
    # association class (life_participant, keyed by participant_sk) is
    # SUFFICIENT — it must not be rejected as "unmodelled".
    p = _JUDGE_PROMPT.lower()
    # Connectivity / path reasoning between related entities.
    assert "connect" in p or "path" in p
    # Anti-invention guard is retained.
    assert "invent" in p
    # Concept-level completeness override (each requested value maps to a real IRI).
    assert "completeness" in p
    # Derivation / self-join acceptance — the gt-00/gt-01 over-rejection fix.
    assert "deriv" in p
    assert "self-join" in p
    assert "coverage_type" in p
    assert "participant_sk" in p


def test_judge_returns_sufficient_decision():
    fake_agent = MagicMock()
    result = MagicMock()
    result.structured_output = MagicMock(sufficient=True, missing=[])
    fake_agent.return_value = result
    model_factory = MagicMock(return_value=MagicMock())
    judge = build_slice_judge(model_factory=model_factory,
                              agent_factory=lambda **kw: fake_agent)
    out = judge({"slice": "...", "question": "q?"})
    assert out["sufficient"] is True
    assert out["missing"] == []


def test_judge_returns_missing_iris_on_insufficient():
    fake_agent = MagicMock()
    result = MagicMock()
    result.structured_output = MagicMock(sufficient=False, missing=["ex:Z", "ex:Y"])
    fake_agent.return_value = result
    judge = build_slice_judge(model_factory=MagicMock(),
                              agent_factory=lambda **kw: fake_agent)
    out = judge({"slice": "...", "question": "q?"})
    assert out["sufficient"] is False
    assert out["missing"] == ["ex:Z", "ex:Y"]


def test_judge_falls_back_to_sufficient_on_judge_failure():
    fake_agent = MagicMock(side_effect=RuntimeError("judge unavailable"))
    judge = build_slice_judge(model_factory=MagicMock(),
                              agent_factory=lambda **kw: fake_agent)
    out = judge({"slice": "x", "question": "q?"})
    assert out["sufficient"] is True
    assert out["missing"] == []
