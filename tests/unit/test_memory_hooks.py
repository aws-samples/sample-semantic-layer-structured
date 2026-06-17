"""Unit tests for ``agents.shared.memory_hooks.LessonsMemoryHooks``.

The hook is the only writer into AgentCore Memory, so its contract has to:
  - redact every turn through the guardrail before calling ``add_turns``
  - drop the turn entirely when the guardrail itself fails
  - swallow downstream errors (memory-write failures must never block a reply)
  - skip turns with no text content (eg. tool-use blocks)

Tests inject fakes for ``ConversationalMessage`` and ``MessageRole`` so we
don't need ``bedrock-agentcore`` installed in the test environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, List

import pytest

from agents.shared.memory_hooks import (
    LessonsMemoryHooks,
    persist_mapping_lesson,
    persist_turn_pair,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeMemorySession:
    """Mimics ``bedrock_agentcore.memory.session.MemorySession.add_turns``."""

    written: List[Any] = field(default_factory=list)
    raise_on_write: bool = False

    def add_turns(self, *, messages):
        if self.raise_on_write:
            raise RuntimeError("simulated memory-write failure")
        self.written.append(messages)


class _FakeMessageRole:
    """Mimics the ``MessageRole`` enum constructor used by the hook."""

    def __init__(self, value: str) -> None:
        self.value = value

    def __eq__(self, other) -> bool:  # pragma: no cover — assertion helper
        return isinstance(other, _FakeMessageRole) and self.value == other.value

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"_FakeMessageRole({self.value!r})"


@dataclass
class _FakeConversationalMessage:
    text: str
    role: Any


class _FakeGuardrail:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    def apply(self, *, text: str, source: str = "OUTPUT") -> dict:
        self.calls.append({"text": text, "source": source})
        return self.response


def _make_event(messages: List[dict]):
    """Build a minimal Strands-shaped event with the given message list."""
    agent = SimpleNamespace(messages=messages)
    return SimpleNamespace(agent=agent)


def _make_hook(*, guardrail, session: _FakeMemorySession) -> LessonsMemoryHooks:
    return LessonsMemoryHooks(
        memory_session_factory=lambda: session,
        guardrail=guardrail,
        conversational_message_cls=_FakeConversationalMessage,
        message_role_cls=_FakeMessageRole,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_on_message_added_writes_redacted_text() -> None:
    guardrail = _FakeGuardrail(
        {
            "blocked": True,
            "message": "email me at {EMAIL_ADDRESS}",
            "action": "GUARDRAIL_INTERVENED",
        }
    )
    session = _FakeMemorySession()
    hook = _make_hook(guardrail=guardrail, session=session)

    event = _make_event(
        [
            {
                "role": "user",
                "content": [{"text": "email me at someone@example.com"}],
            }
        ]
    )
    hook.on_message_added(event)

    assert len(session.written) == 1
    written = session.written[0]
    assert len(written) == 1
    assert written[0].text == "email me at {EMAIL_ADDRESS}"
    assert written[0].role.value == "USER"
    # And the guardrail saw the original.
    assert guardrail.calls[0]["text"] == "email me at someone@example.com"
    assert guardrail.calls[0]["source"] == "OUTPUT"


def test_on_message_added_writes_assistant_role() -> None:
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "NONE"})
    session = _FakeMemorySession()
    hook = _make_hook(guardrail=guardrail, session=session)

    event = _make_event(
        [{"role": "assistant", "content": [{"text": "hello"}]}]
    )
    hook.on_message_added(event)

    assert len(session.written) == 1
    assert session.written[0][0].role.value == "ASSISTANT"
    assert session.written[0][0].text == "hello"


def test_guardrail_failure_drops_turn_fail_closed() -> None:
    # action='ERROR' must drop the turn — never write raw PII.
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "ERROR"})
    session = _FakeMemorySession()
    hook = _make_hook(guardrail=guardrail, session=session)

    event = _make_event(
        [{"role": "user", "content": [{"text": "ssn 123-45-6789"}]}]
    )
    hook.on_message_added(event)

    assert session.written == []


def test_memory_write_failure_swallowed() -> None:
    # If AgentCore Memory itself errors, the hook must not propagate — the
    # agent's reply must never be blocked.
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "NONE"})
    session = _FakeMemorySession(raise_on_write=True)
    hook = _make_hook(guardrail=guardrail, session=session)

    event = _make_event(
        [{"role": "user", "content": [{"text": "what is the schema?"}]}]
    )
    # Should not raise.
    hook.on_message_added(event)
    assert session.written == []


def test_skip_message_with_no_text_block() -> None:
    # Tool-use turns have no text — skip them.
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "NONE"})
    session = _FakeMemorySession()
    hook = _make_hook(guardrail=guardrail, session=session)

    event = _make_event(
        [{"role": "assistant", "content": [{"toolUse": {"name": "x"}}]}]
    )
    hook.on_message_added(event)

    assert session.written == []
    assert guardrail.calls == []  # short-circuit before guardrail call


def test_skip_unknown_role() -> None:
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "NONE"})
    session = _FakeMemorySession()
    hook = _make_hook(guardrail=guardrail, session=session)

    event = _make_event([{"role": "system", "content": [{"text": "ignored"}]}])
    hook.on_message_added(event)

    assert session.written == []


def test_register_hooks_subscribes_both_events() -> None:
    """Lightweight check that ``register_hooks`` calls add_callback twice.

    Uses a fake registry with a stubbed ``strands.hooks`` module so we don't
    need the real Strands package installed.
    """
    import sys
    import types

    # Stub out strands.hooks unconditionally — if `strands` is already loaded
    # (eg. an in-process import from a sibling test), we still need the
    # `hooks` submodule to expose the two event classes the hook references.
    strands_module = sys.modules.get("strands") or types.ModuleType("strands")
    # Mark as a package so `from strands.hooks import ...` resolves.
    if not hasattr(strands_module, "__path__"):
        strands_module.__path__ = []  # type: ignore[attr-defined]
    sys.modules["strands"] = strands_module

    strands_hooks_module = types.ModuleType("strands.hooks")

    class _MessageAddedEvent:
        pass

    class _AfterInvocationEvent:
        pass

    strands_hooks_module.MessageAddedEvent = _MessageAddedEvent
    strands_hooks_module.AfterInvocationEvent = _AfterInvocationEvent
    sys.modules["strands.hooks"] = strands_hooks_module

    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "NONE"})
    session = _FakeMemorySession()
    hook = _make_hook(guardrail=guardrail, session=session)

    callbacks: list[tuple[Any, Any]] = []

    class _FakeRegistry:
        def add_callback(self, event_cls, cb):
            callbacks.append((event_cls, cb))

    hook.register_hooks(_FakeRegistry())
    assert len(callbacks) == 2


# ---------------------------------------------------------------------------
# persist_turn_pair — the imperative writer used by the graph query agents
# ---------------------------------------------------------------------------


@dataclass
class _FakeManagerSession:
    """Captures the messages add_turns receives, plus the actor/session it was
    created for, so tests can assert namespace scoping."""

    actor_id: str
    session_id: str
    written: List[Any] = field(default_factory=list)
    raise_on_write: bool = False

    def add_turns(self, *, messages):
        if self.raise_on_write:
            raise RuntimeError("simulated create_event failure")
        self.written.append(messages)


class _FakeManager:
    """Mimics ``MemorySessionManager`` — records the created session."""

    def __init__(self, *, raise_on_write: bool = False) -> None:
        self.raise_on_write = raise_on_write
        self.session: _FakeManagerSession | None = None

    def create_memory_session(self, *, actor_id: str, session_id: str):
        self.session = _FakeManagerSession(
            actor_id=actor_id, session_id=session_id,
            raise_on_write=self.raise_on_write,
        )
        return self.session


def test_persist_turn_pair_writes_both_redacted_messages() -> None:
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "NONE"})
    manager = _FakeManager()

    wrote = persist_turn_pair(
        memory_id="mem-123",
        actor_id="ont-1/user-9",
        session_id="sess-7",
        user_text="how many policies?",
        assistant_text="There are 42 active policies.",
        guardrail=guardrail,
        manager_factory=lambda: manager,
    )

    assert wrote is True
    assert manager.session is not None
    assert manager.session.actor_id == "ont-1/user-9"
    assert manager.session.session_id == "sess-7"
    written = manager.session.written[0]
    assert [m.role.value for m in written] == ["USER", "ASSISTANT"]
    assert [m.text for m in written] == [
        "how many policies?", "There are 42 active policies.",
    ]


def test_persist_turn_pair_noop_without_memory_id() -> None:
    # No memory resource configured → silent no-op, no manager built.
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "NONE"})
    called = {"built": False}

    def _factory():
        called["built"] = True
        return _FakeManager()

    wrote = persist_turn_pair(
        memory_id="",
        actor_id="ont-1/user-9",
        session_id="sess-7",
        user_text="q",
        assistant_text="a",
        guardrail=guardrail,
        manager_factory=_factory,
    )

    assert wrote is False
    assert called["built"] is False  # short-circuited before touching the SDK


def test_persist_turn_pair_applies_guardrail_redaction() -> None:
    # GUARDRAIL_INTERVENED on the user half → the anonymized text is persisted.
    guardrail = _FakeGuardrail(
        {"blocked": True, "message": "email {EMAIL_ADDRESS}",
         "action": "GUARDRAIL_INTERVENED"}
    )
    manager = _FakeManager()

    persist_turn_pair(
        memory_id="mem-123",
        actor_id="ont-1/user-9",
        session_id="sess-7",
        user_text="email someone@example.com",
        assistant_text="email someone@example.com",
        guardrail=guardrail,
        manager_factory=lambda: manager,
    )

    written = manager.session.written[0]
    assert all(m.text == "email {EMAIL_ADDRESS}" for m in written)


def test_persist_turn_pair_drops_turn_on_guardrail_error() -> None:
    # action='ERROR' must drop both halves fail-closed → nothing written, no-op.
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "ERROR"})
    manager = _FakeManager()

    wrote = persist_turn_pair(
        memory_id="mem-123",
        actor_id="ont-1/user-9",
        session_id="sess-7",
        user_text="ssn 123-45-6789",
        assistant_text="ok",
        guardrail=guardrail,
        manager_factory=lambda: manager,
    )

    assert wrote is False
    assert manager.session is None  # never reached the SDK


def test_persist_turn_pair_swallows_write_failure() -> None:
    # A create_event failure must not propagate (the reply already went out).
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "NONE"})
    manager = _FakeManager(raise_on_write=True)

    wrote = persist_turn_pair(
        memory_id="mem-123",
        actor_id="ont-1/user-9",
        session_id="sess-7",
        user_text="q",
        assistant_text="a",
        guardrail=guardrail,
        manager_factory=lambda: manager,
    )

    assert wrote is False  # swallowed, returned False rather than raising


# ---------------------------------------------------------------------------
# persist_mapping_lesson — crisp "<term> → <chosen>" fact on clarification resolve
# ---------------------------------------------------------------------------


def test_persist_mapping_lesson_writes_declarative_fact() -> None:
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "NONE"})
    manager = _FakeManager()

    wrote = persist_mapping_lesson(
        memory_id="mem-1",
        actor_id="layer-1/v3/user-9",
        session_id="sess-7",
        terms=["admin codes"],
        chosen_label="adminCode",
        guardrail=guardrail,
        manager_factory=lambda: manager,
    )

    assert wrote is True
    written = manager.session.written[0]
    # Only the USER-role declarative fact is written (assistant half is empty).
    assert len(written) == 1
    assert written[0].role.value == "USER"
    assert written[0].text == 'When I refer to "admin codes", I mean adminCode.'


def test_persist_mapping_lesson_noop_without_terms() -> None:
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "NONE"})
    manager = _FakeManager()

    wrote = persist_mapping_lesson(
        memory_id="mem-1",
        actor_id="layer-1/v3/user-9",
        session_id="sess-7",
        terms=[],
        chosen_label="adminCode",
        guardrail=guardrail,
        manager_factory=lambda: manager,
    )

    assert wrote is False
    assert manager.session is None


def test_persist_mapping_lesson_noop_without_memory_id() -> None:
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "NONE"})
    called = {"built": False}

    def _factory():
        called["built"] = True
        return _FakeManager()

    wrote = persist_mapping_lesson(
        memory_id="",
        actor_id="layer-1/v3/user-9",
        session_id="sess-7",
        terms=["codes"],
        chosen_label="adminCode",
        guardrail=guardrail,
        manager_factory=_factory,
    )

    assert wrote is False
    assert called["built"] is False
