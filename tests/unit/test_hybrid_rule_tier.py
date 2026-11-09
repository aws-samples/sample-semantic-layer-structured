"""The governed-rule tier inside the HYBRID query agent.

Same three properties the VKG agent's tier is pinned on
(``test_query_agent_rule_tier.py``), re-pinned here because the hybrid agent is
copy-and-own: it has its own copies of the rule modules and its own gateway
client, so nothing about the VKG tier's behaviour is inherited.

  * **Flag-gated** — with ``GOVERNED_RULES_ENABLED`` off the block makes not one
    call, so a runtime that has not opted in behaves exactly as before.
  * **Fail-soft** — every error returns None and the turn continues into Tier 2
    (the artifact-guarded hybrid graph), matching the Tier 1 block above it.
  * **Evaluate-or-advise** — a matched rule with an unresolved input or an
    unapproved threshold does NOT execute; it advises and names the gap.

Ordering is pinned too: metrics beat rules, rules beat Tier 2.

The flag is read at call time, so these tests set the env var rather than
reloading ``main`` — a reload re-runs the module's boto3 initialisation and
discards monkeypatches other tests in the session rely on.
"""
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from agents.semantic_layer_query_agent import main


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


def _stub_advisory(monkeypatch, captured: List[Any]) -> None:
    """Replace the advisory builder, recording the knowledge_gap it received."""
    def fake(**kw: Any) -> Dict[str, Any]:
        captured.append(kw.get("knowledge_gap"))
        return {"answer": "advisory", "metrics": [], "executed_sql": "",
                "results": [], "kb_empty": False,
                "knowledge_gap": kw.get("knowledge_gap")}

    monkeypatch.setattr(main, "build_advisory_answer", fake)
    # The gap branch must not need a live KB or DDB to render its answer.
    monkeypatch.setattr(main, "_advisory_kb_retrieve", lambda **_kw: (lambda _q: ""))
    monkeypatch.setattr(main, "metrics_table", lambda: None)


def _answer(**kw: Any) -> Any:
    """Invoke the tier with the standard fixture arguments."""
    args: Dict[str, Any] = {"question": "q", "namespace": "ns",
                            "ontology_id": "hybrid-1", "id": "hybrid-1",
                            "config": {}}
    args.update(kw)
    return main._rule_tier_answer(**args)


# --- Flag gating ------------------------------------------------------------


def test_the_tier_is_skipped_when_the_flag_is_off(monkeypatch) -> None:
    """Flag off => not a single rule-path call. This is the regression guard."""
    _enable(monkeypatch, enabled=False)
    calls: List[str] = []
    monkeypatch.setattr(main, "rule_lookup_fn", lambda **kw: calls.append("lookup"))
    assert _answer() is None
    assert calls == []


def test_an_unset_flag_is_off(monkeypatch) -> None:
    """Absent env var behaves like "false" — the feature ships dark.

    The hybrid runtime is deployed to environments where the REST API's
    ``RULES_ENABLED`` may be on while this one is not, so "unset" must be a
    no-call, not a no-op-after-calling.
    """
    monkeypatch.delenv("GOVERNED_RULES_ENABLED", raising=False)
    calls: List[str] = []
    monkeypatch.setattr(main, "rule_lookup_fn", lambda **kw: calls.append("lookup"))
    assert _answer() is None
    assert calls == []


# --- Fail-soft --------------------------------------------------------------


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


def test_an_execution_failure_falls_through_to_tier_2(monkeypatch) -> None:
    """A broken published artefact degrades the turn; it does not fail it."""
    _enable(monkeypatch)
    monkeypatch.setattr(main, "rule_lookup_fn", lambda **kw: _FakeRule())
    monkeypatch.setattr(main, "_cached_ontology_json", lambda **kw: {})

    def boom(**kw: Any) -> Any:
        raise RuntimeError("ontop cannot reformulate")

    monkeypatch.setattr(main, "rule_execute_fn", boom)
    assert _answer() is None


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


# --- Evaluate-or-advise -----------------------------------------------------


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
    # The chat/async response contract needs every one of these keys.
    assert set(out) >= {"answer", "sql_query", "results", "n_quads",
                        "reasoning", "metadata", "provenance"}


def test_the_executor_is_given_the_layers_own_catalog_and_database(
    monkeypatch,
) -> None:
    """THE 2026-08-18 regression, at the wiring seam.

    A hybrid layer's thin VKG stores ``vkg:mapsToTable`` BARE, so Ontop's
    "part before the dot" derivation returns the alphabetically-first TABLE as the
    schema and the rule's SQL died ``SCHEMA_NOT_FOUND: Schema 'cards'``. The tier
    then fell through to Tier 2, which answered a different number — invisibly.
    The caller must therefore hand the executor the layer's real pair; the
    executor's own half of the contract is in ``test_rule_exec_coordinate.py``.
    """
    _enable(monkeypatch)
    monkeypatch.setattr(main, "rule_lookup_fn", lambda **kw: _FakeRule())
    monkeypatch.setattr(main, "_cached_ontology_json", lambda **kw: {})
    seen: List[Dict[str, Any]] = []

    def capture(**kw: Any) -> Dict[str, Any]:
        """Record the executor kwargs and return a successful result."""
        seen.append(kw)
        return {"columns": ["n"], "rows": [[52]], "sql": "SELECT 1",
                "database": "bird_card_games", "catalog": "cat"}

    monkeypatch.setattr(main, "rule_execute_fn", capture)
    _answer(config={"dataSources": [{"catalogId": "cat", "databaseName": "db"}]})
    assert seen[0]["coordinate"] == ("cat", "db")


def test_a_config_with_no_data_sources_falls_through_rather_than_guessing(
    monkeypatch,
) -> None:
    """A hybrid layer with no ``dataSources`` has no coordinate to run against.

    ``_catalog_database_for_layer`` returns ``("", "")`` there, and the executor
    refuses it loudly rather than silently reverting to Ontop's derivation — which
    is the defect above. The tier stays fail-soft, so the turn continues to Tier 2.
    """
    _enable(monkeypatch)
    monkeypatch.setattr(main, "rule_lookup_fn", lambda **kw: _FakeRule())
    monkeypatch.setattr(main, "_cached_ontology_json", lambda **kw: {})
    # The REAL executor, not a stub — the refusal under test is its own. Both hops
    # are stubbed to SUCCEED so a passing assertion cannot come from a failed
    # translation or a gateway that is simply absent under test.
    monkeypatch.setattr(main, "_rule_translate",
                        lambda **kw: {"sql": "SELECT 1", "database": "cards",
                                      "catalog": "AwsDataCatalog"})
    monkeypatch.setattr(main, "_run_rule_sql",
                        lambda **kw: pytest.fail("must not reach Athena"))
    assert _answer(config={}) is None


def test_a_rule_with_missing_inputs_routes_to_advisory(monkeypatch) -> None:
    """The advise-rather-than-evaluate branch — no SQL is executed."""
    _enable(monkeypatch)
    rule = _FakeRule(missing_inputs=["https://ex.org/vkg#daysSinceLastPayment"])
    monkeypatch.setattr(main, "rule_lookup_fn", lambda **kw: rule)
    monkeypatch.setattr(main, "rule_execute_fn",
                        lambda **kw: pytest.fail("must not execute"))
    gaps: List[Any] = []
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
    captured: List[Any] = []
    _stub_advisory(monkeypatch, captured)

    _answer()
    assert captured[0]["unapproved_thresholds"] == ["THR-1"]


def test_the_advise_branch_agrees_with_the_executor_gate(monkeypatch) -> None:
    """An unapplied unapproved bound must not stall an otherwise-runnable rule.

    The branch here and ``rule_executor``'s refusal must be the same predicate.
    If this branch were stricter the rule would advise forever; if it were laxer
    the executor would refuse and the rule would fall to Tier 2 forever. Both
    bugs are invisible in production, so the agreement is pinned here.
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


# --- The Athena adapter -----------------------------------------------------


def test_the_athena_adapter_reports_a_query_failure_as_a_failure(monkeypatch) -> None:
    """``athena_run_query`` says ``error``; ``execute_rule`` reads
    ``state_change_reason``.

    The hybrid executor's failure key differs from the VKG agent's, so without
    this translation a FAILED Athena query would reach ``execute_rule`` looking
    like a successful run that returned no rows — a governed rule answering
    "0 results" for a query that never executed. The worst possible outcome for
    a governance feature, and silent.
    """
    monkeypatch.setattr(
        main, "_rule_athena_run",
        lambda **kw: {"error": "SYNTAX_ERROR: line 1:8", "query_execution_id": "q-1"},
    )
    out = main._run_rule_sql(sql="SELECT 1", database_name="d", catalog_id="")
    assert out["state_change_reason"] == "SYNTAX_ERROR: line 1:8"
    assert out["rows"] == []


def test_the_athena_adapter_passes_a_successful_result_through(monkeypatch) -> None:
    """A successful run carries no failure reason, so ``execute_rule`` proceeds."""
    monkeypatch.setattr(
        main, "_rule_athena_run",
        lambda **kw: {"columns": ["c"], "rows": [["1"]], "query_execution_id": "q-2"},
    )
    out = main._run_rule_sql(sql="SELECT 1", database_name="d", catalog_id="cat")
    assert out["state_change_reason"] is None
    assert out["columns"] == ["c"]
    assert out["rows"] == [["1"]]


# --- Ordering: the call site, not just the function ------------------------


def _stub_metadata(monkeypatch) -> None:
    """Stub the layer-metadata read so _run_query_core reaches the cascade."""
    monkeypatch.setattr(main, "get_latest_metadata_item",
                        lambda _id: {"name": "demo", "namespace": "ns-default"})


def test_the_rule_tier_runs_after_a_tier_1_miss_and_before_tier_2(
        monkeypatch) -> None:
    """A rule answer short-circuits the hybrid Tier-2 workflow entirely."""
    _enable(monkeypatch)
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "_tier1_lookup", lambda **_kw: None)
    payload = {"answer": "a", "sql_query": "SELECT 1", "results": [],
               "n_quads": [], "reasoning": {}, "metadata": {},
               "provenance": {"tier": "governed_rule", "sources": []}}
    monkeypatch.setattr(main, "_rule_tier_answer", lambda **_kw: payload)
    calls: List[str] = []
    monkeypatch.setattr(main, "_resolve",
                        lambda **_kw: calls.append("tier2") or {})

    out = main._run_query_core({"question": "which holdings lapse?",
                                "id": "hybrid-1"})

    assert calls == []
    assert out["provenance"]["tier"] == "governed_rule"


def test_a_rule_miss_still_reaches_tier_2(monkeypatch) -> None:
    """The tier is a short-circuit, not a gate: a miss must not block hybrid."""
    _enable(monkeypatch)
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(main, "_tier1_lookup", lambda **_kw: None)
    monkeypatch.setattr(main, "_rule_tier_answer", lambda **_kw: None)
    monkeypatch.setattr(main, "_artifacts_for",
                        lambda _cfg, **_kw: {"kb": True, "vkg": True})
    monkeypatch.setattr(main, "_resolve", lambda **_kw: {"answer": "tier2"})

    assert main._run_query_core({"question": "q", "id": "hybrid-1"})["answer"] == (
        "tier2")


def test_a_tier_1_metric_hit_wins_over_a_rule(monkeypatch) -> None:
    """Design §7: a governed metric is more specific and cheaper than a rule."""
    _enable(monkeypatch)
    _stub_metadata(monkeypatch)
    metric = SimpleNamespace(metric_id="monthly_revenue", compiled_sql="SELECT 1")
    monkeypatch.setattr(main, "_tier1_lookup", lambda **_kw: metric)
    monkeypatch.setattr(main, "_catalog_database_for_layer", lambda _cfg: ("", "db"))
    monkeypatch.setattr(main, "_athena_client", lambda: object())
    monkeypatch.setattr(
        main, "_build_response_from_metric",
        lambda **_kw: {"provenance": {"tier": "governed_metric", "sources": []}},
    )
    # execute_metric is imported inside the Tier-1 block, so patch the module it
    # is imported FROM rather than a name on main.
    from agents.semantic_layer_query_agent import metric_executor

    monkeypatch.setattr(metric_executor, "execute_metric",
                        lambda **_kw: {"columns": [], "rows": []})
    calls: List[str] = []
    monkeypatch.setattr(main, "_rule_tier_answer",
                        lambda **_kw: calls.append("rule"))

    out = main._run_query_core({"question": "monthly revenue", "id": "hybrid-1"})

    assert calls == []
    assert out["provenance"]["tier"] == "governed_metric"
