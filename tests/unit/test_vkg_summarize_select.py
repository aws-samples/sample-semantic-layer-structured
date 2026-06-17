"""Tests for the VKG agent's deterministic answer synthesis (_summarize_select).

VKG Phase 5 is LLM-free (rows never enter a prompt), so _summarize_select must
turn the SPARQL SELECT result into a useful natural-language answer instead of a
bare "Query returned N row(s) across M column(s)." — the bug where a 'how many'
question rendered "Query returned 1 row(s) across 1 column(s)." instead of the
actual count.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

from ontology_query_agent.main import _summarize_select


def test_scalar_count_states_the_value():
    """1x1 result (the dominant 'how many' case) reports the value itself."""
    out = _summarize_select(columns=['n'], rows=[['10']])
    assert out == 'The result is 10.'


def test_no_rows_is_explained():
    """An empty result set yields a clear no-results sentence, not a 0-row count."""
    out = _summarize_select(columns=['n'], rows=[])
    assert 'no results' in out.lower()


def test_single_multi_column_row_lists_fields():
    """A single record renders its column:value pairs."""
    out = _summarize_select(columns=['name', 'state'], rows=[['Acme', 'CA']])
    assert 'name: Acme' in out
    assert 'state: CA' in out


def test_multi_row_reports_count_and_points_to_table():
    """Many rows → a count + a pointer to the rendered table."""
    rows = [[f'r{i}'] for i in range(5)]
    out = _summarize_select(columns=['x'], rows=rows)
    assert '5 results' in out
    assert 'table' in out.lower()


def test_over_limit_mentions_truncation():
    """When truncated to the display cap, the answer says so."""
    rows = [[f'r{i}'] for i in range(100)]
    out = _summarize_select(columns=['x'], rows=rows, over_limit=True)
    assert 'first 100' in out


def test_not_the_legacy_generic_summary():
    """Regression guard: the answer must NOT be the old bare row/column summary."""
    out = _summarize_select(columns=['n'], rows=[['10']])
    assert 'row(s) across' not in out
