"""Unit tests for ``agents.shared.followup`` — follow-up contextualization.

Covers the lexical follow-up gate, first-turn / non-follow-up passthrough, the
rewrite happy path, and fail-soft behaviour on a model error. The Strands
``Agent`` is the conftest MagicMock stub; tests that exercise the rewrite
monkeypatch ``strands.Agent`` to return a controlled result.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from agents.shared.followup import (
    ContextualizationResult,
    contextualize_question,
    looks_like_followup,
)


# --- the lexical follow-up gate -------------------------------------------
@pytest.mark.parametrize("q", [
    "again, how many are there again?",
    "what about for last year?",
    "and the inactive ones?",
    "show me those by region",
    "why?",
    "list them all",
])
def test_looks_like_followup_true(q: str) -> None:
    assert looks_like_followup(q) is True


@pytest.mark.parametrize("q", [
    "How many active parties have a mobile phone number on record",
    "Count the distinct policy holders grouped by account status value",
    "Which distribution channels generated premium revenue during the quarter",
])
def test_looks_like_followup_false(q: str) -> None:
    # Long, self-contained questions with concrete entities and no referential
    # cue words should not trip the gate (so they incur zero added LLM latency).
    # NOTE: the gate is deliberately permissive — a question containing a
    # referential word like "those"/"their" (even used non-referentially, as in
    # the turn-1 screenshot question) WILL trip it; that only costs one bounded
    # rewrite call that returns the question unchanged. Correctness lives in the
    # rewriter, not the gate.
    assert looks_like_followup(q) is False


# --- helpers ---------------------------------------------------------------
def _history_rows(_session_id: str = ""):
    """Two-turn history: a full question + its assistant answer w/ totals.

    Accepts (and ignores) a session_id so it matches the ``history_loader``
    signature ``contextualize_question`` calls it with.
    """
    return [
        {"role": "user", "text": "How many active parties have a mobile phone?",
         "turnId": "t1"},
        {"role": "assistant", "text": "There are 5 active parties.",
         "turnId": "t1",
         "totals": {"rowCount": 5, "sql": "SELECT COUNT(*) FROM party"}},
    ]


def _model_factory():
    """A no-op model factory — the Agent stub ignores the model anyway."""
    return object()


class _FakeAgent:
    """Stand-in for strands.Agent: returns a fixed rewritten question."""

    def __init__(self, rewritten: str):
        self._rewritten = rewritten
        self.last_prompt = None

    def __call__(self, prompt: str):
        self.last_prompt = prompt
        return SimpleNamespace(
            message={"content": [{"text": self._rewritten}]}
        )


@pytest.fixture
def patch_agent(monkeypatch):
    """Install a factory that monkeypatches strands.Agent to a _FakeAgent."""
    created = {}

    def _install(rewritten: str):
        agent = _FakeAgent(rewritten)
        created["agent"] = agent
        monkeypatch.setattr(sys.modules["strands"], "Agent",
                            lambda **kwargs: agent, raising=False)
        return agent

    return _install


# --- contextualize_question ------------------------------------------------
def test_first_turn_passthrough_no_history(patch_agent) -> None:
    """A follow-up-looking question with NO history passes through untouched."""
    agent = patch_agent("SHOULD NOT BE USED")
    result = contextualize_question(
        question="again, how many?",
        session_id="s1",
        model_factory=_model_factory,
        history_loader=lambda sid: [],  # no prior turns
    )
    assert isinstance(result, ContextualizationResult)
    assert result.rewritten == "again, how many?"
    assert result.changed is False
    assert result.is_followup is True  # nosemgrep: is-function-without-parentheses — bool dataclass field, not a method
    # The model must not be invoked when there is nothing to resolve against.
    assert agent.last_prompt is None


def test_non_followup_passthrough_skips_model(patch_agent) -> None:
    """A self-contained question never reaches the rewriter."""
    agent = patch_agent("SHOULD NOT BE USED")
    q = ("Count the distinct policy holders grouped by account status "
         "value across all regions")
    result = contextualize_question(
        question=q, session_id="s1", model_factory=_model_factory,
        history_loader=_history_rows,
    )
    assert result.rewritten == q
    assert result.changed is False
    assert result.is_followup is False  # nosemgrep: is-function-without-parentheses — bool dataclass field, not a method
    assert agent.last_prompt is None


def test_followup_rewrite_uses_history(patch_agent) -> None:
    """A follow-up with history is rewritten and the transcript is in-prompt."""
    standalone = "How many active parties have a mobile phone?"
    agent = patch_agent(standalone)
    result = contextualize_question(
        question="again, how many are there again?",
        session_id="s1",
        model_factory=_model_factory,
        history_loader=_history_rows,
    )
    assert result.rewritten == standalone
    assert result.changed is True
    assert result.is_followup is True  # nosemgrep: is-function-without-parentheses — bool dataclass field, not a method
    # Prior turns (and the [Prior result] pointer) must be in the rewrite prompt.
    assert "There are 5 active parties." in agent.last_prompt
    assert "[Prior result]" in agent.last_prompt
    assert "again, how many are there again?" in agent.last_prompt


def test_rewrite_failure_falls_back_to_original(monkeypatch) -> None:
    """A model error degrades to the original question (no raise)."""
    def _boom(**kwargs):
        raise RuntimeError("bedrock unavailable")

    monkeypatch.setattr(sys.modules["strands"], "Agent", _boom, raising=False)
    result = contextualize_question(
        question="and what about last year?",
        session_id="s1",
        model_factory=_model_factory,
        history_loader=_history_rows,
    )
    assert result.rewritten == "and what about last year?"
    assert result.changed is False
    assert result.is_followup is True  # nosemgrep: is-function-without-parentheses — bool dataclass field, not a method


def test_empty_rewrite_falls_back_to_original(patch_agent) -> None:
    """An empty model output degrades to the original question."""
    patch_agent("   ")  # whitespace-only -> treated as empty
    result = contextualize_question(
        question="again?",
        session_id="s1",
        model_factory=_model_factory,
        history_loader=_history_rows,
    )
    assert result.rewritten == "again?"
    assert result.changed is False


def test_history_loader_error_passes_through(patch_agent) -> None:
    """A history load error is swallowed; the question passes through."""
    agent = patch_agent("SHOULD NOT BE USED")

    def _raise(_sid):
        raise RuntimeError("ddb down")

    result = contextualize_question(
        question="what about them?",
        session_id="s1",
        model_factory=_model_factory,
        history_loader=_raise,
    )
    assert result.rewritten == "what about them?"
    assert result.changed is False
    assert agent.last_prompt is None


def test_blank_question_passthrough() -> None:
    """A blank question is returned as-is, not flagged a follow-up."""
    result = contextualize_question(
        question="   ", session_id="s1", model_factory=_model_factory,
        history_loader=_history_rows,
    )
    assert result.rewritten == ""
    assert result.is_followup is False  # nosemgrep: is-function-without-parentheses — bool dataclass field, not a method
    assert result.changed is False
