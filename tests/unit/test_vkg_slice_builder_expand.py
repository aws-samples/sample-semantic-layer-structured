"""VkgSliceBuilder.expand must resolve judge missing[] labels to real IRIs.

Regression guard for abstain Gap 2 (2026-07-24): the sufficiency judge names a
never-in-slice concept by a guessed LABEL, so the old ``expand`` unioned a non-IRI
and the re-construct pulled nothing → abstain persisted. The fix resolves each
missing label to a full ontology IRI over the FULL ontology before expanding.
"""

from __future__ import annotations

from rdflib import Graph

from agents.ontology_query_agent.tier2.vkg_slice_builder import VkgSliceBuilder


class _FakeNeptune:
    def __init__(self):
        self.last_candidates = None

    def construct(self, *, candidates, n_hops, namespace):
        self.last_candidates = list(candidates)
        return Graph()  # empty graph is fine; we assert on candidates, not TTL


def _builder(neptune, ont):
    return VkgSliceBuilder(
        neptune=neptune,
        judge_fn=lambda payload: {"sufficient": False, "missing": ["Customers"]},
        token_counter=lambda s: len(s),
        budget=10_000,
        n_hops=1,
        ontology_json=ont,
    )


def test_expand_resolves_label_to_full_ontology_iri():
    ont = {"classes": {"http://x/ont/customers": {"label": "customers"}},
           "properties": {}}
    nep = _FakeNeptune()
    b = _builder(nep, ont)
    b._candidates = ["http://x/ont/orders"]
    # judge named the missing concept by LABEL ("Customers"), not an IRI
    b.expand(slice_text="<ttl>", missing=["Customers"])
    assert "http://x/ont/customers" in nep.last_candidates, "label must resolve to IRI"
    assert "Customers" not in nep.last_candidates, "raw label must NOT be unioned"


def test_expand_passes_through_real_iris_and_unknowns():
    ont = {"classes": {"http://x/ont/customers": {"label": "customers"}}, "properties": {}}
    nep = _FakeNeptune()
    b = _builder(nep, ont)
    b._candidates = []
    # a real IRI stays; an unresolvable label stays unchanged (no worse than before)
    b.expand(slice_text="<ttl>", missing=["http://x/ont/orders", "Nonexistent"])
    assert "http://x/ont/orders" in nep.last_candidates
    assert "Nonexistent" in nep.last_candidates


def test_expand_resolution_now_delegates_to_shared_resolve_to_iri():
    """Wiring guard: the builder's label→IRI resolution now flows through the shared
    ``resolve_to_iri`` (Task 4 delegation). We reuse the same fake-neptune fixture and
    assert the expand still resolves a class LABEL to its full ontology IRI in the
    candidates handed to construct — proving the delegation preserved behavior.
    """
    ont = {"classes": {"http://x/ont/customers": {"label": "customers"}},
           "properties": {}}
    nep = _FakeNeptune()
    b = _builder(nep, ont)
    b._candidates = []
    b.expand(slice_text="<ttl>", missing=["Customers"])
    assert "http://x/ont/customers" in nep.last_candidates, \
        "shared resolve_to_iri must map the label to the class IRI"
