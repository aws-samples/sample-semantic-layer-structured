"""DynamoDB-backed user-feedback service.

This is the persistence surface behind ``POST /query/feedback`` and the
admin "Feedback" tab. We deliberately do NOT write feedback into AgentCore
Memory anymore: the memory store is append-only from the operator's point
of view (no per-record edits, no GET-by-id), so feedback that lived there
couldn't be inspected, edited, or aggregated by an admin. DynamoDB gives
us list/get/delete with stable identifiers and a clean GSI/scan story.

Schema
------

Partition key: ``ontologyId``  — admin tab queries scoped per ontology.
Sort key:      ``sk = "<createdAtIso>#<feedbackId>"`` — newest-first
               ordering by descending sort-key, with a stable id suffix
               so ties (same epoch ms) don't collide.

Attribute map:
    ontologyId, sk, feedbackId (uuid4), createdAt (ISO-8601 UTC),
    sessionId, turnId, userId, userEmail (from JWT — '' for old/anon rows),
    rating ('up' | 'down'),
    comment (string — guardrail-redacted), question (string — redacted),
    answer (string — first 500 chars, redacted), guardrailAction
    ('NONE' | 'GUARDRAIL_INTERVENED' | 'ERROR' — when present we recorded
    a fail-open write so the operator can audit).

Privacy
-------

Comment, question and answer fields are passed through Bedrock Guardrails'
``ApplyGuardrail`` in ``OUTPUT`` mode (the same mode used by the lessons
memory hook) before insert. If the guardrail call itself errors we drop
the offending fields rather than persist raw user content — the rating
and turn pointer are still useful even if the comment is missing.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key

from services.guardrail_service import GuardrailService

logger = logging.getLogger(__name__)


def _redact(guardrail: GuardrailService, text: Optional[str]) -> Dict[str, str]:
    """Run ``text`` through the guardrail and return ``{text, action}``.

    Empty/None passes through unchanged with ``action='NONE'``. When the
    guardrail call itself errors we fall back to an empty string + ``ERROR``
    action so the caller can persist the rest of the record without leaking
    raw user content.
    """
    if not text:
        return {'text': '', 'action': 'NONE'}
    if not guardrail.enabled:
        # No guardrail provisioned — surface that as the action so the operator
        # can spot environments where redaction is off.
        return {'text': text, 'action': 'DISABLED'}
    result = guardrail.apply(text=text, source='OUTPUT')
    action = result.get('action', 'NONE')
    if action == 'ERROR':
        logger.warning(
            "guardrail apply errored — dropping field from feedback write"
        )
        return {'text': '', 'action': 'ERROR'}
    if action == 'GUARDRAIL_INTERVENED':
        # ApplyGuardrail returns the anonymized output in `outputs[].text`,
        # which GuardrailService surfaces as `message`. Fall back to a marker
        # if the guardrail blocked outright with no replacement.
        replacement = result.get('message') or '[REDACTED]'
        return {'text': replacement, 'action': action}
    return {'text': text, 'action': action}


class FeedbackService:
    """DynamoDB-backed CRUD service for per-turn user feedback."""

    def __init__(
        self,
        *,
        table_name: Optional[str] = None,
        region: Optional[str] = None,
        guardrail: Optional[GuardrailService] = None,
        table: Any = None,
    ) -> None:
        """Bind the service to a DynamoDB table + guardrail.

        Args:
            table_name: DDB table; defaults to env ``FEEDBACK_TABLE``.
            region: AWS region; defaults to env ``AWS_REGION`` (or us-east-1).
            guardrail: Optional override (test seam).
            table: Pre-built ``boto3.resource('dynamodb').Table(...)`` (test seam).
        """
        self._table_name = table_name or os.environ.get('FEEDBACK_TABLE', '')
        self._region = region or os.environ.get('AWS_REGION', 'us-east-1')
        self._guardrail = guardrail or GuardrailService()
        self._table = table
        if not self._table_name:
            logger.warning(
                "FEEDBACK_TABLE is empty — feedback endpoints will return 503"
            )

    @property
    def configured(self) -> bool:
        """True when the table name is set; otherwise endpoints short-circuit."""
        return bool(self._table_name)

    def _get_table(self):
        if self._table is None:
            self._table = boto3.resource(
                'dynamodb', region_name=self._region
            ).Table(self._table_name)
        return self._table

    def record(
        self,
        *,
        ontology_id: str,
        user_id: str,
        session_id: str,
        turn_id: str,
        rating: str,
        comment: str,
        question: Optional[str] = None,
        answer: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Write one feedback row, returning the persisted item.

        Args:
            ontology_id: Partition key — required.
            user_id: Authenticated user id; 'anonymous' for unauthenticated paths.
            session_id: Chat session id (already padded to ≥33 chars by caller).
            turn_id: AG-UI turn id the feedback applies to.
            rating: ``'up'`` or ``'down'``.
            comment: Free-text reason — guardrail-redacted before write.
            question: Original user question — redacted before write.
            answer: Assistant answer text being rated — first 500 chars, redacted.
            user_email: Authenticated user's email from the JWT, persisted so the
                admin tab can show a human-readable identity instead of the raw
                Cognito sub. Optional — empty string when unauthenticated or the
                token carried no email claim.

        Returns:
            The full DDB item that was written (suitable to return from the API).

        Raises:
            ValueError: rating not in ``{'up','down'}`` OR service not configured.
        """
        if rating not in ('up', 'down'):
            raise ValueError(f"rating must be 'up' or 'down', got: {rating}")
        if not self.configured:
            raise ValueError("FEEDBACK_TABLE is not configured")

        comment_red = _redact(self._guardrail, comment)
        question_red = _redact(self._guardrail, question)
        # Truncate before redaction so the guardrail call cost is bounded.
        answer_clipped = (answer or '')[:500]
        answer_red = _redact(self._guardrail, answer_clipped)
        # If any field hit ERROR we still write the rest — record the most
        # severe action seen so an admin can spot dropped fields in the UI.
        guardrail_actions = {
            comment_red['action'],
            question_red['action'],
            answer_red['action'],
        }
        if 'ERROR' in guardrail_actions:
            overall = 'ERROR'
        elif 'GUARDRAIL_INTERVENED' in guardrail_actions:
            overall = 'GUARDRAIL_INTERVENED'
        elif 'DISABLED' in guardrail_actions:
            overall = 'DISABLED'
        else:
            overall = 'NONE'

        feedback_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        item: Dict[str, Any] = {
            'ontologyId': ontology_id,
            # Sort key gives newest-first via descending scan; the uuid suffix
            # disambiguates writes that land in the same millisecond.
            'sk': f"{created_at}#{feedback_id}",
            'feedbackId': feedback_id,
            'createdAt': created_at,
            'sessionId': session_id,
            'turnId': turn_id,
            'userId': user_id or 'anonymous',
            # Persist the email so the admin tab can show a human identity; the
            # sub (userId) is kept as the stable fallback for old rows.
            'userEmail': user_email or '',
            'rating': rating,
            'comment': comment_red['text'],
            'question': question_red['text'],
            'answer': answer_red['text'],
            'guardrailAction': overall,
        }
        try:
            self._get_table().put_item(Item=item)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.error("feedback put_item failed for %s: %s", feedback_id, exc)  # nosemgrep: logging-error-without-handling — log-then-reraise is correct; caller handles the exception
            raise
        return item

    def list_for_ontology(
        self,
        *,
        ontology_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return feedback rows for one ontology, newest first.

        Args:
            ontology_id: Partition key.
            limit: Cap; DDB will paginate further on the operator's request.

        Returns:
            List of plain-dict items (DDB Decimal not surfaced — fields are strings).
        """
        if not self.configured:
            return []
        try:
            response = self._get_table().query(
                KeyConditionExpression=Key('ontologyId').eq(ontology_id),
                ScanIndexForward=False,  # descending sort key → newest first
                Limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "feedback query failed for ontology=%s: %s", ontology_id, exc,
            )
            return []
        return list(response.get('Items') or [])

    def delete(self, *, ontology_id: str, feedback_id: str) -> None:
        """Delete one feedback row.

        DDB requires the full primary key; we recover the sort key by
        querying the partition for the matching feedbackId. There is no
        ``feedbackId``-keyed GSI because admin volume is low and the read
        cost per delete is bounded by the per-ontology row count.

        Raises:
            ValueError: service not configured, or no row matched.
        """
        if not self.configured:
            raise ValueError("FEEDBACK_TABLE is not configured")
        # Locate the sk for this feedback_id within the ontology partition.
        response = self._get_table().query(
            KeyConditionExpression=Key('ontologyId').eq(ontology_id),
            FilterExpression='feedbackId = :fid',
            ExpressionAttributeValues={':fid': feedback_id},
            ProjectionExpression='sk, feedbackId',
        )
        items = response.get('Items') or []
        if not items:
            raise ValueError(f"feedback {feedback_id} not found")
        sk = items[0]['sk']
        self._get_table().delete_item(
            Key={'ontologyId': ontology_id, 'sk': sk},
        )
