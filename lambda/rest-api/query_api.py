"""
Query API Endpoints

Provides REST API endpoints for natural language query processing:
- AG-UI streaming chat (multi-turn chat with reasoning)
- Query suggestions
- Per-turn 👍/👎 feedback persistence
- Session list / fetch / archive
"""

import json
import logging
import os
import uuid
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.agentcore_service import AgentCoreService
from services.chat_metrics import ChatMetrics
from services.chat_session_service import (
    ChatSessionNotFoundError,
    ChatSessionService,
    SessionOwnershipError,
)
from services.feedback_service import FeedbackService
from services.guardrail_service import GuardrailService
from services.obo_middleware import get_principal, require_obo
from services.ontology_service import OntologyService

logger = logging.getLogger(__name__)

# Create FastAPI app for query endpoints
app = FastAPI(title="Query API")

# Initialize services
agentcore_service = AgentCoreService()
chat_sessions = ChatSessionService()
guardrail = GuardrailService()
chat_metrics = ChatMetrics()
ontology_service = OntologyService()
feedback_service = FeedbackService()


# Pydantic models for request/response validation
class FeedbackRequest(BaseModel):
    """Body for ``POST /query/feedback``.

    Captures a 👍/👎 rating + optional comment for a single assistant turn.
    The body is persisted into the per-ontology DynamoDB feedback table
    (``FEEDBACK_TABLE``); ``comment``, ``question`` and ``answer`` are
    PII-redacted via Bedrock Guardrails before write. Surfaced to admins
    via the "Feedback" tab — see ``feedback_api.py``.
    """

    sessionId: str = Field(min_length=1)
    ontologyId: str = Field(min_length=1)
    turnId: str = Field(min_length=1)
    rating: Literal['up', 'down']
    comment: str = Field(default='', max_length=2000)
    question: str = Field(default='', max_length=2000)
    answer: str = Field(default='', max_length=4000)


# AgentCore session ids must be ≥33 chars; pad shorter ones rather than rejecting
# the user's frontend-minted UUID outright.
_MIN_RUNTIME_SESSION_ID_LEN = 33


def _runtime_session_id(session_id: str) -> str:
    """Return a runtime session id that satisfies AgentCore's min length rule."""
    if len(session_id) >= _MIN_RUNTIME_SESSION_ID_LEN:
        return session_id
    # Stable padding (deterministic per sessionId) so the same chat keeps
    # routing to the same AgentCore runtime session.
    pad_len = _MIN_RUNTIME_SESSION_ID_LEN - len(session_id)
    return session_id + ('-' * pad_len)




# ============================================================================
# Query Suggestions Endpoint
# ============================================================================

@app.get("/suggestions/{ontology_id}")
async def get_query_suggestions(ontology_id: str):
    """
    Generate AI-powered suggested questions for a semantic metadata layer.

    Invokes the Query Suggestions Agent synchronously. The agent retrieves
    schema context from the Bedrock Knowledge Base and generates 5-8 diverse
    questions relevant to the ontology's data model.

    Args:
        ontology_id: The ontology config ID to generate suggestions for

    Returns:
        JSON with 'suggestions' list: [{"category": str, "question": str}]
    """
    try:
        logger.info(f"Generating query suggestions for ontology: {ontology_id}")
        result = agentcore_service.invoke_suggestions_agent(id=ontology_id)

        if not result.get('success'):
            raise HTTPException(status_code=500, detail="Suggestions agent failed")

        data = result.get('data', {})
        if 'error' in data:
            logger.error(f"Suggestions agent returned error: {data['error']}")
            raise HTTPException(status_code=500, detail=data['error'])

        return JSONResponse(content=data)

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Configuration error for suggestions: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting query suggestions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _ddb_to_jsonable(value):
    """Recursively convert DDB Decimals (and nested dicts/lists) into native
    JSON types so JSONResponse can serialise the session row."""
    from decimal import Decimal
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [_ddb_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _ddb_to_jsonable(v) for k, v in value.items()}
    return value


@app.get("/sessions")
async def list_sessions(
    fastapi_request: Request,
    limit: int = 50,
    cursor: Optional[str] = None,
):
    """Return the caller's chat sessions, newest first.

    Powers the chat-first redesign sidebar (item: chat-first 2026-05-24).
    Each row is hydrated with ``ontologyName`` looked up from the metadata
    table so the frontend can render the rail without a second round-trip.

    Args:
        limit: Page size; defaults to 50.
        cursor: Opaque pagination token returned as ``nextCursor`` from a
            prior call.

    Returns:
        ``{sessions: [...], nextCursor: str | None}``. Each session item
        carries ``sessionId``, ``ontologyId``, ``ontologyName``,
        ``ontologyVersion``, ``mode``, ``title``, ``updatedAt``, ``createdAt``.
    """
    principal = get_principal(fastapi_request)
    user_id = principal.get('userId') or 'anonymous'

    # Cursor is round-tripped as JSON so the frontend can pass it back as a
    # single string. Empty/None means start from the top.
    decoded_cursor: Optional[Dict[str, Any]] = None
    if cursor:
        try:
            decoded_cursor = json.loads(cursor)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid cursor")

    page = chat_sessions.list_for_user(
        user_id=user_id, limit=limit, cursor=decoded_cursor
    )

    # Hydrate ontologyName + ontologyVersion from the metadata table. Cache
    # per-request to avoid repeated DDB reads when many sessions share an
    # ontology. get_metadata_config returns the highest-version (active)
    # record, so its ``version`` is the layer's current/latest version.
    ontology_meta_cache: Dict[str, Dict[str, str]] = {}

    def _ontology_meta(ontology_id: str) -> Dict[str, str]:
        if ontology_id in ontology_meta_cache:
            return ontology_meta_cache[ontology_id]
        try:
            cfg = ontology_service.get_metadata_config(id=ontology_id)
        except Exception:  # noqa: BLE001 — best-effort, not load-bearing
            cfg = None
        meta = {
            'name': (cfg or {}).get('name') or ontology_id,
            # Default to v1 so the UI always has something to show even for
            # rows written before the version field existed.
            'version': (cfg or {}).get('version') or 'v1',
        }
        ontology_meta_cache[ontology_id] = meta
        return meta

    summaries: List[Dict[str, Any]] = []
    for item in page.get('sessions', []):
        item = _ddb_to_jsonable(item)
        ontology_id = item.get('ontologyId') or ''
        meta = _ontology_meta(ontology_id) if ontology_id else {'name': '', 'version': ''}
        summaries.append(
            {
                'sessionId': item.get('sessionId'),
                'ontologyId': ontology_id,
                'ontologyName': meta['name'],
                'ontologyVersion': meta['version'],
                'mode': item.get('mode'),
                'title': item.get('title') or '',
                'updatedAt': item.get('updatedAt'),
                'createdAt': item.get('createdAt'),
            }
        )

    next_cursor_raw = page.get('nextCursor')
    next_cursor = json.dumps(next_cursor_raw) if next_cursor_raw else None
    return JSONResponse(
        content={'sessions': summaries, 'nextCursor': next_cursor}
    )


@app.get("/sessions/{session_id}")
async def get_session(session_id: str, fastapi_request: Request):
    """Return the persisted transcript for a session.

    Used by the frontend on page refresh to restore an in-flight conversation.
    Ownership-enforced: a valid JWT for one user must not read another user's
    transcript by passing its sessionId. Mismatch → 403, missing → 404.
    """
    principal = get_principal(fastapi_request)
    user_id = principal.get('userId') or 'anonymous'
    try:
        session = chat_sessions.get_session_owned(
            session_id=session_id, user_id=user_id
        )
    except ChatSessionNotFoundError:
        raise HTTPException(status_code=404, detail="session not found")
    except SessionOwnershipError:
        raise HTTPException(status_code=403, detail="forbidden")
    session = _ddb_to_jsonable(session)
    # Hydrate the layer name + version (the active/highest-version record) so
    # the chat header can show "name · id · version" on a deep-link/refresh
    # without a second client round-trip. Best-effort: the transcript loads
    # regardless of whether the metadata lookup succeeds.
    ontology_id = session.get('ontologyId') or ''
    if ontology_id:
        try:
            cfg = ontology_service.get_metadata_config(id=ontology_id)
        except Exception:  # noqa: BLE001 — best-effort enrichment
            cfg = None
        if cfg:
            session.setdefault('ontologyName', cfg.get('name') or ontology_id)
            session.setdefault('ontologyVersion', cfg.get('version') or 'v1')
    return JSONResponse(content=session)


@app.delete("/sessions")
async def delete_all_sessions(fastapi_request: Request):
    """Soft-delete (archive) ALL of the caller's chat sessions at once.

    Powers the sidebar "Clear all" action. Scoped to the authenticated
    principal — only the caller's own sessions are archived (same per-user
    GSI as ``GET /sessions``). Idempotent: a second call archives nothing.

    Returns:
        ``{status: 'archived', count: <n newly archived>}``.
    """
    principal = get_principal(fastapi_request)
    user_id = principal.get('userId') or 'anonymous'
    count = chat_sessions.archive_all_for_user(user_id=user_id)
    return JSONResponse(content={'status': 'archived', 'count': count})


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str, fastapi_request: Request):
    """Soft-delete (archive) a chat session.

    The row stays in DDB long enough for the TTL reaper to remove it but is
    excluded from ``GET /sessions`` immediately. This is the "x" action on
    a sidebar row in the chat-first redesign.

    Ownership-enforced: a valid JWT for one user must not archive another
    user's session by passing its sessionId. Mismatch → 403, missing → 404.

    Lessons-learned persistence happens turn-by-turn inside the agent
    runtime via ``LessonsMemoryHooks`` (PII-redacted through Bedrock
    Guardrails). There is no end-of-session reflection step: AgentCore
    Memory's ``SemanticStrategy`` extracts lessons asynchronously from the
    raw turns already written.
    """
    principal = get_principal(fastapi_request)
    user_id = principal.get('userId') or 'anonymous'
    # Verify ownership before archiving. get_session_owned raises 404/403 which
    # we surface directly; only an owned session reaches archive().
    try:
        chat_sessions.get_session_owned(session_id=session_id, user_id=user_id)
        chat_sessions.archive(session_id=session_id)
    except ChatSessionNotFoundError:
        raise HTTPException(status_code=404, detail="session not found")
    except SessionOwnershipError:
        raise HTTPException(status_code=403, detail="forbidden")
    return JSONResponse(content={'status': 'archived', 'sessionId': session_id})


@app.post("/feedback")
async def submit_feedback(request: FeedbackRequest, fastapi_request: Request):
    """Persist user 👍/👎 + comment for one assistant turn into DynamoDB.

    Comment, question and answer fields are passed through Bedrock Guardrails
    (PII anonymization) before insert — see ``services/feedback_service.py``.
    The admin "Feedback" tab reads these rows via ``GET /feedback/{ontologyId}``.
    """
    principal = get_principal(fastapi_request)
    user_id = principal.get('userId') or 'anonymous'
    # Email comes from the verified JWT claims (empty on service-token / local
    # paths); persisted so the admin Feedback tab shows a human identity.
    user_email = principal.get('email') or ''
    runtime_session = _runtime_session_id(request.sessionId)
    try:
        item = feedback_service.record(
            ontology_id=request.ontologyId,
            user_id=user_id,
            session_id=runtime_session,
            turn_id=request.turnId,
            rating=request.rating,
            comment=request.comment,
            question=request.question,
            answer=request.answer,
            user_email=user_email,
        )
    except ValueError as exc:
        # rating validation OR table not configured — both surface as 400 so
        # the frontend's bar shows the message the user typed against.
        msg = str(exc)
        status = 503 if 'not configured' in msg else 400
        raise HTTPException(status_code=status, detail=msg)
    except Exception as exc:  # noqa: BLE001
        logger.error("submit_feedback failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="failed to record feedback")
    return JSONResponse(
        content={'status': 'recorded', 'feedbackId': item['feedbackId']}
    )


# Health check
@app.get("/health")
async def health_check():
    """Health check endpoint for query API"""
    return {"status": "healthy", "service": "query-api"}
