"""
Bedrock Guardrails Service

Thin wrapper around the bedrock-runtime ApplyGuardrail API.
Used by QueryService and MetadataService to pre-screen user inputs
and post-screen agent outputs before returning to the frontend.
"""

import os
import logging
import boto3

logger = logging.getLogger(__name__)


class GuardrailService:

    def __init__(self):
        self.guardrail_id = os.environ.get('GUARDRAIL_IDENTIFIER', '')
        self.guardrail_version = os.environ.get('GUARDRAIL_VERSION', '')
        self.region = os.environ.get('AWS_REGION', 'us-east-1')
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.guardrail_id and self.guardrail_version)

    def _get_client(self):
        if self._client is None:
            self._client = boto3.client('bedrock-runtime', region_name=self.region)
        return self._client

    def apply(self, text: str, source: str = 'INPUT') -> dict:
        """
        Apply the guardrail to the given text.

        Args:
            text: The text content to evaluate.
            source: 'INPUT' for user prompts, 'OUTPUT' for agent responses.

        Returns:
            dict with keys:
              - blocked (bool): True if the guardrail intervened and blocked content
              - message (str): The canned blocked message (empty if not blocked)
              - action (str): 'NONE' or 'GUARDRAIL_INTERVENED'
        """
        if not self.enabled:
            logger.debug("Guardrails not configured — skipping")
            return {'blocked': False, 'message': '', 'action': 'NONE'}

        try:
            logger.info(
                f"Applying guardrail {self.guardrail_id}@{self.guardrail_version} "
                f"(source={source}, text_len={len(text)})"
            )
            response = self._get_client().apply_guardrail(
                guardrailIdentifier=self.guardrail_id,
                guardrailVersion=self.guardrail_version,
                source=source,
                content=[{'text': {'text': text}}],
            )
            action = response.get('action', 'NONE')
            blocked = action == 'GUARDRAIL_INTERVENED'
            logger.info(f"Guardrail result (source={source}): action={action}")

            if blocked:
                outputs = response.get('outputs', [])
                message = outputs[0]['text'] if outputs else 'Content blocked by safety policy.'
                logger.warning(
                    f"Guardrail intervened (source={source}): action={action}"
                )
            else:
                message = ''

            return {'blocked': blocked, 'message': message, 'action': action}

        except Exception as e:
            logger.error(f"ApplyGuardrail call failed: {e}", exc_info=True)
            # Fail open — do not block the query if the guardrail API itself errors
            return {'blocked': False, 'message': '', 'action': 'ERROR'}
