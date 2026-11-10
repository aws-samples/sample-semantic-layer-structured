"""hydrate_rules_namespace — embed-at-hydration from Neptune.

Rules live only in Neptune (design §4), so unlike the metrics path there is no
stored embedding to read off a DDB row: the caller injects a fetch callable and
this function embeds at hydration time. These tests pin the three things that
would otherwise fail silently — the PUBLISHED gate, idempotence, and the
index-time/query-time text agreement.
"""
from typing import Any, Dict, List

from agents.shared import knn_hydration


def _rule(rule_id: str = "RULE-1", **over: Any) -> Dict[str, Any]:
    """Build a fetch-shaped rule row, overriding any field via kwargs."""
    row = {
        "rule_id": rule_id,
        "name": "lapse risk after non-payment",
        "description": "a holding lapses after 90 days of non-payment",
        "synonyms": ["lapse risk", "payment lapse"],
        "lifecycle": "PUBLISHED",
    }
    row.update(over)
    return row


def _stub_index(monkeypatch, *, upserts: List[Dict[str, Any]] | None = None,
                seen_text: List[str] | None = None) -> None:
    """Stub the KNN index + embedder, optionally recording upserts and text.

    :param monkeypatch: pytest monkeypatch fixture.
    :param upserts: If given, every ``knn_index.upsert`` kwargs dict is appended.
    :param seen_text: If given, every string passed to ``embed_text`` is appended.
    """
    monkeypatch.setattr(knn_hydration.knn_index, "ensure_index", lambda **kw: None)
    monkeypatch.setattr(
        knn_hydration.knn_index, "upsert",
        lambda **kw: upserts.append(kw) if upserts is not None else None,
    )

    def _embed(text: str) -> List[float]:
        if seen_text is not None:
            seen_text.append(text)
        return [0.1] * 1024

    monkeypatch.setattr(knn_hydration, "embed_text", _embed)


def setup_function() -> None:
    """Clear hydration tracking so each test starts unhydrated."""
    knn_hydration.reset_for_tests()


def test_published_rules_are_indexed(monkeypatch) -> None:
    """A PUBLISHED rule is upserted under a namespace-qualified doc id."""
    upserts: List[Dict[str, Any]] = []
    _stub_index(monkeypatch, upserts=upserts)

    count = knn_hydration.hydrate_rules_namespace(
        namespace="ont-1", fetch_rules=lambda ns: [_rule()],
    )
    assert count == 1
    assert upserts[0]["index"] == knn_hydration.RULES_INDEX
    # The namespace field is what lookup pre-filters on server-side, so a
    # missing/incorrect value would silently leak another layer's rules.
    assert upserts[0]["doc"]["namespace"] == "ont-1"
    assert upserts[0]["doc_id"] == "ont-1:RULE-1"
    assert upserts[0]["doc"]["id"] == "RULE-1"


def test_the_embedded_text_joins_name_description_and_synonyms(monkeypatch) -> None:
    """All three matchable fields reach the embedder, not just the name."""
    seen: List[str] = []
    _stub_index(monkeypatch, seen_text=seen)

    knn_hydration.hydrate_rules_namespace(
        namespace="ont-1", fetch_rules=lambda ns: [_rule()])
    assert "lapse risk after non-payment" in seen[0]
    assert "90 days of non-payment" in seen[0]
    assert "payment lapse" in seen[0]


def test_hydration_text_is_identical_to_the_models_embedding_text(monkeypatch) -> None:
    """Index-time and query-time text must be byte-identical.

    ``rule_lookup`` embeds the user's question and compares against what this
    function indexed. If the two joins ever diverge, nothing raises — every
    score just quietly drops. Pin them together.
    """
    from agents.shared.rule_models import embedding_text_for

    seen: List[str] = []
    _stub_index(monkeypatch, seen_text=seen)

    row = _rule()
    knn_hydration.hydrate_rules_namespace(
        namespace="ont-1", fetch_rules=lambda ns: [row])
    assert seen[0] == embedding_text_for(
        name=row["name"], description=row["description"],
        synonyms=row["synonyms"],
    )


def test_unpublished_rules_are_skipped(monkeypatch) -> None:
    """A DRAFT or APPROVED rule must never be reachable from a user question.

    APPROVED is included deliberately: it is the state directly before
    PUBLISHED, so an off-by-one gate ("anything reviewed") would pass a
    DRAFT-only test and still expose rules that were never published.
    """
    upserts: List[Dict[str, Any]] = []
    _stub_index(monkeypatch, upserts=upserts)

    count = knn_hydration.hydrate_rules_namespace(
        namespace="ont-1",
        fetch_rules=lambda ns: [_rule(lifecycle="DRAFT"),
                                _rule("RULE-2", lifecycle="APPROVED")],
    )
    assert count == 0
    assert upserts == []


def test_hydration_is_idempotent(monkeypatch) -> None:
    """A second call neither re-embeds nor re-queries Neptune."""
    calls: List[str] = []
    _stub_index(monkeypatch)

    def fetch(ns: str) -> List[Dict[str, Any]]:
        calls.append(ns)
        return [_rule()]

    assert knn_hydration.hydrate_rules_namespace(
        namespace="ont-1", fetch_rules=fetch) == 1
    assert knn_hydration.hydrate_rules_namespace(
        namespace="ont-1", fetch_rules=fetch) == 0
    assert len(calls) == 1  # second call must not re-query Neptune


def test_a_namespace_with_no_published_rules_is_not_re_queried(monkeypatch) -> None:
    """Zero indexed still marks the namespace hydrated.

    Otherwise every single question in a layer that has no published rules pays
    a fresh Neptune round-trip — the worst case, since it is also the common one.
    """
    calls: List[str] = []
    _stub_index(monkeypatch)

    def fetch(ns: str) -> List[Dict[str, Any]]:
        calls.append(ns)
        return [_rule(lifecycle="DRAFT")]

    assert knn_hydration.hydrate_rules_namespace(
        namespace="ont-1", fetch_rules=fetch) == 0
    assert knn_hydration.hydrate_rules_namespace(
        namespace="ont-1", fetch_rules=fetch) == 0
    assert len(calls) == 1


def test_namespaces_are_hydrated_independently(monkeypatch) -> None:
    """Hydrating one namespace must not mark another as done."""
    _stub_index(monkeypatch)

    assert knn_hydration.hydrate_rules_namespace(
        namespace="ont-1", fetch_rules=lambda ns: [_rule()]) == 1
    assert knn_hydration.hydrate_rules_namespace(
        namespace="ont-2", fetch_rules=lambda ns: [_rule()]) == 1


def test_a_rule_with_no_text_is_skipped(monkeypatch) -> None:
    """An empty join is not embedded — it would only yield noise neighbours."""
    upserts: List[Dict[str, Any]] = []
    _stub_index(monkeypatch, upserts=upserts)

    assert knn_hydration.hydrate_rules_namespace(
        namespace="ont-1",
        fetch_rules=lambda ns: [_rule(name="", description="", synonyms=[])],
    ) == 0
    assert upserts == []


def test_reset_for_tests_clears_rule_namespaces(monkeypatch) -> None:
    """reset_for_tests must clear the rule set too, not just metrics/topics."""
    _stub_index(monkeypatch)

    knn_hydration.hydrate_rules_namespace(
        namespace="ont-1", fetch_rules=lambda ns: [_rule()])
    knn_hydration.reset_for_tests()
    assert knn_hydration.hydrate_rules_namespace(
        namespace="ont-1", fetch_rules=lambda ns: [_rule()]) == 1
