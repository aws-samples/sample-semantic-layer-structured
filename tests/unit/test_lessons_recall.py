"""Unit tests for ``agents.shared.lessons_recall`` — the read side of AgentCore
Memory that biases disambiguation from a user's prior-session lessons.

A fake ``MemorySessionManager`` is injected via ``manager_factory`` so the tests
don't need the bedrock-agentcore SDK or any AWS call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List

from agents.shared.lessons_recall import (
    build_recall_resolver,
    match_candidate,
    recall_lessons,
)


@dataclass
class _FakeManager:
    """Mimics ``MemorySessionManager.search_long_term_memories``.

    Records the (query, namespace_prefix, top_k) it was called with and returns
    pre-seeded records shaped like ``MemoryRecord`` (dict with content.text).
    """

    records: List[dict] = field(default_factory=list)
    calls: List[dict] = field(default_factory=list)
    raise_on_search: bool = False

    def search_long_term_memories(self, query, namespace_prefix=None, top_k=3):
        self.calls.append({"query": query, "namespace_prefix": namespace_prefix,
                           "top_k": top_k})
        if self.raise_on_search:
            raise RuntimeError("simulated retrieve_memory_records failure")
        return self.records


def _rec(text: str) -> dict:
    """Build a record shaped like the SDK's MemoryRecord (content.text)."""
    return {"content": {"text": text}}


# ---------------------------------------------------------------------------
# recall_lessons
# ---------------------------------------------------------------------------


def test_recall_lessons_searches_cross_session_prefix() -> None:
    mgr = _FakeManager(records=[_rec("admin codes refers to the adminCode table")])
    texts = recall_lessons(
        memory_id="mem-1",
        semantic_layer_id="layer-1",
        semantic_layer_version="v3",
        user_id="user-9",
        query="admin codes",
        manager_factory=lambda: mgr,
    )
    assert texts == ["admin codes refers to the adminCode table"]
    # Namespace prefix drops {sessionId} so recall spans prior sessions.
    assert mgr.calls[0]["namespace_prefix"] == "/lessons/layer-1/v3/user-9/"


def test_recall_lessons_noop_without_memory_id() -> None:
    mgr = _FakeManager(records=[_rec("x")])
    texts = recall_lessons(
        memory_id="",
        semantic_layer_id="layer-1",
        semantic_layer_version="v3",
        user_id="user-9",
        query="q",
        manager_factory=lambda: mgr,
    )
    assert texts == []
    assert mgr.calls == []  # never searched


def test_recall_lessons_swallows_search_error() -> None:
    mgr = _FakeManager(raise_on_search=True)
    texts = recall_lessons(
        memory_id="mem-1",
        semantic_layer_id="layer-1",
        semantic_layer_version="v3",
        user_id="user-9",
        query="q",
        manager_factory=lambda: mgr,
    )
    assert texts == []  # fail-soft


# ---------------------------------------------------------------------------
# match_candidate
# ---------------------------------------------------------------------------


def test_match_candidate_co_mention_hits() -> None:
    lessons = ["admin codes refers to the adminCode table"]
    assert match_candidate(term="codes", candidate_id="normalized.adminCode",
                           lessons=lessons) is True


def test_match_candidate_requires_both_term_and_candidate() -> None:
    lessons = ["the adminCode table holds reference data"]
    # Term "codes" is absent from the lesson → no support.
    assert match_candidate(term="codes", candidate_id="adminCode",
                           lessons=lessons) is False


def test_match_candidate_phrase_needs_all_words() -> None:
    lessons = ["admin codes map to adminCode"]
    assert match_candidate(term="admin codes", candidate_id="adminCode",
                           lessons=lessons) is True
    # "policy codes" — "policy" missing → no support.
    assert match_candidate(term="policy codes", candidate_id="adminCode",
                           lessons=lessons) is False


# ---------------------------------------------------------------------------
# build_recall_resolver
# ---------------------------------------------------------------------------


def test_resolver_disabled_without_scope() -> None:
    # Missing version → resolver is None (recall disabled, Phase 2 unchanged).
    assert build_recall_resolver(
        memory_id="mem-1", semantic_layer_id="layer-1",
        semantic_layer_version="", user_id="user-9",
    ) is None


def test_resolver_resolves_unique_supported_candidate() -> None:
    mgr = _FakeManager(records=[_rec("admin codes means the adminCode table")])
    resolver = build_recall_resolver(
        memory_id="mem-1", semantic_layer_id="layer-1",
        semantic_layer_version="v3", user_id="user-9",
        manager_factory=lambda: mgr,
    )
    # Two rival candidates; only adminCode is supported by the lesson.
    out = resolver("codes", ["normalized.adminCode", "normalized.zipCode"])
    assert out == "normalized.adminCode"


def test_resolver_returns_none_on_tie() -> None:
    # A lesson that mentions BOTH candidates can't disambiguate → defer.
    mgr = _FakeManager(records=[_rec("codes: adminCode and zipCode are both code tables")])
    resolver = build_recall_resolver(
        memory_id="mem-1", semantic_layer_id="layer-1",
        semantic_layer_version="v3", user_id="user-9",
        manager_factory=lambda: mgr,
    )
    assert resolver("codes", ["adminCode", "zipCode"]) is None


def test_resolver_returns_none_when_memory_silent() -> None:
    mgr = _FakeManager(records=[])
    resolver = build_recall_resolver(
        memory_id="mem-1", semantic_layer_id="layer-1",
        semantic_layer_version="v3", user_id="user-9",
        manager_factory=lambda: mgr,
    )
    assert resolver("codes", ["adminCode", "zipCode"]) is None


def test_resolver_caches_per_term() -> None:
    mgr = _FakeManager(records=[_rec("admin codes means adminCode")])
    resolver = build_recall_resolver(
        memory_id="mem-1", semantic_layer_id="layer-1",
        semantic_layer_version="v3", user_id="user-9",
        manager_factory=lambda: mgr,
    )
    resolver("codes", ["adminCode"])
    resolver("codes", ["adminCode"])  # same term — should hit the cache
    assert len(mgr.calls) == 1
