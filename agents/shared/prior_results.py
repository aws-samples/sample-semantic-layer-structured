"""Lazy on-demand fetcher for SQL rows from a prior turn in this chat session.

Token-efficient design:
  * ``to_strands_messages`` injects a one-line pointer
    ``[Prior result] turnId=t-... rows=N sql=...`` into each assistant turn
    that has totals.
  * The model reads the pointer and decides whether the user actually needs
    the rows. Most follow-ups ("again?", "what was that?") can be answered
    from the prose + pointer alone.
  * When the user asks for specifics ("show me row 5", "list them all"),
    the model calls ``get_previous_query_result(turn_id=...)`` and only then
    do the full rows enter the context window — for that one turn, scoped
    to that one tool call.

Implementation: we re-read the chat-sessions DynamoDB row for the current
session, find the message with the matching ``turnId``, and return its
``totals`` payload. The agent's IAM role must be granted ``GetItem`` on the
table and the table name passed via ``CHAT_SESSIONS_TABLE`` env var.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
from typing import Any, Dict, Optional

import boto3
from strands import tool

logger = logging.getLogger(__name__)


# Set per-invocation by the chat-stream entrypoint so the tool can scope its
# DDB lookup to the current chat session without exposing it to the model.
_SESSION_ID_VAR: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    'prior_results_session_id', default=None,
)


def set_session_id(session_id: str) -> None:
    """Bind the chat sessionId to the current async context.

    Called once per AG-UI run from each query agent's ``_chat_stream`` /
    ``_run_query``. The lookup tool reads this contextvar instead of
    accepting sessionId as a model-visible argument so the model can't be
    tricked into reading another user's turn.
    """
    _SESSION_ID_VAR.set(session_id or '')


# Cap rows returned to the model — prevents a runaway 50k-row prior result
# from blowing the context window on a single tool call.
_MAX_ROWS_TO_RETURN = 50


@tool
def get_previous_query_result(turn_id: str) -> str:
    """Fetch full SQL rows + columns from a prior assistant turn in this chat.

    Use this when the user asks about specific rows, columns, or values from
    a result you already produced earlier in the conversation (e.g. "what
    was the third one?", "show me all of them", "remind me of the values").
    The conversation history shows ``[Prior result] turnId=... rows=...`` for
    every prior turn that has stored rows — pass that ``turnId`` here.

    Do NOT call this for prior turns without a ``[Prior result]`` pointer —
    no rows were stored for those turns.

    Args:
        turn_id: The ``turnId`` from the ``[Prior result]`` pointer.

    Returns:
        JSON string with ``columns``, ``rows`` (capped at 50), ``rowCount``,
        ``truncated`` (bool), and ``sql``. Returns an error JSON if the turn
        is not found or has no stored result.
    """
    session_id = _SESSION_ID_VAR.get() or ''
    if not session_id:
        return json.dumps({'error': 'no chat session in context'})

    table_name = os.getenv('CHAT_SESSIONS_TABLE', '').strip()
    if not table_name:
        return json.dumps({'error': 'CHAT_SESSIONS_TABLE env var not set'})

    region = os.getenv('AWS_REGION', 'us-east-1')
    try:
        ddb = boto3.resource('dynamodb', region_name=region)
        table = ddb.Table(table_name)
        resp = table.get_item(Key={'sessionId': session_id})
    except Exception as exc:  # noqa: BLE001 — surface as tool error
        logger.exception("get_previous_query_result DDB read failed")
        return json.dumps({'error': f'DDB read failed: {exc}'})

    item = resp.get('Item') or {}
    messages = item.get('messages') or []
    # User and assistant turns share the same ``turnId`` — only assistant
    # turns carry ``totals``, so filter to those before matching.
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get('role') != 'assistant':
            continue
        if msg.get('turnId') != turn_id:
            continue
        totals = msg.get('totals')
        if not isinstance(totals, dict):
            return json.dumps({
                'error': f'turn {turn_id} has no stored result',
            })
        rows = totals.get('rows') or []
        truncated = (
            bool(totals.get('truncated')) or len(rows) > _MAX_ROWS_TO_RETURN
        )
        return json.dumps({
            'turnId': turn_id,
            'sql': totals.get('sql') or '',
            'columns': totals.get('columns') or [],
            'rows': rows[:_MAX_ROWS_TO_RETURN],
            'rowCount': totals.get('rowCount'),
            'truncated': truncated,
        })
    return json.dumps({'error': f'turn {turn_id} not found in session'})
