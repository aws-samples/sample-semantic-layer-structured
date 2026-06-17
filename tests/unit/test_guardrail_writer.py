"""Unit tests for ``agents.shared.guardrail_writer``.

The helper is the only path from agent code into AgentCore Memory writes,
so its contract is load-bearing: PII must be redacted, blocks must be
substituted, and guardrail errors must raise (fail-closed).
"""

from __future__ import annotations

import pytest

from agents.shared.guardrail_writer import (
    GuardrailWriteError,
    RedactionResult,
    apply_guardrail_redaction,
)


class _FakeGuardrail:
    """Minimal stand-in for ``GuardrailService``. Returns a canned response."""

    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    def apply(self, *, text: str, source: str = "OUTPUT") -> dict:
        self.calls.append({"text": text, "source": source})
        return self.response


def test_no_intervention_returns_original_text() -> None:
    guardrail = _FakeGuardrail(
        {"blocked": False, "message": "", "action": "NONE"}
    )
    result = apply_guardrail_redaction(text="hello world", guardrail=guardrail)
    assert isinstance(result, RedactionResult)
    assert result.text == "hello world"
    assert result.intervened is False
    assert result.action == "NONE"
    # Source must be OUTPUT — we are storing content, not screening user input.
    assert guardrail.calls == [{"text": "hello world", "source": "OUTPUT"}]


def test_intervention_returns_anonymized_text() -> None:
    # Mirrors what the real bedrock-runtime returns for PII anonymization:
    # action=INTERVENED, outputs[0].text holds the masked string, surfaced
    # as `message` by GuardrailService.
    guardrail = _FakeGuardrail(
        {
            "blocked": True,
            "message": "Email me at {EMAIL_ADDRESS}",
            "action": "GUARDRAIL_INTERVENED",
        }
    )
    result = apply_guardrail_redaction(
        text="Email me at someone@example.com", guardrail=guardrail
    )
    assert result.text == "Email me at {EMAIL_ADDRESS}"
    assert result.intervened is True
    assert result.action == "GUARDRAIL_INTERVENED"


def test_intervention_without_replacement_uses_fallback() -> None:
    # Topic-policy block with no replacement output — defense-in-depth so we
    # never persist the original.
    guardrail = _FakeGuardrail(
        {"blocked": True, "message": "", "action": "GUARDRAIL_INTERVENED"}
    )
    result = apply_guardrail_redaction(
        text="raw legal advice request", guardrail=guardrail
    )
    assert result.text == "[REDACTED]"
    assert result.intervened is True


def test_intervention_with_custom_fallback() -> None:
    guardrail = _FakeGuardrail(
        {"blocked": True, "message": "", "action": "GUARDRAIL_INTERVENED"}
    )
    result = apply_guardrail_redaction(
        text="x", guardrail=guardrail, fallback_redacted="<masked>"
    )
    assert result.text == "<masked>"


def test_guardrail_error_raises_fail_closed() -> None:
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "ERROR"})
    with pytest.raises(GuardrailWriteError):
        apply_guardrail_redaction(text="anything", guardrail=guardrail)


def test_empty_text_short_circuits() -> None:
    # No call to the guardrail when there is nothing to redact.
    guardrail = _FakeGuardrail({"blocked": False, "message": "", "action": "NONE"})
    result = apply_guardrail_redaction(text="", guardrail=guardrail)
    assert result.text == ""
    assert result.intervened is False
    assert guardrail.calls == []
