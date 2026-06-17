"""Bedrock Guardrails wrapper for the AgentCore query agents.

Mirrors ``lambda/rest-api/services/guardrail_service.py`` so both query agents
can pre/post screen chat I/O in-runtime. Fail-open: a guardrail API error never
blocks the turn.

Note: ``agents/shared/`` has no shared ``get_boto_session`` helper — that helper
lives inside each agent's ``main.py`` and is not cleanly importable from shared
code (importing an agent ``main`` triggers heavy runtime side effects). So this
module uses a bare ``boto3`` client built lazily, matching the established
pattern in sibling shared modules (``embedding.py``, ``neptune_metadata.py``).
The lazy ``_get_client`` seam keeps unit tests hermetic via monkeypatching.

Note: a sibling ``agents/shared/guardrail_service_shim.py`` also defines a
``GuardrailService``. That one is keyword-only (``apply(*, text, source)``) and
fail-CLOSED, used by the PII-redaction memory write-path
(``guardrail_writer.py``). This one is positional and fail-OPEN, for chat
INPUT/OUTPUT screening. Keep them separate — the memory hook depends on
fail-closed semantics.
"""

import os
import logging
from typing import Dict

logger = logging.getLogger(__name__)


class GuardrailService:
    """Pre/post screen chat text via bedrock-runtime ApplyGuardrail."""

    def __init__(self) -> None:
        """Read GUARDRAIL_IDENTIFIER / GUARDRAIL_VERSION / AWS_REGION from env."""
        self.guardrail_id: str = os.environ.get('GUARDRAIL_IDENTIFIER', '')
        self.guardrail_version: str = os.environ.get('GUARDRAIL_VERSION', '')
        self.region: str = os.environ.get('AWS_REGION', 'us-east-1')
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
            self._client = boto3.client('bedrock-runtime', region_name=self.region)
        return self._client

    def apply(self, text: str, source: str = 'INPUT') -> Dict[str, object]:
        """Apply the guardrail to ``text``.

        Args:
            text: Content to evaluate.
            source: 'INPUT' for user prompts, 'OUTPUT' for agent responses.

        Returns:
            dict with keys ``blocked`` (bool), ``message`` (str — canned blocked
            text or empty), and ``action`` ('NONE' | 'GUARDRAIL_INTERVENED' |
            'ERROR').
        """
        if not self.enabled:
            return {'blocked': False, 'message': '', 'action': 'NONE'}
        try:
            resp = self._get_client().apply_guardrail(
                guardrailIdentifier=self.guardrail_id,
                guardrailVersion=self.guardrail_version,
                source=source,
                content=[{'text': {'text': text}}],
            )
            action = resp.get('action', 'NONE')
            blocked = action == 'GUARDRAIL_INTERVENED'
            message = ''
            if blocked:
                outputs = resp.get('outputs', [])
                # ``.get('text', ...)`` so a malformed output without a 'text'
                # key can't raise KeyError (matches the shim's defensive read).
                message = (
                    outputs[0].get('text', 'Content blocked by safety policy.')
                    if outputs
                    else 'Content blocked by safety policy.'
                )
            return {'blocked': blocked, 'message': message, 'action': action}
        except Exception as e:  # noqa: BLE001 — fail open
            logger.error(f"ApplyGuardrail failed (fail-open): {e}", exc_info=True)
            return {'blocked': False, 'message': '', 'action': 'ERROR'}
