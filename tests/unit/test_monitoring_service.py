"""Unit tests for MonitoringService — per-layer resolution + correction breakdown.

Uses moto's DDB mock to seed a chat-sessions table with turns carrying
``totals.provenance.tier`` and asserts the service buckets them into the four
resolution layers, scopes by ontologyId, detects corrections on user turns, and
correlates with a (mocked) lessons count.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

import boto3
import pytest

# Make the rest-api package importable.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

from moto import mock_aws  # type: ignore  # noqa: E402

from services.monitoring_service import MonitoringService  # noqa: E402


_TABLE_NAME = 'semantic-layer-chat-sessions'


class _FakeMemoryService:
    """Stand-in for AgentCoreMemoryService returning a fixed lessons count."""

    def __init__(self, count: int):
        self._count = count
        self.calls = []

    def list_records(self, *, ontology_id: str, max_results: int = 50):
        self.calls.append(ontology_id)
        return [{"memoryRecordId": str(i)} for i in range(self._count)]


def _build_table() -> None:
    """Provision the chat-sessions table inside the moto mock."""
    client = boto3.client('dynamodb', region_name='us-east-1')
    client.create_table(
        TableName=_TABLE_NAME,
        AttributeDefinitions=[{'AttributeName': 'sessionId', 'AttributeType': 'S'}],
        KeySchema=[{'AttributeName': 'sessionId', 'KeyType': 'HASH'}],
        BillingMode='PAY_PER_REQUEST',
    )


def _assistant(tier: str) -> dict:
    """An assistant turn whose totals carry the given provenance tier."""
    return {
        'role': 'assistant',
        'text': 'answer',
        'turnId': 't',
        # Decimal mirrors how the DDB resource stores numbers — the service must
        # tolerate it (it only reads provenance.tier, a string).
        'totals': {'rowCount': Decimal('3'), 'provenance': {'tier': tier}},
    }


def _user(text: str) -> dict:
    """A user turn with the given text."""
    return {'role': 'user', 'text': text, 'turnId': 'u'}


def _put_session(table, *, session_id: str, ontology_id: str, messages: list) -> None:
    table.put_item(Item={
        'sessionId': session_id,
        'ontologyId': ontology_id,
        'mode': 'vkg',
        'userId': 'user-1',
        'messages': messages,
    })


@pytest.fixture
def table():
    """Yield a fresh moto-mocked chat-sessions table handle."""
    with mock_aws():
        _build_table()
        yield boto3.resource('dynamodb', region_name='us-east-1').Table(_TABLE_NAME)


def _service(table, *, lessons: int = 0) -> MonitoringService:
    return MonitoringService(
        table_name=_TABLE_NAME,
        region='us-east-1',
        ddb_resource=boto3.resource('dynamodb', region_name='us-east-1'),
        memory_service=_FakeMemoryService(lessons),
    )


def test_buckets_by_tier(table):
    """Each tier maps to its resolution bucket; semantic folds vkg+semantic_sql."""
    _put_session(table, session_id='s1', ontology_id='ont-1', messages=[
        _user("how many policies?"), _assistant('governed_metric'),
        _user("show by product"), _assistant('semantic_sql'),
        _user("graph it"), _assistant('vkg'),
        _user("what can I ask?"), _assistant('advisory'),
    ])
    out = _service(table).aggregate(ontology_id='ont-1')

    assert out['sessionCount'] == 1
    res = out['resolution']
    assert res['totalAnswered'] == 4
    buckets = {b['key']: b for b in res['buckets']}
    assert buckets['metric']['count'] == 1
    assert buckets['semantic']['count'] == 2  # semantic_sql + vkg
    assert buckets['advisory']['count'] == 1
    assert buckets['agentic']['count'] == 0
    assert buckets['semantic']['pct'] == 50.0
    # agentic is documented but not implemented
    assert buckets['agentic']['implemented'] is False
    assert buckets['metric']['implemented'] is True


def test_scopes_by_ontology_id(table):
    """A session for another layer is not counted."""
    _put_session(table, session_id='s1', ontology_id='ont-1',
                 messages=[_user("q"), _assistant('governed_metric')])
    _put_session(table, session_id='s2', ontology_id='ont-OTHER',
                 messages=[_user("q"), _assistant('vkg')])
    out = _service(table).aggregate(ontology_id='ont-1')
    assert out['sessionCount'] == 1
    assert out['resolution']['totalAnswered'] == 1
    buckets = {b['key']: b for b in out['resolution']['buckets']}
    assert buckets['semantic']['count'] == 0  # the other layer's vkg turn excluded


def test_turns_without_provenance_are_not_counted(table):
    """Clarification/legacy turns lacking a provenance tier are skipped."""
    _put_session(table, session_id='s1', ontology_id='ont-1', messages=[
        _user("ambiguous?"),
        {'role': 'assistant', 'text': 'which one?', 'turnId': 'c'},  # no totals
        _user("q"), _assistant('semantic_sql'),
    ])
    out = _service(table).aggregate(ontology_id='ont-1')
    assert out['resolution']['totalAnswered'] == 1


def test_correction_detection_and_pct(table):
    """User correction turns are counted with a percentage + examples."""
    _put_session(table, session_id='s1', ontology_id='ont-1', messages=[
        _user("how many policies?"), _assistant('semantic_sql'),
        _user("that's the wrong table, use coverage"), _assistant('semantic_sql'),
        _user("you're missing the fraud filter"), _assistant('semantic_sql'),
    ])
    out = _service(table, lessons=4).aggregate(ontology_id='ont-1')
    corr = out['corrections']
    assert corr['userTurns'] == 3
    assert corr['correctionTurns'] == 2
    assert corr['pct'] == round(100 * 2 / 3, 1)
    assert corr['lessonsExtracted'] == 4
    assert corr['lessonsCapped'] is False  # 4 < 100 ceiling
    assert corr['examples']  # concrete snippets surfaced


def test_lessons_count_capped_flag(table):
    """>=100 lessons reports lessonsCapped=True so the UI can show '100+'."""
    _put_session(table, session_id='s1', ontology_id='ont-1',
                 messages=[_user("q"), _assistant('semantic_sql')])
    out = _service(table, lessons=100).aggregate(ontology_id='ont-1')
    assert out['corrections']['lessonsExtracted'] == 100
    assert out['corrections']['lessonsCapped'] is True


def test_not_configured_returns_empty_shape(monkeypatch):
    """No table wired → fully-shaped zeroed envelope (no crash).

    Clear CHAT_SESSIONS_TABLE so the empty ``table_name`` arg can't fall
    through to an env var another test in the session may have set (the
    service resolves ``table_name or os.environ['CHAT_SESSIONS_TABLE']``).
    """
    monkeypatch.delenv('CHAT_SESSIONS_TABLE', raising=False)
    svc = MonitoringService(
        table_name='', region='us-east-1',
        memory_service=_FakeMemoryService(0),
    )
    out = svc.aggregate(ontology_id='ont-1')
    assert out['configured'] is False
    assert out['sessionCount'] == 0
    assert out['resolution']['totalAnswered'] == 0
    # all five buckets still present so the UI renders consistently
    assert [b['key'] for b in out['resolution']['buckets']] == [
        'metric', 'semantic', 'advisory', 'agentic'
    ]


def test_empty_layer_has_zero_pct(table):
    """A layer with no traffic reports zeros, not NaN/errors."""
    out = _service(table).aggregate(ontology_id='ont-EMPTY')
    assert out['resolution']['totalAnswered'] == 0
    for b in out['resolution']['buckets']:
        assert b['pct'] == 0.0
    assert out['corrections']['pct'] == 0.0


# ── Scan pagination + page-cap truncation ────────────────────────────────────
# moto won't easily produce a >1MB multi-page scan with tiny items, so we drive
# the pagination loop with a fake table that hands back LastEvaluatedKey pages.

class _FakeTable:
    """A boto3-Table stand-in whose scan() returns scripted pages.

    Each page is ``(items, last_evaluated_key | None)``. Records every scan
    kwargs so the test can assert ExclusiveStartKey threading.
    """

    def __init__(self, pages):
        self._pages = pages
        self._i = 0
        self.scan_calls = []

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        items, last_key = self._pages[self._i]
        self._i += 1
        resp = {"Items": items}
        if last_key is not None:
            resp["LastEvaluatedKey"] = last_key
        return resp


def _service_with_table(fake_table, *, lessons: int = 0) -> MonitoringService:
    svc = MonitoringService(
        table_name=_TABLE_NAME, region='us-east-1',
        memory_service=_FakeMemoryService(lessons),
    )
    svc._table = fake_table  # inject past the lazy boto3 builder
    return svc


def test_scan_accumulates_across_pages():
    """Sessions from every page are aggregated, and ExclusiveStartKey is threaded."""
    page1 = ([{'messages': [_user('q'), _assistant('governed_metric')]}], {'sessionId': 'k1'})
    page2 = ([{'messages': [_user('q'), _assistant('vkg')]}], None)
    fake = _FakeTable([page1, page2])
    out = _service_with_table(fake).aggregate(ontology_id='ont-1')

    assert out['sessionCount'] == 2
    assert out['resolution']['totalAnswered'] == 2
    # second scan call must carry the first page's LastEvaluatedKey
    assert fake.scan_calls[1].get('ExclusiveStartKey') == {'sessionId': 'k1'}


def test_scan_stops_at_page_cap_and_warns(caplog):
    """Hitting _MAX_SCAN_PAGES stops scanning and logs a truncation warning."""
    import logging

    import services.monitoring_service as ms

    # Every page returns a LastEvaluatedKey, so without the cap this is infinite.
    endless = [
        ([{'messages': [_user('q'), _assistant('semantic_sql')]}], {'sessionId': f'k{i}'})
        for i in range(ms._MAX_SCAN_PAGES + 5)
    ]
    fake = _FakeTable(endless)
    with caplog.at_level(logging.WARNING):
        out = _service_with_table(fake).aggregate(ontology_id='ont-1')

    # Scanned exactly the cap, no more (truncated, not infinite).
    assert len(fake.scan_calls) == ms._MAX_SCAN_PAGES
    assert out['sessionCount'] == ms._MAX_SCAN_PAGES
    # Truncation is surfaced, never silent.
    assert any('cap' in r.message.lower() or 'truncat' in r.message.lower()
               for r in caplog.records)
