"""Level 2 — semantic validation of bindings against the ontology."""
from typing import Dict, List

from agents.shared.rule_validator import validate_semantic


class FakeSparql:
    """Records ASK queries and replays canned booleans.

    Mirrors how tests/unit/test_vkg_slice_builder.py fakes the Neptune client:
    the validator takes an injected callable, so no AWS is needed.
    """

    def __init__(self, answers: Dict[str, bool]) -> None:
        self.answers = answers
        self.queries: List[str] = []

    def __call__(self, *, query: str, graph_uri: str) -> bool:
        self.queries.append(query)
        for needle, verdict in self.answers.items():
            if needle in query:
                return verdict
        return False


def test_binding_whose_domain_matches_the_scope_class_passes() -> None:
    # Both needles are required: the binding domain and the outcome-class
    # declaration are separate ASKs, because a violation has to name WHICH
    # binding failed (see the two tests below).
    ask = FakeSparql({"days": True, "LapseRisk": True})
    report = validate_semantic(
        scope_class="https://ex.org/vkg#Holding",
        bindings=["https://ex.org/vkg#days"],
        outcome_class="https://ex.org/vkg#LapseRisk",
        ask=ask,
        ontology_graph_uri="https://g/ont-1",
    )
    assert report.passed is True
    assert report.level == "semantic"


def test_binding_from_an_unrelated_class_fails_and_names_the_binding() -> None:
    report = validate_semantic(
        scope_class="https://ex.org/vkg#Holding",
        bindings=["https://ex.org/vkg#premium"],
        outcome_class="https://ex.org/vkg#LapseRisk",
        ask=FakeSparql({}),
        ontology_graph_uri="https://g/ont-1",
    )
    assert report.passed is False
    assert any("premium" in v for v in report.violations)


def test_it_queries_the_ontology_graph_not_the_rules_graph() -> None:
    ask = FakeSparql({"days": True})
    validate_semantic(
        scope_class="https://ex.org/vkg#Holding",
        bindings=["https://ex.org/vkg#days"],
        outcome_class="https://ex.org/vkg#LapseRisk",
        ask=ask,
        ontology_graph_uri="https://g/ont-1",
    )
    assert all("/rules" not in q for q in ask.queries)


def test_an_unknown_outcome_class_fails() -> None:
    """The conclusion must name a class the ontology actually declares."""
    report = validate_semantic(
        scope_class="https://ex.org/vkg#Holding",
        bindings=[],
        outcome_class="https://ex.org/vkg#Invented",
        ask=FakeSparql({}),
        ontology_graph_uri="https://g/ont-1",
    )
    assert report.passed is False
    assert any("Invented" in v for v in report.violations)


def test_a_sparql_error_fails_closed() -> None:
    def boom(*, query: str, graph_uri: str) -> bool:
        raise RuntimeError("neptune down")

    report = validate_semantic(
        scope_class="https://ex.org/vkg#Holding",
        bindings=["https://ex.org/vkg#days"],
        outcome_class="https://ex.org/vkg#LapseRisk",
        ask=boom,
        ontology_graph_uri="https://g/ont-1",
    )
    assert report.passed is False
    assert any("neptune down" in v for v in report.violations)
