"""The RAG SQL generator prompt must steer away from SELECT * / COUNT(*) on wide
federated (DynamoDB-connector) tables, which fail with a ProjectionExpression
size error. This pins the Fix-C generation-side rule into the prompt the
generator actually sends to the model.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from agents.metadata_query_agent.tier2.rag_query_generator import RagQueryGenerator


def _capture_prompt_agent(captured: list):
    """A fake Strands agent that records the prompt and returns trivial SQL."""
    def _agent(prompt):
        captured.append(prompt)
        result = MagicMock()
        result.message = {"content": [{"text": "SELECT a FROM t LIMIT 10"}]}
        return result
    return _agent


def test_generator_prompt_forbids_select_star_on_wide_tables():
    captured: list = []
    gen = RagQueryGenerator(
        agent_factory=lambda: _capture_prompt_agent(captured),
        dialect="trino",
    )
    gen.generate(slice_text="{}", question="list parties")
    assert captured, "generator never called the agent"
    prompt = captured[0].lower()
    # The rule must mention the failure mode + the remedy.
    assert "select *" in prompt
    assert "count(*)" in prompt
    assert "projection" in prompt
    # And steer to explicit columns / COUNT(<key>).
    assert "explicit" in prompt or "count(<" in prompt
