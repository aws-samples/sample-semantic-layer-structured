"""End-to-end RAG Tier 2: Phase 1 structured KB → Phase 2 markdown-driven
slice → Phase 3 NL→SQL with sqlglot validation.

The Phase 2 slice builder consumes the KB chunk markdown bodies returned by
``retrieve_kb_context_structured`` — no Glue lookups, no Neptune.
"""
from __future__ import annotations

import textwrap
from unittest.mock import MagicMock

from agents.metadata_query_agent.tier2.rag_query_generator import (
    RagQueryGenerator,
)
from agents.metadata_query_agent.tier2.rag_slice_builder import RagSliceBuilder
from agents.metadata_query_agent.tier2.rag_topic_router import RagTopicRouter
from agents.metadata_query_agent.tier2.workflow import (
    PhaseDeps,
    tier2_rag_workflow,
)


def _md_customers() -> str:
    """Markdown KB doc for db.customers — has the join to db.policy."""
    return textwrap.dedent("""
    # AWSDataCatalog.db.customers

    ## Overview
    A row is one customer.

    ## Reference Tables
    - `db.policy`: JOIN db.policy p ON c.customer_id = p.customer_id

    ## Columns
    | Column | Type | Description |
    |--------|------|-------------|
    | customer_id | varchar | Primary key. |
    """).strip()


def _md_policy() -> str:
    """Markdown KB doc for db.policy."""
    return textwrap.dedent("""
    # AWSDataCatalog.db.policy

    ## Columns
    | Column | Type | Description |
    |--------|------|-------------|
    | policy_id | varchar | Primary key. |
    | customer_id | varchar | FK to customer(customer_id). |
    """).strip()


def test_tier2_rag_happy_path():
    """Phase 1 returns 2 tables with chunk bodies → Phase 2 sufficient → SQL JOIN."""
    retrieve = MagicMock(return_value={
        "candidates": [
            {"table_id": "db.customers", "score": 0.91},
            {"table_id": "db.policy", "score": 0.85},
        ],
        "chunks": [_md_customers(), _md_policy()],
        "chunks_by_table": {
            "db.customers": _md_customers(),
            "db.policy": _md_policy(),
        },
    })

    fake_agent = MagicMock()
    fake_agent.return_value.message = {
        "content": [
            {"text":
                "SELECT c.customer_id FROM db.customers c "
                "JOIN db.policy p ON p.customer_id = c.customer_id"},
        ],
    }
    fake_agent.return_value.structured_output = None

    router = RagTopicRouter(
        retrieve_fn=retrieve, kb_id_for=lambda ns: "kb-1",
    )
    judge = lambda payload: {"sufficient": True, "missing": []}  # noqa: E731
    builder = RagSliceBuilder(
        chunks_lookup=router.chunks_for,
        judge_fn=judge,
        token_counter=lambda s: len(s) // 4, budget=12000,
    )
    generator = RagQueryGenerator(
        agent_factory=lambda: fake_agent, dialect="athena",
    )
    deps = PhaseDeps(
        router=router, builder=builder, generator=generator,
        run_execution=lambda sql, db, cat, **_kw: {"columns": ["customer_id"],
                                            "rows": [["1"]], "answer": "1 row"},
    )

    ctx = tier2_rag_workflow(
        question="list parties holding a coverage", namespace="ns",
        kb_id="kb-1", deps=deps,
    )
    assert ctx.candidates == ["db.customers", "db.policy"]
    assert "JOIN" in ctx.sql
    assert ctx.degraded is None
    assert ctx.execution_result["rows"] == [["1"]]
