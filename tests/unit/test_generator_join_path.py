"""Phase 4 generator must use the slice's declared join edges (Cluster C, live-run
follow-up).

The 2026-06-08 post-deploy smoke test of "total market value of all active
holdings, grouped by party" showed the slice DID contain holding, coverage, and
party with the correct reference-join edges
(``coverage.holding_id = holding.holding_id`` and
``coverage.party_id = party.party_id``), yet the generator invented a
``financial_activity`` join with a near-Cartesian ``OR policy_id IS NOT NULL``
predicate instead of bridging holding↔coverage↔party. The slice and judge were
fine; the GENERATION prompt never told the model to connect tables using the
slice's ``joins`` and to bridge through an intermediate table rather than
inventing a predicate. This pins that guidance into the prompt text.
"""
from __future__ import annotations

from agents.metadata_query_agent.tier2.rag_query_generator import JOIN_PATH_GUIDANCE


def test_join_path_guidance_mentions_declared_joins_and_bridging() -> None:
    g = JOIN_PATH_GUIDANCE.lower()
    # Must direct the model to the slice's declared join edges...
    assert "join" in g and "slice" in g
    # ...to bridge through an intermediate table when two tables don't join directly...
    assert "bridge" in g or "intermediate" in g or "through" in g
    # ...and to NOT invent a join predicate.
    assert "invent" in g or "do not" in g or "only" in g
