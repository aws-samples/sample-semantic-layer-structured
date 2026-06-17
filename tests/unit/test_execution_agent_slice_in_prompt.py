"""Unit tests for the RAG Phase-5 execution agent prompt assembly.

The deployed metadata-query agent runs a deterministic Tier 2 graph, so the
only LLM tool span carrying schema-relevant content is ``execute_sql_query``.
The SESSION-level ``SqlGrounded`` judge reads that span's input prompt to verify
every table/column in the SQL appears in the retrieved schema. These tests pin
that the execution prompt carries the slice when supplied (so the judge has
something to ground against) and stays unchanged when it is not.
"""
from agents.metadata_query_agent.tier2.execution_agent import run_execution


class _SpyAgent:
    """Captures the prompt it was called with and returns a canned result."""

    def __init__(self) -> None:
        self.prompt: str = ""

    def __call__(self, prompt: str):
        self.prompt = prompt
        return type(
            "Result",
            (),
            {"message": {"content": [{"text": "1 row"}]}},
        )()


def test_slice_text_is_embedded_in_execution_prompt():
    """When a slice is passed, the prompt carries a read-only schema block."""
    agent = _SpyAgent()
    slice_text = '{"tables": ["normalized.party"], "columns": [{"name": "party_id"}]}'
    out = run_execution(
        agent=agent, sql="SELECT party_id FROM normalized.party LIMIT 10",
        database_name="normalized", catalog_id="cat", slice_text=slice_text,
    )
    # The full slice is present, fenced in a clearly-labelled read-only block
    # that the SqlGrounded judge can locate alongside the SQL.
    assert "[retrieved_schema_context]" in agent.prompt
    assert slice_text in agent.prompt
    assert "do NOT use it to" in agent.prompt
    # The SQL and connection params still follow, unchanged.
    assert "[sql]" in agent.prompt
    assert "database_name=normalized" in agent.prompt
    assert out["answer"] == "1 row"


def test_no_slice_text_leaves_prompt_block_absent():
    """Backward-compatible: omitting the slice emits no schema block."""
    agent = _SpyAgent()
    run_execution(
        agent=agent, sql="SELECT 1", database_name="db", catalog_id="cat",
    )
    assert "[retrieved_schema_context]" not in agent.prompt
    assert agent.prompt.startswith("Execute this query and report the result.")
