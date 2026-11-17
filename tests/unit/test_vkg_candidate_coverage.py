"""Phase-1 VKG candidate coverage — every class must anchor the slice.

Regression guard for the abstain root cause (2026-07-24): the lexical class cap
dropped classes a question referenced only by VALUE ("SME", "CZK-paying"), so the
slice lacked them and the sufficiency judge abstained on concepts that exist. The
fix anchors the slice on ALL classes regardless of lexical overlap.
"""

from __future__ import annotations

from agents.ontology_query_agent.main import _GatewayTopicRouter


def _ont():
    # 13 classes (mimics formula_1 / cwd = the DBs where the 12-cap alone breaks)
    # plus a value-referenced class the question never names by its class label.
    classes = {f"http://x/ont/C{i}": {"label": f"C{i}"} for i in range(12)}
    classes["http://x/ont/customers"] = {"label": "customers"}
    props = {"http://x/ont/customers/Segment": {"label": "Segment"}}
    return {"classes": classes, "properties": props}


def test_all_classes_are_candidates_even_without_lexical_hit():
    r = _GatewayTopicRouter(ontology_json=_ont())
    # The question references the customers class only via a VALUE ("SME") and a
    # column phrased differently ("consumption") — never the class name.
    iris = r.find_candidates(
        question="average monthly consumption for SME in 2013", namespace="x"
    )
    assert "http://x/ont/customers" in iris, "value-referenced class must still anchor"
    class_iris = {i for i in iris if "/ont/C" in i or i.endswith("/customers")}
    assert len(class_iris) == 13, f"all 13 classes must be present, got {len(class_iris)}"


def test_lexically_matching_class_ranks_first():
    # When a class DOES lexically match, it should sort ahead of non-matching ones.
    r = _GatewayTopicRouter(ontology_json=_ont())
    iris = r.find_candidates(question="list all customers", namespace="x")
    assert iris[0] == "http://x/ont/customers"


def test_semantic_property_candidate_included(monkeypatch):
    # A property referenced only SEMANTICALLY (question phrasing != property local
    # name) must surface when retrieve_candidates ranks it in. Patch the symbol as
    # imported INTO main so find_candidates unions its result into the property IRIs.
    from agents.ontology_query_agent import main as m

    monkeypatch.setattr(
        m,
        "retrieve_candidates",
        lambda **k: list(k["lexical"]) + ["http://x/ont/customers/Segment"],
    )
    r = m._GatewayTopicRouter(ontology_json=_ont())
    # "SME" is a Segment VALUE — the property local-name "Segment" never appears.
    iris = r.find_candidates(question="customers in SME", namespace="x")
    assert "http://x/ont/customers/Segment" in iris


def test_semantic_property_retrieval_is_scoped_to_properties(monkeypatch):
    # find_candidates must hand retrieve_candidates a PROPERTIES-ONLY ontology view
    # (classes are already fully anchored, so letting them compete for the k novel
    # slots would waste the budget). Capture the ontology_json it passes.
    from agents.ontology_query_agent import main as m

    seen = {}

    def _capture(**kwargs):
        seen["ontology_json"] = kwargs["ontology_json"]
        # Rank a CLASS IRI in as "novel" — it must NOT end up a property candidate.
        return list(kwargs["lexical"]) + ["http://x/ont/customers"]

    monkeypatch.setattr(m, "retrieve_candidates", _capture)
    r = m._GatewayTopicRouter(ontology_json=_ont())
    iris = r.find_candidates(question="customers in SME", namespace="x")

    # Properties-only view: no classes offered to the semantic ranker.
    assert seen["ontology_json"]["classes"] == {}
    assert "http://x/ont/customers/Segment" in seen["ontology_json"]["properties"]
    # The novel CLASS IRI is present ONLY as the anchor, never duplicated as a prop.
    assert iris.count("http://x/ont/customers") == 1
