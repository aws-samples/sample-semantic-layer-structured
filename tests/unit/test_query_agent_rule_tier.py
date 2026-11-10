"""The governed-rule tier inside the ontology query agent.

Three properties, each pinned below:

  * **Flag-gated** — with ``GOVERNED_RULES_ENABLED`` off the block makes not one
    call, so the agent behaves as it did before this feature existed.
  * **Fail-soft** — every error returns None and the turn continues into Tier 2,
    matching the Tier 1 block one line above.
  * **Evaluate-or-advise** — a matched rule with an unresolved input or an
    unapproved threshold does NOT execute; it advises and names the gap.

Ordering is also pinned (design §7): metrics beat rules, rules beat Tier 2.

The flag is read at call time, so these tests set the env var rather than
reloading ``main`` — a reload re-runs the module's OTel/boto3 initialisation and
discards monkeypatches other tests in the session rely on.
"""
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from agents.ontology_query_agent import main
from agents.shared.metric_lookup import MetricMatch


class _FakeRule:
    """Duck-typed evaluable rule; ``over`` replaces any attribute."""

    def __init__(self, **over: Any) -> None:
        """Build a PUBLISHED rule whose single condition uses THR-1."""
        self.rule_id = "RULE-004"
        self.name = "lapse risk"
        self.lifecycle = "PUBLISHED"
        self.compiled_sparql = "SELECT ?subject WHERE { ?subject a <x> }"
        self.missing_inputs: List[str] = []
        self.conditions = [SimpleNamespace(
            binding="https://ex.org/vkg#days", operator=">",
            threshold_id="THR-1", value=None)]
        self.thresholds = [_threshold()]
        self.conclusion = SimpleNamespace(
            outcome="https://ex.org/vkg#LapseRisk",
            epistemic_status="candidateContributorTo")
        self.provenance = SimpleNamespace(
            document_id="policy.pdf", page=12, source_sentence="s")
        for key, value in over.items():
            setattr(self, key, value)


def _threshold(threshold_id: str = "THR-1", approved_by: str = "alice") -> Any:
    """Build a duck-typed threshold with a bound ``is_approved``."""
    return type("T", (), {
        "threshold_id": threshold_id, "value": 90.0, "unit": "days",
        "approved_by": approved_by, "source_span": "s",
        "is_approved": lambda self: bool(approved_by)})()


def _enable(monkeypatch, *, enabled: bool = True) -> None:
    """Set the runtime flag the tier gates on."""
    monkeypatch.setenv("GOVERNED_RULES_ENABLED", "true" if enabled else "false")


def _stub_advisory(monkeypatch, captured: List[Dict[str, Any]]) -> None:
    """Replace the advisory builder, recording the knowledge_gap it received."""
    def fake(**kw: Any) -> Dict[str, Any]:
        captured.append(kw.get("knowledge_gap"))
        return {"answer": "advisory", "metrics": [], "executed_sql": "",
                "results": [], "kb_empty": False,
                "knowledge_gap": kw.get("knowledge_gap")}

    monkeypatch.setattr(main, "build_advisory_answer", fake)
    # The gap branch must not need a live gateway to render its answer.
    monkeypatch.setattr(main, "_ontology_advisory_chunks",
                        lambda **_kw: '{"context": []}')
    monkeypatch.setattr(main, "metrics_table", lambda: None)


def _answer(**kw: Any) -> Any:
    """Invoke the tier with the standard fixture arguments."""
    args: Dict[str, Any] = {"question": "q", "namespace": "ns",
                            "ontology_id": "ont-1", "id": "ont-1"}
    args.update(kw)
    return main._rule_tier_answer(**args)


def test_the_tier_is_skipped_when_the_flag_is_off(monkeypatch) -> None:
    """Flag off => not a single rule-path call. This is the regression guard."""
    _enable(monkeypatch, enabled=False)
    calls: List[str] = []
    monkeypatch.setattr(main, "rule_lookup_fn",
                        lambda **kw: calls.append("lookup"))
    assert _answer() is None
    assert calls == []


def test_an_unset_flag_is_off(monkeypatch) -> None:
    """Absent env var behaves like "false" — the feature ships dark."""
    monkeypatch.delenv("GOVERNED_RULES_ENABLED", raising=False)
    calls: List[str] = []
    monkeypatch.setattr(main, "rule_lookup_fn",
                        lambda **kw: calls.append("lookup"))
    assert _answer() is None
    assert calls == []


def test_no_rule_match_returns_none(monkeypatch) -> None:
    """No published rule matched the question — fall through, silently."""
    _enable(monkeypatch)
    monkeypatch.setattr(main, "rule_lookup_fn", lambda **kw: None)
    assert _answer() is None


def test_a_lookup_error_returns_none(monkeypatch) -> None:
    """Fail-soft: a rule-tier error must fall through to Tier 2, never 500."""
    _enable(monkeypatch)

    def boom(**kw: Any) -> Any:
        raise RuntimeError("neptune down")

    monkeypatch.setattr(main, "rule_lookup_fn", boom)
    assert _answer() is None


def test_an_evaluable_rule_produces_a_governed_rule_response(monkeypatch) -> None:
    """The executed path carries its own tier, SQL and rows."""
    _enable(monkeypatch)
    monkeypatch.setattr(main, "rule_lookup_fn", lambda **kw: _FakeRule())
    monkeypatch.setattr(main, "_cached_ontology_json", lambda **kw: {"classes": {}})
    monkeypatch.setattr(main, "rule_execute_fn",
                        lambda **kw: {"columns": ["id"], "rows": [["H-1"]],
                                      "sql": "SELECT 1", "database": "d",
                                      "catalog": ""})
    out = _answer()
    assert out["provenance"]["tier"] == "governed_rule"
    assert out["provenance"]["sources"] == ["rule:RULE-004"]
    assert out["sql_query"] == "SELECT 1"
    assert out["results"] == [["H-1"]]
    # The async worker's COMPLETED row needs every one of these keys.
    assert set(out) >= {"answer", "sql_query", "results", "n_quads",
                        "reasoning", "metadata", "provenance"}


def test_an_execution_failure_falls_through_to_tier_2(monkeypatch) -> None:
    """A broken published artefact degrades the turn; it does not fail it."""
    _enable(monkeypatch)
    monkeypatch.setattr(main, "rule_lookup_fn", lambda **kw: _FakeRule())
    monkeypatch.setattr(main, "_cached_ontology_json", lambda **kw: {})

    def boom(**kw: Any) -> Any:
        raise RuntimeError("ontop cannot reformulate")

    monkeypatch.setattr(main, "rule_execute_fn", boom)
    assert _answer() is None


def test_a_rule_with_missing_inputs_routes_to_advisory(monkeypatch) -> None:
    """The advise-rather-than-evaluate branch — no SQL is executed."""
    _enable(monkeypatch)
    rule = _FakeRule(missing_inputs=["https://ex.org/vkg#daysSinceLastPayment"])
    monkeypatch.setattr(main, "rule_lookup_fn", lambda **kw: rule)
    monkeypatch.setattr(main, "rule_execute_fn",
                        lambda **kw: pytest.fail("must not execute"))
    gaps: List[Dict[str, Any]] = []
    _stub_advisory(monkeypatch, gaps)

    out = _answer()
    assert out["provenance"]["tier"] == "advisory"
    assert out["sql_query"] == ""
    assert out["results"] == []
    assert gaps[0]["missing_inputs"] == ["https://ex.org/vkg#daysSinceLastPayment"]
    # The rule is still named as the source, so the UI can show WHICH rule stalled.
    assert out["provenance"]["sources"] == ["rule:RULE-004"]


def test_an_unapproved_threshold_routes_to_advisory(monkeypatch) -> None:
    """An unapproved bound the rule APPLIES stalls it, and the gap says so."""
    _enable(monkeypatch)
    monkeypatch.setattr(
        main, "rule_lookup_fn",
        lambda **kw: _FakeRule(thresholds=[_threshold(approved_by="")]))
    monkeypatch.setattr(main, "rule_execute_fn",
                        lambda **kw: pytest.fail("must not execute"))
    captured: List[Dict[str, Any]] = []
    _stub_advisory(monkeypatch, captured)

    _answer()
    assert captured[0]["unapproved_thresholds"] == ["THR-1"]


def test_the_advise_branch_agrees_with_the_executor_gate(monkeypatch) -> None:
    """An unapplied unapproved bound must not stall an otherwise-runnable rule.

    The branch here and ``rule_executor``'s refusal must be the same predicate.
    If this branch were stricter, the rule would advise forever; if it were
    laxer, the executor would refuse and the rule would fall to Tier 2 forever.
    Both bugs are invisible in production, so the agreement is pinned here.
    """
    _enable(monkeypatch)
    monkeypatch.setattr(
        main, "rule_lookup_fn",
        lambda **kw: _FakeRule(thresholds=[
            _threshold(), _threshold(threshold_id="THR-9", approved_by="")]))
    monkeypatch.setattr(main, "_cached_ontology_json", lambda **kw: {})
    monkeypatch.setattr(main, "rule_execute_fn",
                        lambda **kw: {"columns": [], "rows": [], "sql": "SELECT 1",
                                      "database": "d", "catalog": ""})
    out = _answer()
    assert out["provenance"]["tier"] == "governed_rule"


def test_an_advisory_failure_falls_through(monkeypatch) -> None:
    """Even the gap path is fail-soft; Tier 2 may still answer the question."""
    _enable(monkeypatch)
    monkeypatch.setattr(main, "rule_lookup_fn",
                        lambda **kw: _FakeRule(missing_inputs=["x"]))

    def boom(**kw: Any) -> Any:
        raise RuntimeError("bedrock throttled")

    monkeypatch.setattr(main, "build_advisory_answer", boom)
    monkeypatch.setattr(main, "metrics_table", lambda: None)
    assert _answer() is None


# --- Ordering: the call site, not just the function ------------------------


def _stub_metadata(monkeypatch) -> None:
    """Stub the layer-metadata read so _run_query reaches the tier cascade."""
    monkeypatch.setattr(main, "get_latest_metadata_item",
                        lambda _id: {"name": "demo", "namespace": "ns-default"})


def test_the_rule_tier_runs_after_a_tier_1_miss_and_before_tier_2(
        monkeypatch) -> None:
    """A rule answer short-circuits the Tier 2 workflow entirely."""
    _enable(monkeypatch)
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "tier1_lookup", lambda **_: MetricMatch())
    payload = {"answer": "a", "sql_query": "SELECT 1", "results": [],
               "n_quads": [], "reasoning": {}, "metadata": {},
               "provenance": {"tier": "governed_rule", "sources": []}}
    monkeypatch.setattr(main, "_rule_tier_answer", lambda **_kw: payload)
    spy_tier2 = MagicMock()
    monkeypatch.setattr(main, "tier2_resolve", spy_tier2)

    out = main._run_query({"question": "which holdings lapse?", "id": "ont-1"})

    spy_tier2.assert_not_called()
    assert out["provenance"]["tier"] == "governed_rule"


def test_a_tier_1_metric_hit_wins_over_a_rule(monkeypatch) -> None:
    """Design §7: a governed metric is more specific and cheaper than a rule."""
    _enable(monkeypatch)
    _stub_metadata(monkeypatch)
    fake_metric = MagicMock(metric_id="monthly_revenue", supported_filters=[],
                            supported_dimensions=[], compiled_sql="SELECT 1",
                            dialect="athena")
    monkeypatch.setattr(
        main, "tier1_lookup",
        lambda **_: MetricMatch(matched=fake_metric, matched_score=0.91),
    )
    monkeypatch.setattr(main, "tier1_execute",
                        lambda **_: {"columns": ["x"], "rows": [["1"]],
                                     "metric_id": "monthly_revenue"})
    calls: List[str] = []
    monkeypatch.setattr(main, "_rule_tier_answer",
                        lambda **_kw: calls.append("rule"))
    monkeypatch.setattr(main, "tier2_resolve", MagicMock())

    out = main._run_query({"question": "monthly revenue", "id": "ont-1"})

    assert calls == []
    assert out["provenance"]["tier"] == "governed_metric"
