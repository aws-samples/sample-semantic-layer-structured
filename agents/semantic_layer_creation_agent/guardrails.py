"""Bedrock Guardrails wrapper for the Hybrid creation agent.

Copied-and-owned (per the design's copy-not-import rule) from
``agents/shared/guardrails.py``. Screens the free-text creation inputs
(business-purpose / use-case descriptions the KB builder incorporates into
prose) before they reach the model.

Fail-open: a guardrail API error never blocks the build. The lazy
``_get_client`` seam keeps unit tests hermetic via monkeypatching.

Isolation note: standard library + ``boto3`` (imported lazily inside the seam)
ONLY — no ``agents.shared`` / sibling-agent imports (the Hybrid copy-and-own
isolation rule).
"""

import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)


class GuardrailService:
    """Screen creation-input text via bedrock-runtime ApplyGuardrail."""

    def __init__(self) -> None:
        """Read GUARDRAIL_IDENTIFIER / GUARDRAIL_VERSION / AWS_REGION from env."""
        self.guardrail_id: str = os.environ.get("GUARDRAIL_IDENTIFIER", "")
        self.guardrail_version: str = os.environ.get("GUARDRAIL_VERSION", "")
        self.region: str = os.environ.get("AWS_REGION", "us-east-1")
        self._client = None

    @property
    def enabled(self) -> bool:
        """True only when both guardrail id and version are configured."""
        return bool(self.guardrail_id and self.guardrail_version)

    def _get_client(self):
        """Lazily build a bedrock-runtime boto3 client.

        Lazy construction keeps import network-free and gives unit tests a
        single seam to monkeypatch.

        Returns:
            A boto3 bedrock-runtime client.
        """
        if self._client is None:
            import boto3  # local import keeps cold-start fast

            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def apply(self, text: str, source: str = "INPUT") -> Dict[str, object]:
        """Apply the guardrail to ``text``.

        Args:
            text: Content to evaluate.
            source: 'INPUT' for input text, 'OUTPUT' for generated prose.

        Returns:
            dict with keys ``blocked`` (bool), ``message`` (str — canned blocked
            text or empty), and ``action`` ('NONE' | 'GUARDRAIL_INTERVENED' |
            'ERROR').
        """
        if not self.enabled:
            return {"blocked": False, "message": "", "action": "NONE"}
        try:
            resp = self._get_client().apply_guardrail(
                guardrailIdentifier=self.guardrail_id,
                guardrailVersion=self.guardrail_version,
                source=source,
                content=[{"text": {"text": text}}],
            )
            action = resp.get("action", "NONE")
            blocked = action == "GUARDRAIL_INTERVENED"
            message = ""
            if blocked:
                outputs = resp.get("outputs", [])
                # ``.get('text', ...)`` so a malformed output without a 'text'
                # key can't raise KeyError.
                message = (
                    outputs[0].get("text", "Content blocked by safety policy.")
                    if outputs
                    else "Content blocked by safety policy."
                )
            return {"blocked": blocked, "message": message, "action": action}
        except Exception as e:  # noqa: BLE001 — fail open
            logger.error("ApplyGuardrail failed (fail-open): %s", e, exc_info=True)
            return {"blocked": False, "message": "", "action": "ERROR"}
