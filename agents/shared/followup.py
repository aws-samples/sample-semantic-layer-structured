"""Follow-up contextualization — rewrite a follow-up turn into a standalone question.

The Tier 2 Strands graph (both RAG and VKG agents) resolves a *single,
self-contained* question: Phase 1 embeds the raw question into KB retrieval /
lexical ranking and Phase 2 tokenizes it for term disambiguation. A follow-up
like "again, how many are there again?" therefore reaches the topic router as a
standalone string with no antecedent — the router finds no tables and Phase 2
fires a spurious clarification.

This module closes that gap *before* the graph runs: given the current chat
session's history, it rewrites a follow-up into a fully self-contained question
(resolving pronouns/ellipsis against prior turns), which the existing pipeline
then resolves unchanged.

Design choices:
  * Runs in ``_run_query`` (not as a graph node) so the graph keeps its
    single-question invariant and no new edges / trace wiring are required.
  * History is loaded server-side from ``session_id`` because the frontend
    sends only the sessionId on each chat turn — never a ``messages`` array.
  * A cheap lexical gate (:func:`looks_like_followup`) decides whether to spend
    an LLM call at all, so the common "full question" turn adds zero latency.
  * Fail-soft everywhere: any error returns the original question. A
    contextualization step must never break a turn.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Dual-import: repo-root callers use ``agents.shared``; the agent container has
# ``shared`` directly on PYTHONPATH (no top-level ``agents`` package).
try:
    from agents.shared.chat_sessions import ChatSessionService
    from agents.shared.history import to_strands_messages
except ImportError:  # container path
    from shared.chat_sessions import ChatSessionService  # type: ignore
    from shared.history import to_strands_messages  # type: ignore


# Referential / elliptical cues that signal a turn depends on prior context.
# Word-boundary matched against the lowercased question. Deliberately broad —
# a false positive only costs one bounded LLM call (which returns the question
# unchanged when it is already self-contained); a false negative leaves a real
# follow-up unresolved, which is the bug we are fixing.
_FOLLOWUP_CUES = (
    "again", "instead", "what about", "how about", "and what", "and how",
    "those", "these", "them", "they", "their", "that one", "this one",
    "same", "previous", "prior", "earlier", "last one", "the first",
    "the second", "the third", "the last", "more of", "the rest",
)

# Standalone pronouns that, on their own, imply an antecedent. Matched as whole
# words so "it" doesn't fire on "item" / "with".
_PRONOUN_CUES = ("it", "its", "they", "them", "those", "these", "that", "this")

# A question shorter than this many significant tokens is treated as a likely
# follow-up regardless of cue words (e.g. "and for last year?", "why?").
_SHORT_QUESTION_TERMS = 4

# Number of prior turns to load as context for the rewrite. Matches the chat
# history window the rest of the system already uses.
_HISTORY_WINDOW = 10

# Bound the rewrite output — a single rephrased question is short.
_REWRITE_MAX_TOKENS = 200

_REWRITE_SYSTEM_PROMPT = (
    "You rewrite the user's latest message into a single, fully self-contained "
    "question that can be understood without the conversation history.\n"
    "Rules:\n"
    "- Resolve every pronoun, ellipsis, and back-reference (\"them\", \"that\", "
    "\"again\", \"what about X\") using the conversation history provided.\n"
    "- Preserve the user's original intent and any filters/qualifiers from "
    "earlier turns that the follow-up is implicitly reusing.\n"
    "- If the latest message is ALREADY fully self-contained, return it "
    "unchanged.\n"
    "- Output ONLY the rewritten question text. No preamble, no quotes, no "
    "markdown, no explanation."
)


@dataclass
class ContextualizationResult:
    """Outcome of a contextualization attempt.

    Attributes:
        original: The user's raw latest message.
        rewritten: The self-contained question to feed downstream (equals
            ``original`` when no rewrite happened).
        is_followup: Whether the lexical gate flagged the turn as a follow-up.
        changed: Whether ``rewritten`` differs from ``original``.
    """

    original: str
    rewritten: str
    is_followup: bool
    changed: bool


def _significant_terms(question: str) -> List[str]:
    """Return lowercased word tokens longer than two characters.

    A lightweight tokenizer for the follow-up heuristic — intentionally simpler
    than the disambiguation term extractor (we only need a rough length signal,
    not stop-word filtering).
    """
    return [w for w in re.findall(r"\b\w+\b", question.lower()) if len(w) > 2]


def looks_like_followup(question: str) -> bool:
    """Heuristically decide whether ``question`` depends on prior conversation.

    Returns True when the question is short, contains a referential cue phrase,
    or starts with a bare pronoun/conjunction — all signals that it cannot be
    resolved standalone. This gate exists purely to avoid spending an LLM call
    on obvious full questions; a generous True is cheap (the rewriter returns
    self-contained questions unchanged), a missed follow-up is the actual bug.

    Args:
        question: The user's latest message.

    Returns:
        True if the turn looks like a context-dependent follow-up.
    """
    q = (question or "").strip().lower()
    if not q:
        return False

    # Short questions almost always lean on prior context.
    if len(_significant_terms(q)) < _SHORT_QUESTION_TERMS:
        return True

    # Cue phrases anywhere in the question.
    if any(cue in q for cue in _FOLLOWUP_CUES):
        return True

    # A leading bare pronoun/conjunction ("they ...", "and ...", "it ...").
    first_word = re.findall(r"\b\w+\b", q)
    if first_word and first_word[0] in (_PRONOUN_CUES + ("and", "or", "but", "so")):
        return True

    # Whole-word pronoun anywhere (e.g. "list all of them by region").
    words = set(first_word)
    if words.intersection(_PRONOUN_CUES):
        return True

    return False


def _render_history(messages: List[Dict[str, Any]]) -> str:
    """Render Strands message dicts into a compact transcript for the prompt.

    Args:
        messages: ``[{role, content: [{text}]}, ...]`` from
            :func:`agents.shared.history.to_strands_messages`.

    Returns:
        A newline-separated ``User: ...`` / ``Assistant: ...`` transcript.
    """
    lines: List[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or []
        text = ""
        if content and isinstance(content[0], dict):
            text = content[0].get("text", "")
        if not text:
            continue
        label = "User" if role == "user" else "Assistant"
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


def _load_history_messages(
    *, session_id: str,
    history_loader: Optional[Callable[[str], List[Dict[str, Any]]]],
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Load prior turns as Strands message dicts (empty list on any miss).

    Args:
        session_id: The chat session id.
        history_loader: Optional override returning raw history rows — used by
            tests to avoid a live DynamoDB read. Defaults to
            ``ChatSessionService.history_window``.
        user_id: When provided, the history read is ownership-enforced — a
            session owned by another user yields ``[]`` so a forged sessionId
            cannot leak the victim's context into the follow-up rewrite.

    Returns:
        Strands-shaped messages for prior turns, or ``[]`` when there is no
        usable history (first turn, missing session, or any load error).
    """
    if not session_id:
        return []
    try:
        if history_loader is not None:
            rows = history_loader(session_id)
        else:
            rows = ChatSessionService().history_window(
                session_id=session_id, n=_HISTORY_WINDOW, user_id=user_id,
            )
    except Exception as exc:  # noqa: BLE001 — history is best-effort
        logger.warning("followup: history load failed (session=%s): %s",
                       session_id, exc)
        return []
    return to_strands_messages(rows)


def contextualize_question(
    *,
    question: str,
    session_id: str,
    model_factory: Callable[[], Any],
    history_loader: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    user_id: Optional[str] = None,
) -> ContextualizationResult:
    """Rewrite a follow-up question into a standalone one using chat history.

    Loads the session's recent turns, and — only when the latest message looks
    like a context-dependent follow-up AND history exists — runs one bounded
    LLM call to produce a self-contained question. Returns the original question
    unchanged on a first turn, a non-follow-up, or any failure.

    Args:
        question: The user's latest natural-language message.
        session_id: The chat session id (used to load prior turns).
        model_factory: Zero-arg callable returning a Strands-compatible
            ``BedrockModel`` (the caller passes its own ``_build_query_model``
            so the rewrite uses the same model + credentials as the agent).
        history_loader: Optional override for loading raw history rows; defaults
            to ``ChatSessionService.history_window``. Primarily for tests.

    Returns:
        A :class:`ContextualizationResult`. The caller threads
        ``result.rewritten`` into Tier 1 / Tier 2.
    """
    original = (question or "").strip()
    if not original:
        return ContextualizationResult(
            original=original, rewritten=original,
            is_followup=False, changed=False,
        )

    is_followup = looks_like_followup(original)
    if not is_followup:
        return ContextualizationResult(
            original=original, rewritten=original,
            is_followup=False, changed=False,
        )

    messages = _load_history_messages(
        session_id=session_id, history_loader=history_loader, user_id=user_id,
    )
    if not messages:
        # Looks like a follow-up but there is nothing to resolve against (first
        # turn, or history unavailable) — pass the question through untouched.
        return ContextualizationResult(
            original=original, rewritten=original,
            is_followup=True, changed=False,
        )

    transcript = _render_history(messages)
    prompt = (
        f"# Conversation so far\n{transcript}\n\n"
        f"# User's latest message\n{original}\n\n"
        "Rewrite the latest message as a single self-contained question."
    )

    try:
        # Lazy import so this module has no hard Strands dependency at import
        # time (keeps the unit tests that stub model_factory dependency-free).
        from strands import Agent

        agent = Agent(
            model=model_factory(),
            system_prompt=_REWRITE_SYSTEM_PROMPT,
            tools=[],
        )
        result = agent(prompt)
        rewritten = result.message["content"][0]["text"].strip()
    except Exception as exc:  # noqa: BLE001 — never break the turn on rewrite error
        logger.warning("followup: rewrite failed (session=%s): %s",
                       session_id, exc)
        return ContextualizationResult(
            original=original, rewritten=original,
            is_followup=True, changed=False,
        )

    # Guard against an empty / degenerate rewrite — fall back to the original.
    if not rewritten:
        return ContextualizationResult(
            original=original, rewritten=original,
            is_followup=True, changed=False,
        )

    changed = rewritten != original
    if changed:
        logger.info("followup: rewrote %r -> %r (session=%s)",
                    original, rewritten, session_id)
    return ContextualizationResult(
        original=original, rewritten=rewritten,
        is_followup=True, changed=changed,
    )
