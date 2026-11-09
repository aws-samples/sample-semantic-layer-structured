"""rule_lookup — Tier 1.5 governed-rule match. Every failure falls through.

The contract under test is unusual and deliberate: this module is FAIL-SOFT
where the rule validators are fail-closed. A validator guards an approval gate,
so an error there must block; this is a query-path optimisation, so an error
here must fall through to Tier 2 and never surface as a 500. Most of the tests
below therefore assert ``is None`` on an error path — that is the feature, not a
swallowed bug.
"""
from typing import Any, Dict, List, Optional

import pytest

from agents.shared import rule_lookup


class FakeKnn:
    """Stand-in with metric_lookup's ``knn_search`` signature."""

    def __init__(self, hits: Optional[List[Dict[str, Any]]] = None,
                 raises: bool = False) -> None:
        """Record the search filter and return scripted hits.

        :param hits: Hits to return, best first (``{"id", "score"}``).
        :param raises: When True, ``knn_search`` raises instead of returning.
        """
        self._hits = hits or []
        self._raises = raises
        self.filter_terms: Optional[Dict[str, str]] = None

    def knn_search(self, *, endpoint: str, index: str, vector: List[float],
                   k: int, filter_terms: Dict[str, str]) -> List[Dict[str, Any]]:
        """Return the scripted hits, capturing the server-side filter."""
        if self._raises:
            raise RuntimeError("opensearch down")
        self.filter_terms = filter_terms
        return self._hits


def _rule_row(rule_id: str = "RULE-1", **over: Any) -> Dict[str, Any]:
    """Build a graph-shaped rule row that validates as a ``Rule``.

    Deliberately spelled to match ``rule_models.Rule`` exactly: the field is
    ``namespace`` (not ``ontology_id``), ``level`` is required with no default,
    ``Provenance.page`` is ``Optional[int]`` (so ``None``, never ``""``) and
    ``Provenance.confidence`` is required.

    :param rule_id: Identifier for the row.
    :param over: Field overrides applied on top of the valid baseline.
    :returns: A rule dict as ``fetch_rules`` would return it.
    """
    row = {
        "rule_id": rule_id, "namespace": "ont-1",
        "name": "lapse risk", "description": "d", "rule_type": "diagnostic",
        "level": "general",
        "lifecycle": "PUBLISHED",
        "scope": {"subject_class": "https://ex.org/vkg#Holding"},
        "conditions": [{"binding": "https://ex.org/vkg#days",
                        "operator": ">", "threshold_id": "THR-1"}],
        "thresholds": [{"threshold_id": "THR-1", "value": 90.0, "unit": "days",
                        "source_span": "90 days", "approved_by": "alice"}],
        "conclusion": {"outcome": "https://ex.org/vkg#LapseRisk",
                       "epistemic_status": "candidateContributorTo"},
        "missing_inputs": [], "compiled_sparql": "SELECT ?s WHERE { ?s ?p ?o }",
        "provenance": {"document_id": "d1", "source_sentence": "s",
                       "page": None, "model_id": "m", "confidence": 0.0},
    }
    row.update(over)
    return row


def _lookup(knn: Any, rows: Optional[List[Dict[str, Any]]] = None, **kw: Any):
    """Call ``rule_lookup.lookup`` with the standard fixture arguments."""
    return rule_lookup.lookup(
        question="which holdings are at lapse risk?",
        namespace="ont-1",
        knn=knn, knn_endpoint="", knn_index="rules",
        fetch_rules=lambda ns: rows if rows is not None else [_rule_row()],
        **kw,
    )


@pytest.fixture(autouse=True)
def _isolate_hydration(monkeypatch) -> None:
    """Clear hydration tracking and stub the hydration-side embedder.

    Two separate things have to be neutralised, and missing the second one is
    why this file used to pass locally but fail in CI. Each test patches
    ``rule_lookup.embed_text``, which only covers the *query* embedding.
    ``hydrate_rules_namespace`` embeds each rule through its own module-level
    ``knn_hydration.embed_text`` reference, so it still reached live Bedrock —
    fine on a workstation with credentials, an exception in a hermetic runner.
    Because the module is fail-soft, that exception surfaced as ``None``, which
    is the *expected* answer for nine of these tests; only the two asserting a
    positive outcome went red. Stub both and the file is truly offline.

    :param monkeypatch: pytest fixture used to install the stub.
    :returns: None.
    """
    from agents.shared import knn_hydration
    knn_hydration.reset_for_tests()
    monkeypatch.setattr(knn_hydration, "embed_text", lambda text: [0.1] * 1024)


def test_the_fixture_row_actually_validates() -> None:
    """Guard the fixture itself.

    Every fall-through test below passes when the row is malformed, so a broken
    baseline would make this whole file assert nothing. Validate it directly.
    """
    from agents.shared.rule_models import Rule

    rule = Rule.model_validate(_rule_row())
    assert rule.rule_id == "RULE-1"
    assert rule.is_evaluable() is True


def test_a_clear_match_returns_the_rule(monkeypatch) -> None:
    """A top hit above threshold yields the hydrated, validated Rule."""
    monkeypatch.setattr(rule_lookup, "embed_text", lambda q: [0.1] * 1024)
    rule = _lookup(FakeKnn([{"id": "RULE-1", "score": 0.93}]))
    assert rule is not None
    assert rule.rule_id == "RULE-1"


def test_the_namespace_is_pre_filtered_server_side(monkeypatch) -> None:
    """Cross-namespace hits in the top-K must not mask a real match."""
    monkeypatch.setattr(rule_lookup, "embed_text", lambda q: [0.1] * 1024)
    knn = FakeKnn([{"id": "RULE-1", "score": 0.93}])
    _lookup(knn)
    assert knn.filter_terms == {"namespace": "ont-1"}


def test_a_below_threshold_hit_falls_through(monkeypatch) -> None:
    """0.80 is under the 0.85 default, so it is a miss, not a weak answer."""
    monkeypatch.setattr(rule_lookup, "embed_text", lambda q: [0.1] * 1024)
    assert _lookup(FakeKnn([{"id": "RULE-1", "score": 0.80}])) is None


def test_no_hits_falls_through(monkeypatch) -> None:
    """An empty index is a miss, not an error."""
    monkeypatch.setattr(rule_lookup, "embed_text", lambda q: [0.1] * 1024)
    assert _lookup(FakeKnn([])) is None


def test_a_knn_error_falls_through(monkeypatch) -> None:
    """KNN being down must degrade the query, not fail it."""
    monkeypatch.setattr(rule_lookup, "embed_text", lambda q: [0.1] * 1024)
    assert _lookup(FakeKnn(raises=True)) is None


def test_an_embedding_error_falls_through(monkeypatch) -> None:
    """A throttled embedder must not surface as a 500 to the user."""
    def boom(q: str) -> List[float]:
        raise RuntimeError("bedrock throttled")

    monkeypatch.setattr(rule_lookup, "embed_text", boom)
    assert _lookup(FakeKnn([{"id": "RULE-1", "score": 0.99}])) is None


def test_a_hydration_error_falls_through(monkeypatch) -> None:
    """Neptune being unreachable falls through rather than blocking."""
    monkeypatch.setattr(rule_lookup, "embed_text", lambda q: [0.1] * 1024)

    def boom(ns: str) -> List[Dict[str, Any]]:
        raise RuntimeError("neptune down")

    assert rule_lookup.lookup(
        question="q", namespace="ont-1", knn=FakeKnn([{"id": "RULE-1", "score": 0.99}]),
        knn_endpoint="", knn_index="rules", fetch_rules=boom,
    ) is None


def test_index_drift_falls_through(monkeypatch) -> None:
    """KNN knows a rule the graph no longer returns — treat as a miss."""
    monkeypatch.setattr(rule_lookup, "embed_text", lambda q: [0.1] * 1024)
    assert _lookup(FakeKnn([{"id": "RULE-GONE", "score": 0.99}])) is None


def test_a_malformed_rule_row_falls_through(monkeypatch) -> None:
    """A row that fails model validation is a miss, not a 500.

    The row is marked PUBLISHED on purpose so it gets past the lifecycle gate
    and actually reaches ``Rule.model_validate`` — otherwise this test would
    pass for the wrong reason and leave the validation path uncovered.
    """
    monkeypatch.setattr(rule_lookup, "embed_text", lambda q: [0.1] * 1024)
    assert _lookup(FakeKnn([{"id": "RULE-1", "score": 0.99}]),
                   rows=[{"rule_id": "RULE-1", "lifecycle": "PUBLISHED"}]) is None


def test_a_non_published_rule_is_never_returned(monkeypatch) -> None:
    """An APPROVED rule unpublished after hydration must not answer.

    The index only ever holds PUBLISHED rules, so reaching this branch means the
    rule changed state between hydration and the question. Re-checking at read
    time is what keeps the review gate meaningful under that race.
    """
    monkeypatch.setattr(rule_lookup, "embed_text", lambda q: [0.1] * 1024)
    assert _lookup(FakeKnn([{"id": "RULE-1", "score": 0.99}]),
                   rows=[_rule_row(lifecycle="APPROVED")]) is None
