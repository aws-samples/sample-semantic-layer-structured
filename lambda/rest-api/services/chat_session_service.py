"""
Chat Session Service — DynamoDB-backed transcript store for AG-UI chat (item #1).

Each session is one DDB item keyed by ``sessionId``. Items hold the full
transcript (user + assistant turns) plus per-turn reasoning steps so that a
browser refresh mid-conversation can restore the chat from
``GET /query/sessions/{id}``. Items expire 24 hours after the most recent
write via the ``ttl`` attribute (configured on the table itself).

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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Sessions live for 24 hours after last activity. Pick a constant rather than
# threading config because the design doc fixes this at 24h.
_TTL_SECONDS: int = 24 * 3600

# Sliding window — number of most recent messages to send back to the agent
# as conversation history. Older turns are summarised by the agent itself if
# present (the design doc leaves the older-turn summary out for now).
_DEFAULT_HISTORY_WINDOW: int = 10

# GSI from dynamodb-stack.ts: partition userId, sort updatedAt (desc), KEYS_ONLY.
_USER_INDEX_NAME: str = 'userId-updatedAt-index'

# Maximum length of an auto-derived title from the first user message.
_TITLE_MAX_CHARS: int = 80

# BatchGetItem hard limit per AWS API.
_BATCH_GET_CHUNK: int = 25


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

    Kept in sync with ``agents/shared/chat_sessions.py``. AgentCore Runtime
    isolates sessions at the microVM level but does NOT validate
    user-to-session ownership, so the application must reject any access to a
    session the authenticated user does not own — surfaced as HTTP 403 by the
    per-id ``GET``/``DELETE /sessions/{id}`` routes.
    """


class ChatSessionService:
    """DDB CRUD for chat session transcripts.

    The class is intentionally side-effect-free at construction time (the
    boto3 resource is built lazily) so unit tests using ``moto`` can patch the
    client before the first call.
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
                instance — used by tests with moto.
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
        item = {
            'sessionId': session_id,
            'ontologyId': ontology_id,
            'mode': mode,
            'userId': user_id,
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
        return item

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
        """Return an existing session or create one if it doesn't exist."""
        try:
            return self.get_session(session_id=session_id)
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

        Raises:
            ValueError: when ``role`` is not one of the two accepted values.
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

        self._get_table().update_item(
            Key={'sessionId': session_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
            ConditionExpression='attribute_exists(sessionId)',
        )

    def history_window(
        self,
        *,
        session_id: str,
        n: int = _DEFAULT_HISTORY_WINDOW,
    ) -> List[Dict[str, Any]]:
        """Return the last ``n`` messages in chronological order.

        Returns an empty list for missing sessions so the caller can treat a
        missing transcript the same as an empty one (e.g. first turn).
        """
        try:
            session = self.get_session(session_id=session_id)
        except ChatSessionNotFoundError:
            return []
        messages = session.get('messages') or []
        return messages[-n:]

    def delete_session(self, *, session_id: str) -> None:
        """Remove a session row. Safe to call on missing ids (no-op)."""
        self._get_table().delete_item(Key={'sessionId': session_id})

    # ---------------------------------------------------------------------
    # Sidebar / per-user list (chat-first redesign 2026-05-24)
    # ---------------------------------------------------------------------

    def list_for_user(
        self,
        *,
        user_id: str,
        limit: int = 50,
        cursor: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """List a user's sessions newest-first, excluding archived rows.

        The GSI ``userId-updatedAt-index`` is KEYS_ONLY, so we Query for keys
        and BatchGetItem the full items in chunks. Archived rows are filtered
        out client-side (the GSI doesn't project ``archived``).

        Args:
            user_id: Cognito-derived principal id.
            limit: Maximum number of sessions returned.
            cursor: Opaque ``LastEvaluatedKey`` from a prior call; pass through
                to continue pagination.

        Returns:
            ``{'sessions': [...], 'nextCursor': dict | None}`` where each
            session is the full DDB item.
        """
        query_kwargs: Dict[str, Any] = {
            'IndexName': _USER_INDEX_NAME,
            'KeyConditionExpression': 'userId = :uid',
            'ExpressionAttributeValues': {':uid': user_id},
            'ScanIndexForward': False,  # newest first
            'Limit': limit,
        }
        if cursor:
            query_kwargs['ExclusiveStartKey'] = cursor

        resp = self._get_table().query(**query_kwargs)
        keys: List[Dict[str, str]] = [
            {'sessionId': item['sessionId']} for item in resp.get('Items', [])
        ]
        next_cursor = resp.get('LastEvaluatedKey')

        if not keys:
            return {'sessions': [], 'nextCursor': next_cursor}

        # BatchGetItem chunked to the AWS hard limit (25 per request).
        items_by_id: Dict[str, Dict[str, Any]] = {}
        ddb_resource = self._ddb_resource or boto3.resource(
            'dynamodb', region_name=self._region
        )
        for start in range(0, len(keys), _BATCH_GET_CHUNK):
            chunk = keys[start:start + _BATCH_GET_CHUNK]
            batch_resp = ddb_resource.batch_get_item(
                RequestItems={self._table_name: {'Keys': chunk}}
            )
            for item in batch_resp.get('Responses', {}).get(self._table_name, []):
                items_by_id[item['sessionId']] = item

        # Preserve GSI sort order; drop archived rows.
        ordered_items = [
            items_by_id[k['sessionId']]
            for k in keys
            if k['sessionId'] in items_by_id
            and not items_by_id[k['sessionId']].get('archived', False)
        ]
        return {'sessions': ordered_items, 'nextCursor': next_cursor}

    def archive(self, *, session_id: str) -> None:
        """Soft-delete: flip ``archived=True`` and bump ``updatedAt``.

        Used by ``DELETE /query/sessions/{id}`` so the row stays queryable
        long enough for the TTL reaper to remove it.

        Raises:
            ChatSessionNotFoundError: when the session doesn't exist.
        """
        try:
            self._get_table().update_item(
                Key={'sessionId': session_id},
                UpdateExpression='SET archived = :true, updatedAt = :now',
                ExpressionAttributeValues={':true': True, ':now': _now_iso()},
                ConditionExpression='attribute_exists(sessionId)',
            )
        except ClientError as exc:
            if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
                raise ChatSessionNotFoundError(session_id) from exc
            raise

    def archive_all_for_user(self, *, user_id: str) -> int:
        """Soft-delete every (non-archived) session owned by ``user_id``.

        Powers ``DELETE /query/sessions`` (the sidebar "Clear all" action).
        Pages the full ``userId-updatedAt-index`` GSI (not just the first
        page like ``list_for_user``) and flips ``archived=True`` on each row
        that isn't already archived. Idempotent: re-running archives nothing.

        Args:
            user_id: Cognito-derived principal id whose sessions to archive.

        Returns:
            The number of sessions newly archived.
        """
        table = self._get_table()
        query_kwargs: Dict[str, Any] = {
            'IndexName': _USER_INDEX_NAME,
            'KeyConditionExpression': 'userId = :uid',
            'ExpressionAttributeValues': {':uid': user_id},
        }
        archived = 0
        while True:
            resp = table.query(**query_kwargs)
            for item in resp.get('Items', []):
                session_id = item.get('sessionId')
                if not session_id:
                    continue
                # The GSI is KEYS_ONLY, so we can't tell archived rows apart
                # here. The conditional skips rows already archived so the
                # count reflects only newly-archived sessions and a re-run is
                # a no-op rather than re-bumping updatedAt.
                try:
                    table.update_item(
                        Key={'sessionId': session_id},
                        UpdateExpression='SET archived = :true, updatedAt = :now',
                        ExpressionAttributeValues={
                            ':true': True, ':now': _now_iso(), ':false': False,
                        },
                        ConditionExpression=(
                            'attribute_exists(sessionId) AND '
                            '(attribute_not_exists(archived) OR archived = :false)'
                        ),
                    )
                    archived += 1
                except ClientError as exc:
                    # Already archived (condition failed) → skip, not an error.
                    if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                        raise
            cursor = resp.get('LastEvaluatedKey')
            if not cursor:
                break
            query_kwargs['ExclusiveStartKey'] = cursor
        return archived

    def set_title(self, *, session_id: str, title: str) -> None:
        """Override the auto-derived title for a session.

        Raises:
            ChatSessionNotFoundError: when the session doesn't exist.
        """
        try:
            self._get_table().update_item(
                Key={'sessionId': session_id},
                UpdateExpression='SET title = :t, updatedAt = :now',
                ExpressionAttributeValues={
                    ':t': title[:_TITLE_MAX_CHARS],
                    ':now': _now_iso(),
                },
                ConditionExpression='attribute_exists(sessionId)',
            )
        except ClientError as exc:
            if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
                raise ChatSessionNotFoundError(session_id) from exc
            raise
