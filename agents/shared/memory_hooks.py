"""Strands ``HookProvider`` that records every conversation turn into Bedrock
AgentCore Memory — short-term raw turns plus long-term consolidated lessons
via the ``SemanticStrategy`` configured on the memory resource.

Design note: there is intentionally **no** synchronous reflection step.
``SemanticStrategy`` extracts and consolidates lessons asynchronously on the
service side; the writer's job is just to feed it raw turns. Every write goes
through ``apply_guardrail_redaction`` first so PII is anonymized before it
ever reaches AgentCore Memory.

Two entry points share that single guarded write path:
  - ``LessonsMemoryHooks`` — a Strands ``HookProvider`` for any conversational
    ``Agent`` (observes ``MessageAddedEvent`` and persists each turn).
  - ``persist_turn_pair`` — an imperative helper the query agents call directly
    at their ``_run_query`` boundary. They run a deterministic Tier 2 *graph*
    rather than a single ReAct ``Agent``, so there is no conversation-level
    agent for the hook to attach to; the helper persists the resolved
    (question, answer) pair instead.

References:
- short-term sample: agentcore-samples/01-tutorials/04-AgentCore-memory/01-short-term-memory/01-single-agent/with-strands-agent
- long-term  sample: agentcore-samples/01-tutorials/04-AgentCore-memory/02-long-term-memory/01-single-agent/using-strands-agent-hooks
"""

from __future__ import annotations

import logging
from typing import Any, Optional

try:
    from agents.shared.guardrail_writer import (
        GuardrailWriteError,
        apply_guardrail_redaction,
    )
except ImportError:  # container path: agents/ is on PYTHONPATH
    from shared.guardrail_writer import (  # type: ignore
        GuardrailWriteError,
        apply_guardrail_redaction,
    )

logger = logging.getLogger(__name__)


def _extract_text(message: Any) -> Optional[str]:
    """Pull the first text block out of a Strands ``messages`` entry.

    Strands message shape (Bedrock variant):
        {"role": "user"|"assistant", "content": [{"text": "..."}, ...]}

    Returns None when the message has no text content (eg. a tool-use turn).
    """
    if not isinstance(message, dict):
        return None
    content = message.get("content") or []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            return block["text"]
    return None


class LessonsMemoryHooks:
    """Records turns into AgentCore Memory; long-term records emerge via the
    memory resource's configured ``SemanticStrategy``.

    Wiring on a Strands agent:

        Agent(
            ...,
            hooks=[LessonsMemoryHooks(
                memory_session_factory=lambda: mgr.create_memory_session(
                    actor_id=f"{semantic_layer_id}/{semantic_layer_version}/{user_id}",
                    session_id=session_id),
                guardrail=guardrail_service,
            )],
        )

    The ``actor_id`` encodes
    ``"<semanticLayerId>/<semanticLayerVersion>/<userId>"`` so the memory
    resource's ``/lessons/{actorId}/{sessionId}/`` strategy template resolves
    to ``/lessons/<semanticLayerId>/<semanticLayerVersion>/<userId>/<sessionId>/``
    — per-layer, per-layer-version, per-user, per-session namespaces.

    The ``memory_session_factory`` returns a ``MemorySession`` (from
    ``bedrock_agentcore.memory.session``). It's a lambda so the actor/session
    IDs can be captured per-invocation rather than at agent-construction time.

    Attributes:
        _factory: Zero-arg callable returning a ``MemorySession``.
        _guardrail: ``GuardrailService``-shaped object.
        _conversational_message_cls: ``ConversationalMessage`` constructor.
        _message_role_cls: ``MessageRole`` enum.
    """

    def __init__(
        self,
        *,
        memory_session_factory,
        guardrail,
        conversational_message_cls=None,
        message_role_cls=None,
    ) -> None:
        """Construct a hook bound to a memory-session factory and a guardrail.

        Args:
            memory_session_factory: Zero-arg callable returning a
                ``bedrock_agentcore.memory.session.MemorySession``.
            guardrail: A ``GuardrailService``-shaped object.
            conversational_message_cls: Override for ``ConversationalMessage``
                (test seam — production code imports the real one).
            message_role_cls: Override for ``MessageRole``.
        """
        self._factory = memory_session_factory
        self._guardrail = guardrail
        if conversational_message_cls is None or message_role_cls is None:
            # Lazy-import the bedrock-agentcore SDK so unit tests can run
            # without the package installed (we inject fakes via the kwargs).
            from bedrock_agentcore.memory.constants import (  # type: ignore
                ConversationalMessage,
                MessageRole,
            )

            self._conversational_message_cls = (
                conversational_message_cls or ConversationalMessage
            )
            self._message_role_cls = message_role_cls or MessageRole
        else:
            self._conversational_message_cls = conversational_message_cls
            self._message_role_cls = message_role_cls

    # ------------------------------------------------------------------
    # Hook callbacks
    # ------------------------------------------------------------------

    def on_message_added(self, event: Any) -> None:
        """Record the most recently added message into AgentCore Memory.

        Failures are swallowed and logged — the agent's own response must
        never be blocked by a memory-write error.
        """
        try:
            msg = event.agent.messages[-1]
        except (AttributeError, IndexError):
            return

        text = _extract_text(msg)
        if not text:
            return  # tool-use turn, nothing to log
        role = msg.get("role")
        if role not in ("user", "assistant"):
            return

        try:
            redaction = apply_guardrail_redaction(
                text=text, guardrail=self._guardrail
            )
        except GuardrailWriteError:
            # Fail-closed: never persist an unredacted turn.
            logger.warning(
                "guardrail unavailable — dropping turn from memory write"
            )
            return

        try:
            session = self._factory()
            role_enum = self._message_role_cls(role.upper())
            session.add_turns(
                messages=[
                    self._conversational_message_cls(redaction.text, role_enum)
                ]
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, log + drop
            logger.warning(
                "failed to write turn to AgentCore Memory: %s", exc
            )

    def on_after_invocation(self, _event: Any) -> None:
        """No-op — long-term consolidation happens server-side via the
        resource's ``SemanticStrategy``. Kept as a registration point so
        future hooks (eg. metrics) can subscribe without restructuring.
        """
        return

    # ------------------------------------------------------------------
    # Strands HookProvider contract
    # ------------------------------------------------------------------

    def register_hooks(self, registry: Any) -> None:
        """Register the per-message + post-invocation callbacks.

        Imports the event classes lazily so this module is importable in
        environments without ``strands-agents`` installed (eg. a unit-test
        runner that injects a fake registry).
        """
        from strands.hooks import (  # type: ignore
            AfterInvocationEvent,
            MessageAddedEvent,
        )

        registry.add_callback(MessageAddedEvent, self.on_message_added)
        registry.add_callback(AfterInvocationEvent, self.on_after_invocation)


def persist_mapping_lesson(
    *,
    memory_id: str,
    actor_id: str,
    session_id: str,
    terms: list,
    chosen_label: str,
    guardrail,
    region: Optional[str] = None,
    manager_factory=None,
) -> bool:
    """Persist a crisp "<term> → <chosen>" mapping lesson into AgentCore Memory.

    Called when a user *resolves* a disambiguation clarification (picks one
    option). The free-form turn text alone might or might not lead the semantic
    strategy to extract the mapping; writing an explicit, unambiguous sentence
    here gives the strategy a high-quality fact to consolidate, so a later
    session can recall "the user's '<term>' means <chosen>" (see
    ``lessons_recall``).

    The sentence is phrased declaratively as a stable user-scoped preference:

        ``When I refer to "admin codes", I mean adminCode.``

    Fail-soft / fail-closed exactly like :func:`persist_turn_pair`: a missing
    ``memory_id`` (or empty terms/choice) is a no-op; the sentence is still
    guardrail-redacted before the write; any error is logged and swallowed.

    Args:
        memory_id: AgentCore Memory id (``LESSONS_MEMORY_ID``); empty → no-op.
        actor_id: ``"<semanticLayerId>/<semanticLayerVersion>/<userId>"``.
        session_id: Chat session id.
        terms: The ambiguous term(s) the clarification was about.
        chosen_label: The option the user selected (table/IRI local name).
        guardrail: ``GuardrailService``-shaped object.
        region: AWS region for the data-plane client.
        manager_factory: Test seam (see :func:`persist_turn_pair`).

    Returns:
        ``True`` when the lesson was written; ``False`` on a no-op / drop.
    """
    if not memory_id:
        return False
    phrase = " / ".join(t for t in (terms or []) if t).strip()
    if not phrase or not chosen_label:
        return False
    # A USER-role declarative fact — semantic extraction keys on USER/ASSISTANT
    # turns, and phrasing it as the user's own stated preference makes the
    # extracted record a stable, recallable mapping.
    lesson = f'When I refer to "{phrase}", I mean {chosen_label}.'
    return persist_turn_pair(
        memory_id=memory_id,
        actor_id=actor_id,
        session_id=session_id,
        user_text=lesson,
        assistant_text="",  # the mapping fact lives entirely on the user turn
        guardrail=guardrail,
        region=region,
        manager_factory=manager_factory,
    )


def persist_turn_pair(
    *,
    memory_id: str,
    actor_id: str,
    session_id: str,
    user_text: str,
    assistant_text: str,
    guardrail,
    region: Optional[str] = None,
    manager_factory=None,
) -> bool:
    """Persist one (user question, assistant answer) pair into AgentCore Memory.

    This is the **imperative** counterpart to ``LessonsMemoryHooks``. Both query
    agents (``ontology_query_agent``, ``metadata_query_agent``) run a deterministic
    Tier 2 *graph* — not a single Strands ``Agent`` ReAct loop — so there is no
    conversation-level agent whose ``MessageAddedEvent`` the hook could observe.
    Instead the agents call this directly at their ``_run_query`` boundary once a
    turn resolves, feeding the same write path the hook uses: every turn is
    PII-redacted through Bedrock Guardrails, then written via
    ``MemorySession.add_turns``. AgentCore's ``SemanticStrategy`` consolidates the
    long-term ``/lessons/...`` records asynchronously on the service side.

    Fail-soft by contract: a missing ``memory_id`` short-circuits to a no-op (so
    environments without the memory stack — and the many direct ``_run_query``
    unit tests — keep working), and any downstream error is logged and swallowed
    rather than propagated, because persisting a lesson must never break the
    user's reply. A turn whose guardrail call fails (or which redacts to empty)
    is dropped fail-closed, never persisted raw.

    Args:
        memory_id: AgentCore Memory id (the runtime's ``LESSONS_MEMORY_ID``).
            Empty/falsey → no-op returning ``False``.
        actor_id: Actor identifier; callers encode
            ``"<semanticLayerId>/<semanticLayerVersion>/<userId>"`` so the
            strategy's ``/lessons/{actorId}/{sessionId}/`` template resolves to
            ``/lessons/<semanticLayerId>/<semanticLayerVersion>/<userId>/<sessionId>/``
            — a per-layer, per-layer-version, per-user, per-session namespace.
        session_id: Chat session identifier (partitions the namespace).
        user_text: The user's (already-contextualized) question for this turn.
        assistant_text: The assistant's answer for this turn.
        guardrail: A ``GuardrailService``-shaped object with
            ``apply(text=..., source=...)``.
        region: AWS region for the data-plane client; defaults to the SDK's
            session resolution when ``None``.
        manager_factory: Test seam — zero-arg callable returning a
            ``MemorySessionManager``-shaped object. Production leaves this
            ``None`` and the real SDK manager is constructed.

    Returns:
        ``True`` when at least one message was written; ``False`` on no-op
        (memory unconfigured) or when every message was dropped/failed.
    """
    if not memory_id:
        # No memory resource wired (eg. local dev, unit tests) — silent no-op.
        return False

    # Redact both halves first; drop fail-closed on guardrail error and skip
    # any message that has no text to persist.
    messages_text: list[tuple[str, str]] = []
    for role, text in (("USER", user_text), ("ASSISTANT", assistant_text)):
        if not text:
            continue
        try:
            redaction = apply_guardrail_redaction(text=text, guardrail=guardrail)
        except GuardrailWriteError:
            logger.warning(
                "guardrail unavailable — dropping %s turn from lessons memory",
                role,
            )
            continue
        if redaction.text:
            messages_text.append((role, redaction.text))

    if not messages_text:
        return False

    try:
        # Lazy-import the SDK so this module imports cleanly in test envs that
        # don't ship bedrock-agentcore (the manager_factory seam covers tests).
        from bedrock_agentcore.memory.constants import (  # type: ignore
            ConversationalMessage,
            MessageRole,
        )

        if manager_factory is not None:
            manager = manager_factory()
        else:
            from bedrock_agentcore.memory.session import (  # type: ignore
                MemorySessionManager,
            )

            manager = MemorySessionManager(
                memory_id=memory_id, region_name=region
            )

        memory_session = manager.create_memory_session(
            actor_id=actor_id, session_id=session_id
        )
        memory_session.add_turns(
            messages=[
                ConversationalMessage(text, MessageRole(role))
                for role, text in messages_text
            ]
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort, log + drop
        logger.warning(
            "failed to persist lessons turn-pair to AgentCore Memory "
            "(actor=%s session=%s): %s",
            actor_id, session_id, exc,
        )
        return False
