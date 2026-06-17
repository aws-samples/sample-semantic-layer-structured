"""Unit tests for the AgentCore-Memory-backed lessons_api endpoints.

These tests exercise the FastAPI surface in ``lambda/rest-api/lessons_api.py``
with a fake ``AgentCoreMemoryService`` injected so no AWS calls are made.

Three concerns are covered:

  1. ``GET /{ontology_id}`` returns the records the service yields, with the
     wire shape the frontend expects (``{"lessons": [...]}``).
  2. ``GET /{ontology_id}`` short-circuits to ``{"lessons": []}`` when the
     memory resource is not configured (no ``LESSONS_MEMORY_ID``).
  3. ``DELETE /{ontology_id}/{record_id}`` returns 503 when the service is
     not configured, 200 + ``{"deleted": ...}`` on success, and 500 on a
     boto3-level exception.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

import lessons_api  # noqa: E402
from services.agentcore_memory_service import (  # noqa: E402
    AgentCoreMemoryService,
)


def _build_client(*, list_records_return: Any = None, configured: bool = True) -> TestClient:
    """Patch the module-level ``memory_service`` with a controllable fake."""
    fake = MagicMock()
    fake.configured = configured
    if list_records_return is not None:
        fake.list_records.return_value = list_records_return
    else:
        fake.list_records.return_value = []
    lessons_api.memory_service = fake
    return TestClient(lessons_api.app)


def test_list_lessons_returns_records_from_service() -> None:
    records: List[Dict[str, Any]] = [
        {
            'memoryRecordId': 'rec-1',
            'content': 'Always include the WHERE clause',
            'namespaces': ['/lessons/abc/alice/sess-1/'],
            'createdAt': '2026-05-25T12:00:00Z',
        },
        {
            'memoryRecordId': 'rec-2',
            'content': 'Prefer property paths over joins',
            'namespaces': ['/lessons/abc/alice/sess-1/'],
            'createdAt': '2026-05-25T12:05:00Z',
        },
    ]
    client = _build_client(list_records_return=records)

    response = client.get('/abc')

    assert response.status_code == 200
    assert response.json() == {'lessons': records}
    lessons_api.memory_service.list_records.assert_called_once_with(
        ontology_id='abc',
        max_results=50,
    )


def test_list_lessons_respects_limit_query_param() -> None:
    client = _build_client(list_records_return=[])

    response = client.get('/abc?limit=10')

    assert response.status_code == 200
    lessons_api.memory_service.list_records.assert_called_once_with(
        ontology_id='abc',
        max_results=10,
    )


def test_list_lessons_rejects_limit_above_agentcore_ceiling() -> None:
    """AgentCore caps ListMemoryRecords at 100; the query param is bounded to
    match so an over-large limit is a 422, not a swallowed ValidationException
    that silently empties the admin UI."""
    client = _build_client(list_records_return=[])

    response = client.get('/abc?limit=200')

    assert response.status_code == 422
    lessons_api.memory_service.list_records.assert_not_called()


def test_list_lessons_returns_empty_when_unconfigured() -> None:
    """If LESSONS_MEMORY_ID is not set, the service yields [] and the API
    surfaces the same — the admin UI degrades gracefully rather than 500ing."""
    client = _build_client(list_records_return=[], configured=False)

    response = client.get('/abc')

    assert response.status_code == 200
    assert response.json() == {'lessons': []}


def test_service_clamps_max_results_to_agentcore_ceiling() -> None:
    """AgentCoreMemoryService clamps a caller's max_results to 100 before
    calling list_memory_records — defense-in-depth behind the API bound, since
    AgentCore throws (not clamps) on maxResults > 100 and the service's broad
    except would otherwise mask it as an empty list."""
    fake_client = MagicMock()
    fake_client.list_memory_records.return_value = {'memoryRecordSummaries': []}
    service = AgentCoreMemoryService(memory_id='mem-1', client=fake_client)

    service.list_records(ontology_id='abc', max_results=200)

    _, kwargs = fake_client.list_memory_records.call_args
    assert kwargs['maxResults'] == 100
    assert kwargs['namespace'] == '/lessons/abc/'


def test_delete_record_also_deletes_source_events() -> None:
    """delete_record must remove the long-term record AND the session's source
    events, so the SEMANTIC strategy can't re-extract the same lesson (the
    "delete then it reappears" bug). The actor/session are derived from the
    record's namespace ``/lessons/<layerId>/<layerVersion>/<userId>/<session>/``.
    """
    fake_client = MagicMock()
    fake_client.get_memory_record.return_value = {
        'memoryRecord': {
            'namespaces': [
                '/lessons/layer-abc/v1/user-123/sess-xyz/',
            ],
        },
    }
    fake_client.list_events.return_value = {
        'events': [{'eventId': 'evt-1'}, {'eventId': 'evt-2'}],
    }
    service = AgentCoreMemoryService(memory_id='mem-1', client=fake_client)

    service.delete_record(memory_record_id='rec-1')

    # The long-term record is deleted.
    fake_client.delete_memory_record.assert_called_once_with(
        memoryId='mem-1', memoryRecordId='rec-1',
    )
    # Events were listed with the actor/session parsed from the namespace
    # (actorId joins everything between "/lessons/" and the trailing session).
    _, list_kwargs = fake_client.list_events.call_args
    assert list_kwargs['actorId'] == 'layer-abc/v1/user-123'
    assert list_kwargs['sessionId'] == 'sess-xyz'
    # Both source events are deleted so nothing remains to re-extract.
    assert fake_client.delete_event.call_count == 2
    deleted_ids = {
        c.kwargs['eventId'] for c in fake_client.delete_event.call_args_list
    }
    assert deleted_ids == {'evt-1', 'evt-2'}


def test_delete_record_falls_back_to_record_only_on_bad_namespace() -> None:
    """If the namespace can't be parsed into actor/session, we still delete the
    record (original behaviour) but skip — and don't crash on — event cleanup."""
    fake_client = MagicMock()
    fake_client.get_memory_record.return_value = {
        'memoryRecord': {'namespaces': ['/unexpected/shape/']},
    }
    service = AgentCoreMemoryService(memory_id='mem-1', client=fake_client)

    service.delete_record(memory_record_id='rec-1')

    fake_client.delete_memory_record.assert_called_once_with(
        memoryId='mem-1', memoryRecordId='rec-1',
    )
    fake_client.list_events.assert_not_called()
    fake_client.delete_event.assert_not_called()


def test_delete_record_event_cleanup_failure_does_not_raise() -> None:
    """A failure listing/deleting events is best-effort: the long-term record is
    already gone, so a cleanup error must not bubble up as a 500."""
    fake_client = MagicMock()
    fake_client.get_memory_record.return_value = {
        'memoryRecord': {
            'namespaces': ['/lessons/layer-abc/v1/user-123/sess-xyz/'],
        },
    }
    fake_client.list_events.side_effect = RuntimeError('boom')
    service = AgentCoreMemoryService(memory_id='mem-1', client=fake_client)

    # Must not raise despite the list_events failure.
    service.delete_record(memory_record_id='rec-1')

    fake_client.delete_memory_record.assert_called_once()


def test_delete_lesson_returns_deleted_record_id_on_success() -> None:
    client = _build_client()

    response = client.delete('/abc/rec-1')

    assert response.status_code == 200
    assert response.json() == {'deleted': 'rec-1'}
    lessons_api.memory_service.delete_record.assert_called_once_with(
        memory_record_id='rec-1',
    )


def test_delete_lesson_returns_503_when_unconfigured() -> None:
    """The service raises ValueError when LESSONS_MEMORY_ID is empty so the
    operator notices instead of silently dropping deletes."""
    client = _build_client(configured=False)
    lessons_api.memory_service.delete_record.side_effect = ValueError(
        'LESSONS_MEMORY_ID is not configured'
    )

    response = client.delete('/abc/rec-1')

    assert response.status_code == 503
    assert 'not configured' in response.json()['detail']


def test_delete_lesson_returns_500_on_boto_exception() -> None:
    """An unexpected boto-level error is mapped to 500 with a generic detail
    so the underlying error message isn't leaked to the UI."""
    client = _build_client()
    lessons_api.memory_service.delete_record.side_effect = RuntimeError('boom')

    response = client.delete('/abc/rec-1')

    assert response.status_code == 500
    assert response.json()['detail'] == 'failed to delete record'


