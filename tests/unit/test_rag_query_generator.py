"""Tests for the RAG NL→SQL query generator (with one repair round)."""
from unittest.mock import MagicMock

from agents.metadata_query_agent.tier2.rag_query_generator import RagQueryGenerator


def test_generate_returns_sql_first_try():
    fake_agent = MagicMock()
    r = MagicMock()
    r.message = {'content': [{'text': 'SELECT 1'}]}
    r.structured_output = None
    fake_agent.return_value = r
    g = RagQueryGenerator(agent_factory=lambda: fake_agent, dialect="athena")
    out = g.generate(slice_text='{"tables": []}', question="q")
    assert out == "SELECT 1"


def test_generate_repairs_on_syntax_error():
    fake_agent = MagicMock()
    bad = MagicMock()
    bad.message = {'content': [{'text': 'SELECT FROM WHERE GROUP BY'}]}
    good = MagicMock()
    good.message = {'content': [{'text': 'SELECT 1'}]}
    fake_agent.side_effect = [bad, good]
    g = RagQueryGenerator(agent_factory=lambda: fake_agent, dialect="athena")
    assert g.generate(slice_text="x", question="q") == "SELECT 1"
    assert fake_agent.call_count == 2
