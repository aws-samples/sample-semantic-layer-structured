"""Guardrail wrapper for the agent-runtime image.

The Lambda REST API ships its own ``services.guardrail_service.GuardrailService``;
the agent runtime images don't have access to the lambda-side ``services/``
package, so we keep an identical-shape shim here. Both surfaces:

    apply(text: str, source: str = 'INPUT') -> {action, blocked, message}

so ``apply_guardrail_redaction`` and ``LessonsMemoryHooks`` can consume either
without branching.

Note: a sibling ``agents/shared/guardrails.py`` also defines a
``GuardrailService`` — positional (``apply(text, source)``) and fail-OPEN, for
chat INPUT/OUTPUT screening. This shim is keyword-only and fail-closed. Keep
them separate.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class GuardrailService:
    """Calls bedrock-runtime ApplyGuardrail. Returns ``action='ERROR'`` on
    SDK failure so the caller can fail-closed (drop the turn rather than
    persist raw PII).
    """

    def __init__(self) -> None:
        self.guardrail_id = os.environ.get('GUARDRAIL_IDENTIFIER', '')
        self.guardrail_version = os.environ.get('GUARDRAIL_VERSION', '')
        self.region = os.environ.get('AWS_REGION', 'us-east-1')
        self._client = None

    @property
    def enabled(self) -> bool:
        """True when both guardrail id + version are configured."""
        return bool(self.guardrail_id and self.guardrail_version)

    def _get_client(self):
        if self._client is None:
            import boto3  # local import keeps cold-start fast
            self._client = boto3.client('bedrock-runtime', region_name=self.region)
        return self._client

    def apply(self, *, text: str, source: str = 'INPUT') -> dict:
        """Run the guardrail and surface ``{action, blocked, message}``.

        Args:
            text: Content to evaluate.
            source: 'INPUT' | 'OUTPUT' — see ``apply_guardrail`` API.

        Returns:
            dict with ``action`` ('NONE' | 'GUARDRAIL_INTERVENED' | 'ERROR'),
            ``blocked`` (bool), and ``message`` (anonymized text or empty).
        """
        if not self.enabled:
            # When the guardrail isn't deployed, treat every call as a pass.
            # The hook still works — just no PII protection. Operators must
            # ensure GuardrailsStack is deployed before relying on this.
            logger.debug("Guardrails not configured — skipping ApplyGuardrail")
            return {'blocked': False, 'message': '', 'action': 'NONE'}

        try:
            response = self._get_client().apply_guardrail(
                guardrailIdentifier=self.guardrail_id,
                guardrailVersion=self.guardrail_version,
                source=source,
                content=[{'text': {'text': text}}],
            )
        except Exception as exc:  # noqa: BLE001 — surface as ERROR
            logger.error("ApplyGuardrail call failed: %s", exc, exc_info=True)
            return {'blocked': False, 'message': '', 'action': 'ERROR'}

        action = response.get('action', 'NONE')
        outputs = response.get('outputs') or []
        message = outputs[0].get('text', '') if outputs else ''
        return {
            'blocked': action == 'GUARDRAIL_INTERVENED',
            'message': message,
            'action': action,
        }
