"""End-to-end VKG Tier 2: Phase 1 KNN → Phase 3 CONSTRUCT (small fixture
ontology) → Phase 4 SPARQL gen (mocked LLM) → Phase 5 grounding + execution.
Drives the real Strands graph workflow end to end.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from rdflib import RDF, RDFS, Graph, Literal, URIRef

from agents.ontology_query_agent.tier2.vkg_query_generator import (
    VkgQueryGenerator,
)
from agents.ontology_query_agent.tier2.vkg_slice_builder import VkgSliceBuilder
from agents.ontology_query_agent.tier2.vkg_topic_router import VkgTopicRouter
from agents.ontology_query_agent.tier2.workflow import (
    PhaseDeps,
    tier2_vkg_workflow,
)
from agents.shared import knn_hydration

EX = "http://ex.com/"


def _seed_ontology() -> Graph:
    """Tiny ontology — Customer/Policy + a hasPolicy property (rdfs:domain/range)
    — used as the Phase 3 CONSTRUCT result so the slice grounds the SPARQL."""
    g = Graph()
    g.add((URIRef(f"{EX}Customer"), RDF.type, RDFS.Class))
    g.add((URIRef(f"{EX}Policy"), RDF.type, RDFS.Class))
    g.add((URIRef(f"{EX}Customer"), RDFS.label, Literal("Customer")))
    g.add((URIRef(f"{EX}Policy"), RDFS.label, Literal("Policy")))
    g.add((URIRef(f"{EX}hasPolicy"), RDFS.domain, URIRef(f"{EX}Customer")))
    g.add((URIRef(f"{EX}hasPolicy"), RDFS.range, URIRef(f"{EX}Policy")))
    return g


def test_tier2_vkg_happy_path():
    """Phase 1 KNN hits → Phase 3 sufficient first round → Phase 4 SPARQL →
    Phase 5 grounds against the slice and executes."""
    # Reset the hydration cache so each test starts cold.
    knn_hydration.reset_for_tests()
    knn = MagicMock()
    knn.knn_search.return_value = [
        {"id": f"{EX}Customer", "score": 0.91, "metadata": {}},
        {"id": f"{EX}Policy", "score": 0.83, "metadata": {}},
    ]
    neptune = MagicMock()
    neptune.construct.return_value = _seed_ontology()

    judge = lambda payload: {"sufficient": True, "missing": []}  # noqa: E731

    fake_agent = MagicMock()
    fake_agent.return_value.message = {
        "content": [
            {"text": f"PREFIX ex: <{EX}> SELECT ?c WHERE "
                     "{ ?c a ex:Customer . ?c ex:hasPolicy ?p }"},
        ],
    }
    fake_agent.return_value.structured_output = None

    router = VkgTopicRouter(
        endpoint="https://x", knn=knn,
        embed_fn=lambda t: [0.0] * 1024,
        neptune_lexical=MagicMock(),
        # Stub hydration to a no-op — the test injects its own KNN hits.
        fetch_iri_metadata=lambda namespace: [],
    )
    builder = VkgSliceBuilder(
        neptune=neptune, judge_fn=judge,
        token_counter=lambda s: len(s) // 4,
        budget=12000, n_hops=2,
    )
    generator = VkgQueryGenerator(agent_factory=lambda: fake_agent)
    deps = PhaseDeps(
        router=router, builder=builder, generator=generator,
        run_execution=lambda sparql: {"columns": ["c"], "rows": [["1"]],
                                      "answer": "1 customer", "n_quads": []},
    )

    ctx = tier2_vkg_workflow(question="who has a policy?", namespace="ns-default",
                             deps=deps)
    assert ctx.candidates == [f"{EX}Customer", f"{EX}Policy"]
    assert ctx.sparql_query.startswith("PREFIX")
    assert ctx.degraded is None
    assert ctx.execution_result["rows"] == [["1"]]
