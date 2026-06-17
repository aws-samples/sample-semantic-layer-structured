"""Chat Session write-path helper — kept in sync manually with the Lambda original.

This is the write-path subset of ``lambda/rest-api/services/chat_session_service.py``
ported into the agent container, which cannot import ``lambda/rest-api`` directly.
Only ``create_session``, ``get_session``, ``get_or_create``, ``append_turn`` and
``history_window`` are carried here; the read/list/archive/title endpoints stay
in the Lambda. The message-record shape, TTL, and title backfill MUST stay
identical to the original so reloads render identically.

Schema:
    sessionId (PK, S)
    ontologyId (S)
    mode (S)              -- "vkg" | "semantic-rag"
    userId (S)
    messages (L of M)     -- [{role, text, turnId, reasoningSteps[]}]
    createdAt (S)         -- ISO-8601 UTC
    updatedAt (S)         -- ISO-8601 UTC
    ttl (N)               -- epoch seconds, refreshed on every append
"""

from __future__ import annotations

import logging
import os
import time
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def _to_ddb_safe(value: Any) -> Any:
    """Recursively convert ``float`` to ``Decimal`` for the DynamoDB resource.

    The boto3 DynamoDB resource serializer rejects native ``float`` with
    "Float types are not supported. Use Decimal types instead". The assistant
    turn's ``totals`` payload carries floats (relevance scores, runtimeMs, token
    usage), so persisting it raised — and because ``append_turn`` is called
    inside a fail-soft ``try/except`` in the agent, the assistant turn was
    silently dropped while the (float-free) user turn persisted. That is the
    "user bubble shows, assistant response missing on reload" bug.

    Non-finite floats (NaN/Infinity) are coerced to ``None`` since DynamoDB
    cannot store them either.
    """
    if isinstance(value, bool):
        return value  # bool is an int subclass — keep as-is, never Decimal
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # NaN/Inf  # nosemgrep: useless-eqeq — value != value is the canonical NaN check
            return None
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_ddb_safe(v) for v in value]
    return value

# Sessions live for 24 hours after last activity. Pick a constant rather than
# threading config because the design doc fixes this at 24h.
_TTL_SECONDS: int = 24 * 3600

# Sliding window — number of most recent messages to send back to the agent
# as conversation history. Older turns are summarised by the agent itself if
# present (the design doc leaves the older-turn summary out for now).
_DEFAULT_HISTORY_WINDOW: int = 10

# Maximum length of an auto-derived title from the first user message.
_TITLE_MAX_CHARS: int = 80

# GSI from dynamodb-stack.ts: partition userId, sort updatedAt (desc), KEYS_ONLY.
_USER_INDEX_NAME: str = 'userId-updatedAt-index'

# Lifecycle control #4: cap the number of active (non-archived) sessions a
# single user may hold. On create, the oldest sessions beyond the cap are
# archived so a user can't create sessions without bound. Configurable via env
# so ops can tune it without a redeploy.
_MAX_SESSIONS_PER_USER: int = int(os.environ.get('MAX_SESSIONS_PER_USER', '50'))


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _expiry_epoch() -> int:
    """Return the epoch-seconds value for `ttl` 24h from now."""
    return int(time.time()) + _TTL_SECONDS


class ChatSessionNotFoundError(LookupError):
    """Raised when a session id has no corresponding DDB item."""


class SessionOwnershipError(PermissionError):
    """Raised when a caller's userId does not match the session's stored owner.

    AgentCore Runtime isolates sessions at the microVM level but does NOT
    validate user-to-session ownership: a valid JWT bearing another user's
    sessionId is accepted by the Runtime. Enforcing the binding is the
    application's responsibility — every access to a session must assert the
    authenticated user owns it, or fail closed with this error.
    """


class ChatSessionService:
    """DDB write path for chat session transcripts.

    The class is intentionally side-effect-free at construction time (the
    boto3 resource is built lazily) so unit tests can patch/inject the
    resource before the first call.
    """

    def __init__(
        self,
        *,
        table_name: Optional[str] = None,
        region: Optional[str] = None,
        ddb_resource: Any = None,
    ) -> None:
        """Build a service bound to the chat-sessions DDB table.

        Args:
            table_name: Override for the table name. Defaults to
                ``CHAT_SESSIONS_TABLE`` env var.
            region: AWS region; defaults to ``AWS_REGION`` env var.
            ddb_resource: Optional pre-built ``boto3.resource('dynamodb')``
                instance — used by tests.
        """
        self._table_name = table_name or os.environ.get('CHAT_SESSIONS_TABLE', '')
        self._region = region or os.environ.get('AWS_REGION', 'us-east-1')
        self._ddb_resource = ddb_resource
        self._table = None

        if not self._table_name:
            # Fail loudly: routes that depend on this service should not silently
            # noop. The ``CHAT_SESSIONS_TABLE`` env var is wired in CDK.
            logger.warning(
                "CHAT_SESSIONS_TABLE env var is empty — chat endpoints will error"
            )

    def _get_table(self):
        """Return a cached boto3 Table handle."""
        if self._table is None:
            resource = self._ddb_resource or boto3.resource(
                'dynamodb', region_name=self._region
            )
            self._table = resource.Table(self._table_name)
        return self._table

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------

    def create_session(
        self,
        *,
        session_id: str,
        ontology_id: str,
        mode: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """Insert a new session row.

        Raises:
            ValueError: if a session with the same id already exists.
        """
        # userId backs the ``userId-updatedAt-index`` GSI. DynamoDB rejects an
        # empty string for a key attribute (ValidationException → the whole
        # session silently fails to persist, so it never appears in the chat
        # sidebar). Default to 'anonymous' when the caller couldn't resolve a
        # principal (e.g. JWT sub decode failed) so the row is always indexable.
        item = {
            'sessionId': session_id,
            'ontologyId': ontology_id,
            'mode': mode,
            'userId': user_id or 'anonymous',
            'messages': [],
            'createdAt': _now_iso(),
            'updatedAt': _now_iso(),
            'ttl': _expiry_epoch(),
        }
        try:
            self._get_table().put_item(
                Item=item,
                ConditionExpression='attribute_not_exists(sessionId)',
            )
        except ClientError as exc:
            if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
                raise ValueError(f"session already exists: {session_id}") from exc
            raise
        # Lifecycle control #4: keep the user under the active-session cap.
        # Best-effort — a capping failure must never block a legitimate create.
        try:
            self.enforce_session_cap(user_id=item['userId'])
        except Exception as exc:  # noqa: BLE001 — capping is non-critical
            logger.warning('session cap enforcement failed (non-fatal) user=%s: %s',
                           item['userId'], exc)
        return item

    def enforce_session_cap(self, *, user_id: str) -> int:
        """Archive a user's oldest sessions beyond ``_MAX_SESSIONS_PER_USER``.

        Queries the ``userId-updatedAt-index`` GSI newest-first and archives
        every active session ranked beyond the cap. Bounds runaway session
        creation per user (lifecycle control #4). Never silent: logs how many
        sessions it evicted.

        Args:
            user_id: The principal whose session count to bound.

        Returns:
            The number of sessions newly archived (0 when under the cap).
        """
        if not user_id:
            return 0
        table = self._get_table()
        # KEYS_ONLY GSI, newest-first. We only need the keys past the cap; pull
        # one page large enough to see them (cap + a small margin).
        resp = table.query(
            IndexName=_USER_INDEX_NAME,
            KeyConditionExpression='userId = :uid',
            ExpressionAttributeValues={':uid': user_id},
            ScanIndexForward=False,  # newest first
            Limit=_MAX_SESSIONS_PER_USER + 25,
        )
        keys = [item['sessionId'] for item in resp.get('Items', [])]
        overflow = keys[_MAX_SESSIONS_PER_USER:]
        archived = 0
        for session_id in overflow:
            try:
                table.update_item(
                    Key={'sessionId': session_id},
                    UpdateExpression='SET archived = :true, updatedAt = :now',
                    ExpressionAttributeValues={
                        ':true': True, ':now': _now_iso(), ':false': False,
                    },
                    # Skip rows already archived so a re-run is a no-op.
                    ConditionExpression=(
                        'attribute_exists(sessionId) AND '
                        '(attribute_not_exists(archived) OR archived = :false)'
                    ),
                )
                archived += 1
            except ClientError as exc:
                if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    raise
        if archived:
            logger.info('session cap: archived %d oldest session(s) for user=%s '
                        '(cap=%d)', archived, user_id, _MAX_SESSIONS_PER_USER)
        return archived

    def get_session(self, *, session_id: str) -> Dict[str, Any]:
        """Return the full session item.

        Raises:
            ChatSessionNotFoundError: if no item with that id is present.
        """
        resp = self._get_table().get_item(Key={'sessionId': session_id})
        item = resp.get('Item')
        if not item:
            raise ChatSessionNotFoundError(session_id)
        return item

    def get_session_owned(self, *, session_id: str, user_id: str) -> Dict[str, Any]:
        """Return the session item only if ``user_id`` owns it.

        Args:
            session_id: Session identifier to fetch.
            user_id: The authenticated principal (Cognito sub). Empty resolves
                to ``'anonymous'``, matching ``create_session``'s default.

        Returns:
            The full DDB item when the caller owns it.

        Raises:
            ChatSessionNotFoundError: if no item with that id is present.
            SessionOwnershipError: if the stored ``userId`` differs from the
                caller's ``user_id``.
        """
        item = self.get_session(session_id=session_id)
        owner = item.get('userId') or 'anonymous'
        if owner != (user_id or 'anonymous'):
            raise SessionOwnershipError(session_id)
        return item

    def get_or_create(
        self,
        *,
        session_id: str,
        ontology_id: str,
        mode: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """Return an existing session or create one if it doesn't exist.

        Raises:
            SessionOwnershipError: when the session exists but is owned by a
                different user. A valid JWT is not authorization to an
                arbitrary sessionId — the caller must own it.
        """
        try:
            return self.get_session_owned(session_id=session_id, user_id=user_id)
        except ChatSessionNotFoundError:
            return self.create_session(
                session_id=session_id,
                ontology_id=ontology_id,
                mode=mode,
                user_id=user_id,
            )

    def append_turn(
        self,
        *,
        session_id: str,
        role: str,
        text: str,
        turn_id: str,
        reasoning_steps: Optional[List[Dict[str, Any]]] = None,
        totals: Optional[Dict[str, Any]] = None,
        thinking_text: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """Append one message to the transcript and refresh the TTL.

        Args:
            session_id: Existing session identifier.
            role: ``user`` or ``assistant``.
            text: Plain-text body for this turn.
            turn_id: Frontend-minted UUID for the turn (lets the UI correlate
                streaming events with the persisted message).
            reasoning_steps: Per-turn AG-UI tool-call records.
            totals: ``run_finished.totals`` payload (sql/rows/kbSources) so
                a reload restores the rich result panel under the assistant
                bubble. Only meaningful for assistant turns.
            user_id: When provided (non-None), the write is guarded by an
                atomic DynamoDB condition ``userId = :uid`` so a caller can
                never append to a session owned by another user — even if a
                higher-level ownership check were bypassed. This is the
                authoritative, DB-level enforcement of session ownership.

        Raises:
            ValueError: when ``role`` is not one of the two accepted values.
            SessionOwnershipError: when ``user_id`` is given and does not match
                the session's stored owner (or the session is gone).
        """
        if role not in ('user', 'assistant'):
            raise ValueError(f"unsupported role: {role!r}")

        message: Dict[str, Any] = {
            'role': role,
            'text': text,
            'turnId': turn_id,
            'reasoningSteps': reasoning_steps or [],
        }
        if totals:
            message['totals'] = totals
        # Persisted only when the run actually streamed thinking — empty
        # strings are dropped so DDB items stay tight.
        if thinking_text:
            message['thinking'] = thinking_text
        # The DynamoDB resource serializer rejects native float (scores,
        # runtimeMs, token usage in totals/reasoning_steps). Convert the whole
        # record to Decimal-safe form so the assistant turn actually persists
        # instead of being dropped by the caller's fail-soft except.
        message = _to_ddb_safe(message)
        # Atomic list_append + TTL refresh + updatedAt bump. Using
        # if_not_exists for the messages list is defensive against a race
        # where the item exists without that attribute.
        update_expr = (
            'SET messages = list_append(if_not_exists(messages, :empty), :m), '
            'updatedAt = :now, #ttl = :ttl'
        )
        expr_names: Dict[str, str] = {'#ttl': 'ttl'}
        expr_values: Dict[str, Any] = {
            ':empty': [],
            ':m': [message],
            ':now': _now_iso(),
            ':ttl': _expiry_epoch(),
        }

        # Backfill title from the first user message. if_not_exists keeps this
        # idempotent — only the first user turn sets it; later calls no-op.
        if role == 'user':
            update_expr += ', title = if_not_exists(title, :title)'
            expr_values[':title'] = text[:_TITLE_MAX_CHARS]

        # Default guard: the row must exist. When the caller supplies a user_id,
        # tighten it to an atomic ownership assertion so a forged sessionId can
        # never be written to a session owned by someone else.
        condition_expr = 'attribute_exists(sessionId)'
        if user_id is not None:
            condition_expr += ' AND userId = :uid'
            expr_values[':uid'] = user_id or 'anonymous'

        try:
            self._get_table().update_item(
                Key={'sessionId': session_id},
                UpdateExpression=update_expr,
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
                ConditionExpression=condition_expr,
            )
        except ClientError as exc:
            # With an ownership guard, a failed condition means either the row
            # vanished or the caller doesn't own it — both are ownership
            # failures from the caller's perspective. Without a guard, re-raise
            # the original error (preserves prior behaviour).
            if user_id is not None and \
                    exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
                raise SessionOwnershipError(session_id) from exc
            raise

    def history_window(
        self,
        *,
        session_id: str,
        n: int = _DEFAULT_HISTORY_WINDOW,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return the last ``n`` messages in chronological order.

        Returns an empty list for missing sessions so the caller can treat a
        missing transcript the same as an empty one (e.g. first turn).

        Args:
            session_id: Session whose transcript to read.
            n: Number of most recent messages to return.
            user_id: When provided, ownership is enforced — a session owned by
                another user yields ``[]`` (a security warning is logged) so a
                forged sessionId cannot leak the victim's conversation context.
        """
        try:
            if user_id is not None:
                session = self.get_session_owned(
                    session_id=session_id, user_id=user_id
                )
            else:
                session = self.get_session(session_id=session_id)
        except ChatSessionNotFoundError:
            return []
        except SessionOwnershipError:
            logger.warning(
                'history window: session %s not owned by caller — returning '
                'empty history (possible session-hijack attempt)', session_id
            )
            return []
        messages = session.get('messages') or []
        return messages[-n:]
