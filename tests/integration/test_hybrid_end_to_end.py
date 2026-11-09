"""End-to-end Hybrid graph integration test — REAL bridge + grounding + graph.

Drives the full 6-phase Hybrid workflow (:func:`run_hybrid_workflow`) end to end
with only the three external seams stubbed:

  * ``generator`` — returns canned SQL (no Bedrock call),
  * ``fetch``     — parses and returns the on-disk thin-VKG fixture slice
                    (``tests/fixtures/hybrid_layer/thin_vkg.ttl``) instead of a
                    real Neptune round-trip,
  * ``run_execution`` — returns a canned result dict (no Athena call).

Everything else runs for real: the SQL→ontology bridge
(:mod:`agents.semantic_layer_query_agent.sql_ontology_bridge`), the inverse
Ontop mapping index (:mod:`agents.semantic_layer_query_agent.ontop_mappings`),
the ontology-verified grounding gate
(:mod:`agents.semantic_layer_query_agent.grounding`), and the Strands graph
routing (Phase 5 back-edge / abstain terminal) in ``graph.py``.

The three scenarios exercise a REAL qualified-grounding violation (not a faked
miss): the fixture predicate ``hasStateTc`` has ``rdfs:domain`` ``Address`` but
is mapped to a column on BOTH ``address`` and ``party``. SQL reading
``party.state_tc`` therefore bridges to ``(Party, hasStateTc)`` — a
``property:… on <Party>`` miss — while ``address.state_tc`` grounds cleanly.

Test-layout choice
------------------
``pyproject.toml`` sets ``testpaths = ["tests/unit"]`` and CI runs
``pytest tests/unit/ …`` explicitly (see ``.gitlab-ci.yml``). This file lives
under ``tests/integration/`` and is therefore NOT collected by the bare/CI
runs; it is run on demand by explicit path:

    python -m pytest tests/integration/test_hybrid_end_to_end.py -v

``pythonpath = ["."]`` (already set) makes ``import agents…`` resolve. We
deliberately leave ``testpaths`` unchanged so CI timing/scope is untouched.
"""
from __future__ import annotations

import pathlib
from typing import Dict, List

from rdflib import Graph

from agents.semantic_layer_query_agent.graph import (
    HybridDeps,
    run_hybrid_workflow,
)

# The standalone thin-VKG slice this test grounds against. Loaded once and reused
# by the fetch stub in each scenario.
_FIXTURE_TTL = (
    pathlib.Path(__file__).resolve().parents[1]
    / "fixtures"
    / "hybrid_layer"
    / "thin_vkg.ttl"
)

# The vocab prefix must match the `mapsTo*` predicate IRIs in the fixture so the
# inverse index picks up the mapping triples.
_VOCAB = "https://x#"


class _Router:
    """Phase 1 recall router stub — returns a single candidate table id."""

    def find_candidates(self, *, question: str, namespace: str) -> List[str]:
        """Return a fixed ``database.table`` candidate for the question.

        Args:
            question: The natural-language question (unused — fixed candidate).
            namespace: The semantic-layer namespace (unused).

        Returns:
            A one-element list with a ``database.table`` candidate id.
        """
        return ["db.address"]


class _Builder:
    """Phase 3 slice builder stub — always sufficient on the first round."""

    def build(self, *, candidates: List[str], namespace: str,
              question: str) -> str:
        """Return an empty JSON slice (the grounding gate uses the graph slice).

        Args:
            candidates: Phase 1 candidate ids (unused).
            namespace: The semantic-layer namespace (unused).
            question: The natural-language question (unused).

        Returns:
            A minimal JSON slice string.
        """
        return "{}"

    def is_sufficient(self, *, slice_text: str, question: str):
        """Report the slice sufficient so Phase 3 exits on the first round.

        Args:
            slice_text: The current slice (unused).
            question: The natural-language question (unused).

        Returns:
            ``(True, None)`` — sufficient, no missing tables.
        """
        return True, None

    def expand(self, *, slice_text: str, missing: List[str]) -> str:
        """Return the slice unchanged (never called — build is sufficient).

        Args:
            slice_text: The current slice.
            missing: The judge's missing-table list (unused).

        Returns:
            The slice text unchanged.
        """
        return slice_text


def _deps(gen_seq: List[str]) -> HybridDeps:
    """Build :class:`HybridDeps` whose generator emits ``gen_seq`` in order.

    The generator returns the SQL at the current call index (clamping at the last
    element once exhausted), so a one-element ``gen_seq`` re-emits the same SQL on
    every regenerate back-edge — the abstain scenario.

    Args:
        gen_seq: SQL statements to return on successive ``generate`` calls.

    Returns:
        A fully-wired :class:`HybridDeps` with real vocab/dialect and stubbed
        generator / fetch / run_execution seams.
    """
    calls = {"i": 0}

    class _Gen:
        """Phase 4 SQL generator stub — replays ``gen_seq`` positionally."""

        def generate(self, *, slice_text: str, question: str,
                     negative_constraint: str = "") -> str:
            """Return the next canned SQL statement.

            Args:
                slice_text: The Phase 3 slice (unused).
                question: The natural-language question (unused).
                negative_constraint: The grounding gate's negative constraint
                    on a regenerate back-edge (unused — sequence is positional).

            Returns:
                The SQL for the current call index (clamped at the last element).
            """
            sql = gen_seq[min(calls["i"], len(gen_seq) - 1)]
            calls["i"] += 1
            return sql

        # Mirrors the real generator's usage attribute the Phase 4 node reads.
        last_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}

    class _Fetch:
        """Phase 5 structural-graph fetcher stub — parses the fixture slice."""

        def get(self, *, namespace: str, candidates: List[str]) -> Graph:
            """Parse and return the on-disk thin-VKG fixture as an rdflib Graph.

            Args:
                namespace: The semantic-layer namespace (unused).
                candidates: Phase 1 candidate ids (unused).

            Returns:
                The parsed fixture slice graph.
            """
            g = Graph()
            g.parse(source=str(_FIXTURE_TTL), format="turtle")
            return g

    def _run_execution(ctx) -> Dict[str, object]:
        """Return a canned execution result for a grounded SQL statement.

        Args:
            ctx: The workflow context (unused — canned result).

        Returns:
            A result dict with a stubbed answer and empty usage.
        """
        return {"answer": "ok", "rows": [["1"]], "usage": {}}

    return HybridDeps(
        router=_Router(),
        builder=_Builder(),
        generator=_Gen(),
        fetch=_Fetch(),
        run_execution=_run_execution,
        vocab=_VOCAB,
        dialect="athena",
    )


def test_hybrid_e2e_valid_grounds():
    """Scenario 1 — valid SQL grounds on the first pass, no regenerate.

    ``address.state_tc`` bridges to ``(Address, hasStateTc)``; the predicate's
    ``rdfs:domain`` Address admits the subject class, so the grounding gate finds
    NO miss and the graph runs execution and terminates cleanly.
    """
    deps = _deps(["SELECT a.state_tc FROM address a"])
    ctx = run_hybrid_workflow(question="what state?", namespace="ns", deps=deps)

    assert ctx.degraded is None
    assert ctx.grounding_rounds == 0
    assert ctx.grounding_missing == []
    assert ctx.execution_result.get("answer") == "ok"


def test_hybrid_e2e_regenerate_then_ground():
    """Scenario 2 — a real wrong-class miss forces a regenerate, then grounds.

    The first SQL reads ``party.state_tc`` -> ``(Party, hasStateTc)``. The
    predicate is domained to Address only, so Party is not admitted: a genuine
    ``property:<hasStateTc> on <Party>`` miss. The Phase 5 → Phase 4 back-edge
    regenerates, the second SQL reads ``address.state_tc`` and grounds.
    """
    deps = _deps([
        "SELECT p.state_tc FROM party p",
        "SELECT a.state_tc FROM address a",
    ])
    ctx = run_hybrid_workflow(question="what state?", namespace="ns", deps=deps)

    assert ctx.grounding_rounds >= 1
    assert ctx.degraded is None
    assert ctx.execution_result.get("answer") == "ok"


def test_hybrid_e2e_abstain_unresolved():
    """Scenario 3 — a persistent wrong-class miss abstains at the ceiling.

    The generator always emits the ``party.state_tc`` SQL, so the wrong-class
    miss fires every round. Once the grounding-round ceiling is exceeded the
    graph abstains via the degraded terminal rather than executing un-grounded
    SQL — never a silent single-engine answer.
    """
    deps = _deps(["SELECT p.state_tc FROM party p"])
    ctx = run_hybrid_workflow(question="what state?", namespace="ns", deps=deps)

    assert ctx.degraded == "grounding_unresolved"
    assert ctx.execution_result == {}
