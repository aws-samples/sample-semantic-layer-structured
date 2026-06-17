"""Unit tests for ``POST /query/feedback`` and the admin ``/feedback`` API.

The write endpoint persists a 👍/👎 + comment for one assistant turn into
the per-ontology DynamoDB feedback table; the admin API exposes list/delete.
Both tests inject a fake ``FeedbackService`` so no AWS calls are made.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

import feedback_api  # noqa: E402
import query_api  # noqa: E402


def _query_client() -> TestClient:
    fake = MagicMock()
    fake.configured = True
    fake.record.return_value = {'feedbackId': 'fid-1'}
    query_api.feedback_service = fake
    return TestClient(query_api.app)


def _admin_client() -> TestClient:
    fake = MagicMock()
    fake.configured = True
    feedback_api.feedback_service = fake
    return TestClient(feedback_api.app)


# ---------------------------------------------------------------------------
# Write surface (mounted under /query in main.py; here we hit query_api.app)
# ---------------------------------------------------------------------------

def test_submit_feedback_records_thumbs_up() -> None:
    client = _query_client()

    response = client.post(
        '/feedback',
        json={
            'sessionId': 's' * 40,
            'ontologyId': 'ont-abc',
            'turnId': 't-1',
            'rating': 'up',
            'question': 'How many customers?',
            'answer': 'There are 42 customers.',
        },
    )

    assert response.status_code == 200
    assert response.json() == {'status': 'recorded', 'feedbackId': 'fid-1'}
    kwargs = query_api.feedback_service.record.call_args.kwargs
    assert kwargs['rating'] == 'up'
    assert kwargs['ontology_id'] == 'ont-abc'
    assert kwargs['turn_id'] == 't-1'
    # No JWT in TestClient → falls back to 'anonymous' so DDB writes still
    # carry a user attribution.
    assert kwargs['user_id'] == 'anonymous'


def test_submit_feedback_pads_short_session_id() -> None:
    """Frontend may mint short uuids; the runtime session id must be ≥33."""
    client = _query_client()

    response = client.post(
        '/feedback',
        json={
            'sessionId': 'short',
            'ontologyId': 'ont-abc',
            'turnId': 't-1',
            'rating': 'down',
            'comment': 'wrong table',
        },
    )

    assert response.status_code == 200
    kwargs = query_api.feedback_service.record.call_args.kwargs
    assert len(kwargs['session_id']) >= 33
    assert kwargs['session_id'].startswith('short')


def test_submit_feedback_rejects_invalid_rating() -> None:
    client = _query_client()

    response = client.post(
        '/feedback',
        json={
            'sessionId': 's' * 40,
            'ontologyId': 'ont-abc',
            'turnId': 't-1',
            'rating': 'maybe',
        },
    )

    assert response.status_code == 422  # pydantic Literal enforcement


def test_submit_feedback_returns_503_when_table_unconfigured() -> None:
    client = _query_client()
    query_api.feedback_service.record.side_effect = ValueError(
        'FEEDBACK_TABLE is not configured'
    )

    response = client.post(
        '/feedback',
        json={
            'sessionId': 's' * 40,
            'ontologyId': 'ont-abc',
            'turnId': 't-1',
            'rating': 'up',
        },
    )

    assert response.status_code == 503


def test_submit_feedback_returns_500_on_record_exception() -> None:
    client = _query_client()
    query_api.feedback_service.record.side_effect = RuntimeError('boom')

    response = client.post(
        '/feedback',
        json={
            'sessionId': 's' * 40,
            'ontologyId': 'ont-abc',
            'turnId': 't-1',
            'rating': 'up',
        },
    )

    assert response.status_code == 500
    assert response.json()['detail'] == 'failed to record feedback'


# ---------------------------------------------------------------------------
# Admin surface — list / delete
# ---------------------------------------------------------------------------

def test_list_feedback_returns_rows_from_service() -> None:
    client = _admin_client()
    feedback_api.feedback_service.list_for_ontology.return_value = [
        {
            'ontologyId': 'ont-abc',
            'feedbackId': 'fid-1',
            'rating': 'up',
            'comment': '[REDACTED]',
            'createdAt': '2026-05-28T12:00:00.000+00:00',
        },
    ]

    response = client.get('/ont-abc?limit=10')

    assert response.status_code == 200
    assert response.json()['feedback'][0]['feedbackId'] == 'fid-1'
    feedback_api.feedback_service.list_for_ontology.assert_called_once_with(
        ontology_id='ont-abc', limit=10,
    )


def test_delete_feedback_returns_404_when_missing() -> None:
    client = _admin_client()
    feedback_api.feedback_service.delete.side_effect = ValueError(
        'feedback fid-999 not found'
    )

    response = client.delete('/ont-abc/fid-999')

    assert response.status_code == 404


def test_delete_feedback_returns_503_when_unconfigured() -> None:
    client = _admin_client()
    feedback_api.feedback_service.delete.side_effect = ValueError(
        'FEEDBACK_TABLE is not configured'
    )

    response = client.delete('/ont-abc/fid-1')

    assert response.status_code == 503


def test_delete_feedback_returns_200_on_success() -> None:
    client = _admin_client()
    feedback_api.feedback_service.delete.return_value = None

    response = client.delete('/ont-abc/fid-1')

    assert response.status_code == 200
    assert response.json() == {'deleted': 'fid-1'}
