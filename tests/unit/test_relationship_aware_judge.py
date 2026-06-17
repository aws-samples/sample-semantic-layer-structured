"""Relationship-aware slice sufficiency (Cluster C3).

The Phase 3 judge must reason about whether the slice can CONNECT the entities a
question relates, not just whether the tables are present:
  * When two related entities have no join path in the slice but a bridge table
    would connect them, the judge requests the bridge in ``missing[]`` — which the
    builder's ``expand`` then adds (composing with the C2 bridge discovery).
  * When NO path can exist (e.g. the curated ``relation`` table is party-to-party
    only, so "insured party that is also the policyholder of a holding" is
    unexpressible), the judge rejects rather than passing a slice the generator
    will then try to satisfy by hallucinating columns (Q1's grounding dead-end).

The judge itself is an LLM, so these tests pin (a) the prompt carries the
relationship-path instruction, and (b) the builder faithfully expands when the
judge returns a bridge table in ``missing``.
"""
from __future__ import annotations

import json
import textwrap

from agents.metadata_query_agent.query_prompts import JUDGE_PROMPT
from agents.metadata_query_agent.tier2.rag_slice_builder import RagSliceBuilder


def test_judge_prompt_has_relationship_path_instruction() -> None:
    p = JUDGE_PROMPT.lower()
    # Must instruct the judge to check a connecting join PATH between related
    # entities and to reject when none can exist.
    assert "join path" in p or "connect" in p
    assert "bridge" in p or "junction" in p or "intermediate" in p


def test_judge_prompt_requires_columns_array_evidence() -> None:
    # Change 3: the judge must judge column availability from the `columns` array
    # only, and must not be satisfied by a dotted name in `tables`.
    p = JUDGE_PROMPT.lower()
    assert "columns" in p and "tables" in p
    assert "dotted" in p or "table.column" in p


def _md(table: str, body: str) -> str:
    return textwrap.dedent(f"# normalized.{table}\n\n{body}").strip()


def test_expand_adds_judge_requested_bridge_table() -> None:
    # First judge call: insufficient, asks for the coverage bridge. Second call:
    # sufficient. The builder must fetch + include coverage on expand.
    chunks = {
        "normalized.holding": _md("holding", textwrap.dedent("""
            ## Columns
            | Column | Type | Description |
            |--------|------|-------------|
            | holding_id | varchar | PK |
            | market_value | double | value |
        """)),
        "normalized.party": _md("party", textwrap.dedent("""
            ## Columns
            | Column | Type | Description |
            |--------|------|-------------|
            | party_id | varchar | PK |
        """)),
        "normalized.coverage": _md("coverage", textwrap.dedent("""
            ## Reference Tables
            - `normalized.holding`: JOIN normalized.holding h ON c.holding_id = h.holding_id
            - `normalized.party`: JOIN normalized.party p ON c.party_id = p.party_id

            ## Columns
            | Column | Type | Description |
            |--------|------|-------------|
            | coverage_id | varchar | PK |
            | holding_id | varchar | FK |
            | party_id | varchar | FK |
        """)),
    }

    calls = {"n": 0}

    def judge(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"sufficient": False, "missing": ["normalized.coverage"]}
        return {"sufficient": True, "missing": []}

    b = RagSliceBuilder(
        chunks_lookup=lambda *, table_ids, namespace: {
            t: chunks[t] for t in table_ids if t in chunks},
        judge_fn=judge,
        token_counter=lambda s: len(s) // 4, budget=100000,
    )
    # Build WITHOUT the bridge first (disable auto-bridge by giving endpoints that
    # don't name coverage in their own docs — holding/party have no Reference
    # Tables here, so bridge discovery finds nothing and the judge must drive it).
    slice_text = b.build(
        candidates=["normalized.holding", "normalized.party"], namespace="ns")
    assert "normalized.coverage" not in json.loads(slice_text)["tables"]
    # Judge asks for coverage → expand pulls it in.
    expanded = b.expand(slice_text=slice_text, missing=["normalized.coverage"])
    assert "normalized.coverage" in json.loads(expanded)["tables"]
