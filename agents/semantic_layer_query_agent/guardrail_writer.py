"""PII-redacting wrapper around Bedrock Guardrails for AgentCore Memory writes.

Every conversation turn that ever reaches AgentCore Memory MUST first be
passed through ``apply_guardrail_redaction`` so PII (and other configured
sensitive content) is anonymized before persistence. This is enforced by
calling this helper from ``LessonsMemoryHooks`` — there is no other path
into ``MemorySession.add_turns`` from agent code.

The helper is fail-closed: if the guardrail call itself errors, the original
text is **not** written (the caller raises and the hook drops the turn).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class _GuardrailLike(Protocol):
    """Minimal interface used so tests can swap in a fake guardrail."""

    def apply(self, *, text: str, source: str = ...) -> dict: ...


@dataclass
class RedactionResult:
    """Outcome of applying the guardrail to a single turn.

    Attributes:
        text: The text the caller should persist. Either the original
            (no intervention) or the guardrail's anonymized output.
        intervened: True when the guardrail anonymized or replaced text.
        action: The raw ``action`` field returned by ApplyGuardrail
            ('NONE' | 'GUARDRAIL_INTERVENED' | 'ERROR').
    """

    text: str
    intervened: bool
    action: str


class GuardrailWriteError(RuntimeError):
    """Raised when the guardrail itself fails — caller should drop the turn."""


def apply_guardrail_redaction(
    *,
    text: str,
    guardrail: _GuardrailLike,
    fallback_redacted: str = "[REDACTED]",
) -> RedactionResult:
    """Run text through Bedrock Guardrails and return the version safe to persist.

    Args:
        text: The original turn content (user or assistant message).
        guardrail: A ``GuardrailService``-shaped object with an
            ``apply(text=..., source=...)`` method.
        fallback_redacted: String written when the guardrail blocked content
            entirely (no anonymized output available).

    Returns:
        A ``RedactionResult`` whose ``text`` is what the caller should persist.

    Raises:
        GuardrailWriteError: When the guardrail call returned ``action='ERROR'``.
            Fail-closed — never persist an unredacted turn.
    """
    if not text:
        return RedactionResult(text=text, intervened=False, action="NONE")

    # Source 'OUTPUT' is the right setting for *outgoing* content we are about
    # to log — Bedrock applies the same PII anonymizers regardless of source,
    # but OUTPUT is conceptually correct (we're storing model+user output for
    # later retrieval, not user input awaiting a response).
    result = guardrail.apply(text=text, source="OUTPUT")
    action = result.get("action", "NONE")

    if action == "ERROR":
        # GuardrailService.apply already logs; we raise so the hook drops
        # the turn rather than writing raw PII to AgentCore Memory.
        raise GuardrailWriteError("guardrail apply call failed")

    if action == "GUARDRAIL_INTERVENED":
        # The bedrock-runtime ApplyGuardrail response includes the anonymized
        # text in the outputs[].text field. GuardrailService surfaces that as
        # `message`. If a topic-policy block returned no replacement output,
        # fall back to a redacted marker so we never persist the original.
        anonymized = result.get("message") or fallback_redacted
        return RedactionResult(text=anonymized, intervened=True, action=action)

    return RedactionResult(text=text, intervened=False, action=action)
