"""rule_executor — the two-hop execution and the epistemic answer wording.

Two contracts are pinned here.

**Refusal, not repair.** Tier 2 repairs a failed translation with an LLM because
its SPARQL was model-generated. A rule's SPARQL was compiled deterministically
and smoke-executed at publish, so a failure here is a regression in a published
artefact — repairing it would paper over that. Every failure path raises.

**The gate matches ``Rule.is_evaluable``.** The thresholds that block execution
are the ones a CONDITION references, which is exactly what ``is_evaluable`` and
``rule_compiler`` check. A gate stricter than the predicate the caller branched
on would refuse a rule the caller was told to execute, and the fall-through
would be indistinguishable from an Ontop failure.

Every call here passes ``coordinate=None`` — "use the translation's own pair" —
because none of these cases is about WHERE the SQL runs. That contract, and the
hybrid override that motivated making the parameter required, live in
``test_rule_exec_coordinate.py``.
"""
from typing import Any, Dict, List

import pytest

from agents.shared.rule_executor import (
    EPISTEMIC_PHRASE,
    RuleExecutionError,
    build_rule_answer,
    execute_rule,
)


def _threshold(threshold_id: str = "THR-1", approved_by: str = "alice") -> Any:
    """Build a duck-typed threshold with a bound ``is_approved``.

    :param threshold_id: Identifier a condition can reference.
    :param approved_by: Approver name; empty means unapproved.
    :returns: An object with ``Threshold``'s executor-visible surface.
    """
    return type("T", (), {
        "threshold_id": threshold_id, "value": 90.0, "unit": "days",
        "approved_by": approved_by, "source_span": "after 90 days",
        "is_approved": lambda self: bool(approved_by)})()


class _Rule:
    """Duck-typed stand-in — keeps the test independent of rule_models.

    Carries ``conditions`` and ``lifecycle`` because both are required fields on
    the real ``Rule`` (``conditions`` has ``min_length=1``) and the executor gates
    on both. A fake without them would let the gate pass for the wrong reason.
    """

    def __init__(self, **over: Any) -> None:
        """Build a PUBLISHED, evaluable rule; ``over`` replaces any attribute."""
        self.rule_id = "RULE-004"
        self.name = "lapse risk after non-payment"
        self.lifecycle = "PUBLISHED"
        self.compiled_sparql = "SELECT ?subject WHERE { ?subject a <x> }"
        self.missing_inputs: List[str] = []
        self.conditions = [type("C", (), {
            "binding": "https://ex.org/vkg#daysSinceLastPayment",
            "operator": ">", "threshold_id": "THR-1", "value": None})()]
        self.conclusion = type("C", (), {
            "outcome": "https://ex.org/vkg#LapseRisk",
            "epistemic_status": "candidateContributorTo"})()
        self.thresholds = [_threshold()]
        self.provenance = type("P", (), {
            "document_id": "policy-2026.pdf", "page": 12,
            "source_sentence": "A holding lapses after 90 days of non-payment."})()
        for key, value in over.items():
            setattr(self, key, value)


def _ok_translate(**_kw: Any) -> Dict[str, Any]:
    """Stand in for ``NeptuneGatewayClient.translate_sql`` on success."""
    return {"sql": "SELECT id FROM holding", "database": "acme", "catalog": ""}


def _ok_execute(**_kw: Any) -> Dict[str, Any]:
    """Stand in for ``_run_athena_sql`` on success."""
    return {"columns": ["id"], "rows": [["H-1"], ["H-2"]],
            "state_change_reason": None}


def test_a_published_rule_executes_and_returns_rows() -> None:
    """The happy path: SPARQL → SQL → rows, with the SQL echoed back."""
    out = execute_rule(
        rule=_Rule(), ontology_json={"classes": {}}, ontology_id="ont-1",
        translate=_ok_translate, execute_sql=_ok_execute, coordinate=None,
    )
    assert out["rows"] == [["H-1"], ["H-2"]]
    assert out["sql"] == "SELECT id FROM holding"


def test_the_translation_receives_the_ontology_id_for_cache_warmth() -> None:
    """A stable id lets the Ontop Handler reuse its warm reformulator (PC=1)."""
    seen: List[Dict[str, Any]] = []

    def capture(**kw: Any) -> Dict[str, Any]:
        seen.append(kw)
        return _ok_translate()

    execute_rule(rule=_Rule(), ontology_json={"classes": {}}, ontology_id="ont-1",
                 translate=capture, execute_sql=_ok_execute, coordinate=None)
    assert seen[0]["ontology_id"] == "ont-1"
    assert seen[0]["sparql"] == _Rule().compiled_sparql


def test_a_rule_with_missing_inputs_is_refused() -> None:
    """The evaluate/advise switch — executor must not be the weak link."""
    with pytest.raises(RuleExecutionError):
        execute_rule(
            rule=_Rule(missing_inputs=["https://ex.org/vkg#x"]),
            ontology_json={}, ontology_id="ont-1",
            translate=_ok_translate, execute_sql=_ok_execute, coordinate=None,
        )


def test_a_rule_with_no_compiled_sparql_is_refused() -> None:
    """No compiled artefact means nothing was ever smoke-executed at publish."""
    with pytest.raises(RuleExecutionError):
        execute_rule(rule=_Rule(compiled_sparql=""), ontology_json={},
                     ontology_id="ont-1", translate=_ok_translate,
                     execute_sql=_ok_execute, coordinate=None)


def test_a_rule_that_is_not_published_is_refused() -> None:
    """Only a PUBLISHED rule may answer a user's question.

    ``rule_lookup`` re-checks this at read time, but the executor is the boundary
    that actually runs a query, so it re-checks too: no future caller can execute
    a DRAFT or merely APPROVED rule by wiring itself in without the lookup.
    """
    with pytest.raises(RuleExecutionError):
        execute_rule(rule=_Rule(lifecycle="APPROVED"), ontology_json={},
                     ontology_id="ont-1", translate=_ok_translate,
                     execute_sql=_ok_execute, coordinate=None)


def test_an_unapproved_threshold_is_refused() -> None:
    """An unapproved bound that the rule APPLIES blocks execution."""
    with pytest.raises(RuleExecutionError):
        execute_rule(rule=_Rule(thresholds=[_threshold(approved_by="")]),
                     ontology_json={}, ontology_id="ont-1",
                     translate=_ok_translate, execute_sql=_ok_execute,
                     coordinate=None)


def test_an_unapplied_unapproved_threshold_does_not_block_execution() -> None:
    """The gate is scoped exactly like ``Rule.is_evaluable``.

    A threshold no condition references is not part of the evaluation:
    ``rule_compiler`` never reads it, so a rule carrying an orphan unapproved
    bound can legitimately be PUBLISHED and ``is_evaluable`` returns True. If the
    executor refused it anyway, the caller would be told to execute and the rule
    would silently fall through to Tier 2 for good.
    """
    out = execute_rule(
        rule=_Rule(thresholds=[_threshold(),
                               _threshold(threshold_id="THR-9", approved_by="")]),
        ontology_json={}, ontology_id="ont-1",
        translate=_ok_translate, execute_sql=_ok_execute, coordinate=None,
    )
    assert out["rows"] == [["H-1"], ["H-2"]]


def test_a_translation_failure_raises_rather_than_repairing() -> None:
    """No LLM repair: compiled SPARQL smoke-passed at publish, so this is a bug."""
    with pytest.raises(RuleExecutionError):
        execute_rule(rule=_Rule(), ontology_json={}, ontology_id="ont-1",
                     translate=lambda **kw: {"error": "cannot reformulate"},
                     execute_sql=_ok_execute, coordinate=None)


def test_a_raising_translation_is_normalised_to_one_error_type() -> None:
    """The gateway raises on non-JSON; the caller only handles RuleExecutionError."""
    def boom(**_kw: Any) -> Dict[str, Any]:
        raise RuntimeError("translate_sparql_to_sql returned non-JSON")

    with pytest.raises(RuleExecutionError):
        execute_rule(rule=_Rule(), ontology_json={}, ontology_id="ont-1",
                     translate=boom, execute_sql=_ok_execute, coordinate=None)


def test_an_athena_failure_raises() -> None:
    """``_run_athena_sql`` reports query failure as a dict, so it is inspected."""
    with pytest.raises(RuleExecutionError):
        execute_rule(
            rule=_Rule(), ontology_json={}, ontology_id="ont-1",
            translate=_ok_translate,
            execute_sql=lambda **kw: {"columns": [], "rows": [],
                                      "state_change_reason": "TABLE_NOT_FOUND"},
            coordinate=None,
        )


def test_a_raising_athena_call_is_normalised_to_one_error_type() -> None:
    """A boto3 ClientError propagates out of ``_run_athena_sql`` — catch it here."""
    def boom(**_kw: Any) -> Dict[str, Any]:
        raise RuntimeError("ClientError: InvalidRequestException")

    with pytest.raises(RuleExecutionError):
        execute_rule(rule=_Rule(), ontology_json={}, ontology_id="ont-1",
                     translate=_ok_translate, execute_sql=boom, coordinate=None)


def test_the_answer_states_the_epistemic_status_in_words() -> None:
    """The claim's strength is template-driven, so it cannot drift."""
    answer = build_rule_answer(rule=_Rule(), result=_ok_execute())
    assert EPISTEMIC_PHRASE["candidateContributorTo"] in answer
    assert "confirmed" not in answer.lower() or "not a confirmed" in answer.lower()


def test_the_answer_names_the_threshold_and_its_approver() -> None:
    """The user sees the bound applied and who stands behind it."""
    answer = build_rule_answer(rule=_Rule(), result=_ok_execute())
    assert "90" in answer
    assert "alice" in answer


def test_the_answer_cites_only_the_thresholds_the_rule_applies() -> None:
    """An orphan bound was never used, so claiming it was would be false.

    It would also read as "approved by None" — an unapproved orphan can reach a
    PUBLISHED rule (see the unapplied-threshold test above).
    """
    answer = build_rule_answer(
        rule=_Rule(thresholds=[_threshold(),
                               _threshold(threshold_id="THR-9", approved_by="")]),
        result=_ok_execute(),
    )
    assert "THR-1" in answer
    assert "THR-9" not in answer
    assert "None" not in answer


def test_the_answer_cites_the_source_document_and_page() -> None:
    """Provenance travels to the user, not only into the audit record."""
    answer = build_rule_answer(rule=_Rule(), result=_ok_execute())
    assert "policy-2026.pdf" in answer
    assert "12" in answer


def test_the_answer_reports_the_row_count() -> None:
    """How many records matched is the answer's actual payload."""
    answer = build_rule_answer(rule=_Rule(), result=_ok_execute())
    assert "2" in answer


def test_a_zero_row_result_reads_as_none_matched_not_as_an_error() -> None:
    """No rows is a real, correct answer — the rule ran and nothing fired."""
    answer = build_rule_answer(
        rule=_Rule(), result={"columns": ["id"], "rows": [],
                              "state_change_reason": None})
    assert "no " in answer.lower()


def test_a_rule_with_no_provenance_still_renders() -> None:
    """``Rule.provenance`` is Optional, so the citation clause must be optional."""
    answer = build_rule_answer(rule=_Rule(provenance=None), result=_ok_execute())
    assert "lapse risk after non-payment" in answer
    assert "Source:" not in answer


def test_every_epistemic_status_has_a_phrase() -> None:
    """A status with no phrase would render as a bare IRI to the user."""
    from agents.shared.rule_vocab import EPISTEMIC_STATUSES

    assert set(EPISTEMIC_STATUSES) == set(EPISTEMIC_PHRASE)
