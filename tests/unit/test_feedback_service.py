"""Unit tests for ``services/feedback_service.py``.

Exercises the DDB-backed feedback service that replaced the AgentCore-Memory
write path. A fake DDB table + fake guardrail are injected so no AWS calls
are made.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

from services.feedback_service import FeedbackService  # noqa: E402


class _FakeTable:
    """Minimal DDB Table double — captures put_item / supports query/delete."""

    def __init__(self, *, items: List[Dict[str, Any]] | None = None) -> None:
        self.items: List[Dict[str, Any]] = list(items or [])
        self.put_calls: List[Dict[str, Any]] = []
        self.delete_calls: List[Dict[str, Any]] = []

    def put_item(self, *, Item: Dict[str, Any]) -> Dict[str, Any]:
        self.put_calls.append(Item)
        self.items.append(Item)
        return {}

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        # Best-effort: filter items by hard-coded ontology partition value
        # encoded in KeyConditionExpression's argument bindings — we just
        # return everything we've seen for the tests below.
        items = list(self.items)
        # Honor descending sort if requested.
        if kwargs.get('ScanIndexForward') is False:
            items = list(reversed(items))
        # FilterExpression is used by the delete path to find by feedbackId.
        eav = kwargs.get('ExpressionAttributeValues') or {}
        if ':fid' in eav:
            items = [it for it in items if it.get('feedbackId') == eav[':fid']]
        return {'Items': items}

    def delete_item(self, *, Key: Dict[str, Any]) -> Dict[str, Any]:
        self.delete_calls.append(Key)
        self.items = [
            it for it in self.items
            if not (it.get('ontologyId') == Key.get('ontologyId')
                    and it.get('sk') == Key.get('sk'))
        ]
        return {}


def _service(*, intervened: bool = False, enabled: bool = True) -> tuple[FeedbackService, _FakeTable]:
    """Build a FeedbackService with a fake table + guardrail for unit tests."""
    table = _FakeTable()
    guardrail = MagicMock()
    guardrail.enabled = enabled
    if intervened:
        guardrail.apply.return_value = {
            'action': 'GUARDRAIL_INTERVENED',
            'message': '[REDACTED-PII]',
            'blocked': False,
        }
    else:
        # Echo input back as the anonymized output (action=NONE means no change).
        guardrail.apply.side_effect = lambda *, text, source='OUTPUT': {
            'action': 'NONE', 'message': '', 'blocked': False,
        }
    svc = FeedbackService(
        table_name='feedback-test', guardrail=guardrail, table=table,
    )
    return svc, table


def test_record_writes_redacted_item_to_ddb() -> None:
    svc, table = _service(intervened=True)

    item = svc.record(
        ontology_id='ont-abc',
        user_id='alice',
        session_id='s' * 40,
        turn_id='t-1',
        rating='up',
        comment='please call me at 555-1234',
        question='How many customers?',
        answer='42 customers',
    )

    assert len(table.put_calls) == 1
    put = table.put_calls[0]
    assert put['ontologyId'] == 'ont-abc'
    assert put['userId'] == 'alice'
    assert put['rating'] == 'up'
    # Comment / question / answer all replaced with the guardrail's anonymized output.
    assert put['comment'] == '[REDACTED-PII]'
    assert put['question'] == '[REDACTED-PII]'
    assert put['answer'] == '[REDACTED-PII]'
    assert put['guardrailAction'] == 'GUARDRAIL_INTERVENED'
    # Sort key encodes createdAt followed by feedbackId for stable ordering.
    assert put['sk'].startswith(put['createdAt'])
    assert put['sk'].endswith(put['feedbackId'])
    assert item == put


def test_record_persists_user_email_when_provided() -> None:
    """The email from the JWT is stored on the row so the admin tab can show a
    human identity instead of the raw Cognito sub."""
    svc, table = _service()

    svc.record(
        ontology_id='ont-abc',
        user_id='sub-123',
        session_id='s' * 40,
        turn_id='t-1',
        rating='up',
        comment='',
        user_email='alice@example.com',
    )

    assert table.put_calls[0]['userEmail'] == 'alice@example.com'


def test_record_defaults_user_email_to_empty_string() -> None:
    """Unauthenticated / service-token paths carry no email; the field is still
    written (as '') so old-row fallback logic in the UI is uniform."""
    svc, table = _service()

    svc.record(
        ontology_id='ont-abc',
        user_id='anonymous',
        session_id='s' * 40,
        turn_id='t-1',
        rating='down',
        comment='',
    )

    assert table.put_calls[0]['userEmail'] == ''


def test_record_truncates_answer_to_500_chars_before_redaction() -> None:
    svc, table = _service()
    long_answer = 'x' * 1000

    svc.record(
        ontology_id='o',
        user_id='alice',
        session_id='s' * 40,
        turn_id='t',
        rating='down',
        comment='',
        answer=long_answer,
    )

    assert table.put_calls[0]['answer'] == 'x' * 500


def test_record_rejects_invalid_rating() -> None:
    svc, _ = _service()
    with pytest.raises(ValueError, match='rating must be'):
        svc.record(
            ontology_id='o',
            user_id='alice',
            session_id='s' * 40,
            turn_id='t',
            rating='maybe',
            comment='',
        )


def test_record_raises_when_table_unconfigured() -> None:
    """503 on the API surface — operator should notice missing config."""
    svc = FeedbackService(table_name='', guardrail=MagicMock(enabled=False))
    with pytest.raises(ValueError, match='not configured'):
        svc.record(
            ontology_id='o',
            user_id='alice',
            session_id='s' * 40,
            turn_id='t',
            rating='up',
            comment='',
        )


def test_list_for_ontology_returns_items_newest_first() -> None:
    svc, table = _service()
    table.items = [
        {'ontologyId': 'o', 'sk': 'a', 'feedbackId': '1', 'createdAt': 'a'},
        {'ontologyId': 'o', 'sk': 'b', 'feedbackId': '2', 'createdAt': 'b'},
    ]
    out = svc.list_for_ontology(ontology_id='o', limit=10)
    # _FakeTable.query honors ScanIndexForward=False → reversed order.
    assert [it['feedbackId'] for it in out] == ['2', '1']


def test_delete_resolves_sk_via_query() -> None:
    svc, table = _service()
    table.items = [
        {'ontologyId': 'o', 'sk': 'sk-1', 'feedbackId': 'fid-1'},
        {'ontologyId': 'o', 'sk': 'sk-2', 'feedbackId': 'fid-2'},
    ]
    svc.delete(ontology_id='o', feedback_id='fid-2')
    assert table.delete_calls == [{'ontologyId': 'o', 'sk': 'sk-2'}]
    # Side-effect: row removed from the fake table.
    assert all(it['feedbackId'] != 'fid-2' for it in table.items)


def test_delete_raises_when_feedback_not_found() -> None:
    svc, table = _service()
    table.items = []
    with pytest.raises(ValueError, match='not found'):
        svc.delete(ontology_id='o', feedback_id='missing')
