"""Unit tests for agents.shared.advisory.

Covers the regex intent fast-path, governed-metric enumeration (PUBLISHED only,
keyed by layer id), the structural no-SQL guarantee, KB-context parsing of both
JSON-string and dict shapes, and the empty-KB degrade. The synthesize + KB
callables are injected, so these tests need no Strands/Bedrock — just stubs.
"""
from unittest.mock import MagicMock

import json
import pytest

from agents.shared.advisory import (
    build_advisory_answer,
    list_governed_metrics,
    regex_is_advisory,
    _parse_kb_context,
)


# ---------------------------------------------------------------------------
# regex_is_advisory — the router fast-path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "what can I ask here?",
    "what metrics could I calculate with this data?",
    "what kind of metrics are available?",
    "explain the coverage table",
    "describe the schema",
    "what's in this layer?",
    "what questions can I ask?",
    # The exact screenshot question that motivated this feature — passive
    # "metrics that could be calculated" phrasing must hit the fast-path.
    "What are some common metrics that could be calculated with this data?",
])
def test_regex_matches_advisory_phrasing(q):
    """Clear capability/discovery phrasing is recognized as advisory."""
    assert regex_is_advisory(q) is True


@pytest.mark.parametrize("q", [
    "how many parties are there?",
    "what is the total payout amount?",
    "list the policies with the highest premium",
    "count coverage rows by state",
    # Meta-WORDED data query — names "party types" but asks for actual rows.
    # Must NOT be pulled into advisory (the conservative-default guard).
    "List the top 5 most common party types and their human-readable descriptions.",
    "",
])
def test_regex_does_not_match_data_queries(q):
    """Real data queries (incl. meta-worded ones) are NOT matched — the router
    falls through to the classifier / data path on a negative match."""
    assert regex_is_advisory(q) is False


# ---------------------------------------------------------------------------
# list_governed_metrics
# ---------------------------------------------------------------------------

def _metrics_table_with(items):
    """A stub DDB Table whose query() returns the given items."""
    table = MagicMock()
    table.query.return_value = {'Items': items}
    return table


def test_list_governed_metrics_published_only():
    """Only PUBLISHED metrics are returned; DRAFT rows are filtered out."""
    table = _metrics_table_with([
        {'metric_id': 'm1', 'name': 'Revenue', 'description': 'TTM revenue',
         'lifecycle': 'PUBLISHED'},
        {'metric_id': 'm2', 'name': 'Draft Metric', 'description': 'wip',
         'lifecycle': 'DRAFT'},
    ])
    metrics = list_governed_metrics(layer_id='layer-1', metrics_table=table)
    assert metrics == [{'metric_id': 'm1', 'name': 'Revenue', 'description': 'TTM revenue'}]


def test_list_governed_metrics_queries_by_layer_id():
    """The query is keyed by NS#<layer_id> + SK begins_with METRIC# (id only)."""
    table = _metrics_table_with([])
    list_governed_metrics(layer_id='layer-xyz', metrics_table=table)
    assert table.query.called
    # The KeyConditionExpression is a boto3 condition object; assert the call
    # happened with a KeyConditionExpression kwarg (value detail is boto-internal).
    _, kwargs = table.query.call_args
    assert 'KeyConditionExpression' in kwargs


# ---------------------------------------------------------------------------
# _parse_kb_context
# ---------------------------------------------------------------------------

def test_parse_kb_context_json_string():
    """A JSON string with a context list parses to that list."""
    raw = json.dumps({'context': [{'content': 'table coverage ...'}]})
    assert _parse_kb_context(raw) == [{'content': 'table coverage ...'}]


def test_parse_kb_context_dict():
    """A dict is accepted directly."""
    assert _parse_kb_context({'context': [{'content': 'x'}]}) == [{'content': 'x'}]


def test_parse_kb_context_error_payload_is_empty():
    """An error payload (or non-JSON) yields an empty list."""
    assert _parse_kb_context(json.dumps({'error': 'boom'})) == []
    assert _parse_kb_context('not json') == []
    assert _parse_kb_context(None) == []


# ---------------------------------------------------------------------------
# build_advisory_answer — the no-SQL guarantee + empty-KB degrade
# ---------------------------------------------------------------------------

def test_build_advisory_answer_never_emits_sql_or_rows():
    """The structural guarantee: executed_sql == '' and results == [] always."""
    table = _metrics_table_with([
        {'metric_id': 'm1', 'name': 'Revenue', 'description': 'TTM',
         'lifecycle': 'PUBLISHED'},
    ])
    kb = lambda q: json.dumps({'context': [{'content': 'coverage table: ...'}]})
    synth = lambda prompt: "You can ask about revenue and coverage."

    result = build_advisory_answer(
        question="what can I ask?",
        layer_id="layer-1",
        kb_retrieve=kb,
        metrics_table=table,
        synthesize=synth,
        layer_name="Insurance",
    )
    assert result['executed_sql'] == ''
    assert result['results'] == []
    assert result['answer'] == "You can ask about revenue and coverage."
    assert result['metrics'][0]['metric_id'] == 'm1'
    assert result['kb_empty'] is False


def test_build_advisory_answer_passes_metrics_and_schema_into_prompt():
    """The synthesize prompt is grounded in the metrics + KB content."""
    table = _metrics_table_with([
        {'metric_id': 'rev', 'name': 'Revenue', 'description': 'TTM revenue',
         'lifecycle': 'PUBLISHED'},
    ])
    captured = {}
    def synth(prompt):
        captured['prompt'] = prompt
        return "ok"
    build_advisory_answer(
        question="what metrics exist?",
        layer_id="L",
        kb_retrieve=lambda q: json.dumps({'context': [{'content': 'COVERAGE table'}]}),
        metrics_table=table,
        synthesize=synth,
    )
    assert 'Revenue' in captured['prompt']
    assert 'COVERAGE table' in captured['prompt']
    assert 'Never write SQL' in captured['prompt']


def test_build_advisory_answer_empty_kb_degrades_to_metrics():
    """With an empty KB and a blank model answer, fall back to listing metrics."""
    table = _metrics_table_with([
        {'metric_id': 'm1', 'name': 'Revenue', 'description': 'TTM',
         'lifecycle': 'PUBLISHED'},
    ])
    result = build_advisory_answer(
        question="what's in this layer?",
        layer_id="layer-1",
        kb_retrieve=lambda q: json.dumps({'context': []}),  # empty KB
        metrics_table=table,
        synthesize=lambda prompt: "",  # model returned nothing
    )
    assert result['kb_empty'] is True
    assert 'Revenue' in result['answer']
    assert 'empty' in result['answer'].lower()


def test_build_advisory_answer_empty_kb_no_metrics():
    """Empty KB AND no metrics → an honest 'can't answer yet', never blank."""
    result = build_advisory_answer(
        question="what can I ask?",
        layer_id="layer-1",
        kb_retrieve=lambda q: json.dumps({'context': []}),
        metrics_table=_metrics_table_with([]),
        synthesize=lambda prompt: "",
    )
    assert result['kb_empty'] is True
    assert result['answer'].strip() != ""


def test_build_advisory_answer_metrics_ddb_error_is_soft():
    """A DDB failure enumerating metrics must not raise — advisory continues."""
    table = MagicMock()
    table.query.side_effect = RuntimeError("ddb down")
    result = build_advisory_answer(
        question="explain the schema",
        layer_id="layer-1",
        kb_retrieve=lambda q: json.dumps({'context': [{'content': 'x'}]}),
        metrics_table=table,
        synthesize=lambda prompt: "Here is the schema.",
    )
    assert result['metrics'] == []
    assert result['answer'] == "Here is the schema."
    assert result['executed_sql'] == ''
