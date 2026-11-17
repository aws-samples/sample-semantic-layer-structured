"""RuleService CRUD + lifecycle over a fake Neptune."""
import os
import sys

import pytest

# The REST-API package root is not on sys.path by default (only the repo root is,
# via pyproject's `pythonpath`), so `services.*` needs the same shim every other
# rest-api unit test uses.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

from agents.shared.rule_models import (  # noqa: E402
    Condition, Conclusion, Rule, RuleScope, Threshold,
)
from services.rule_service import RuleService  # noqa: E402


def _rule(rule_id: str = "RULE-004", **over) -> Rule:
    base = dict(
        rule_id=rule_id, namespace="ont-1", name="lapse risk",
        description="days since payment", rule_type="diagnostic",
        level="contextualised",
        scope=RuleScope(subject_class="https://ex.org/vkg#Holding"),
        conditions=[Condition(binding="https://ex.org/vkg#days", operator=">",
                              threshold_id="THR-011")],
        conclusion=Conclusion(outcome="https://ex.org/vkg#LapseRisk"),
        thresholds=[Threshold(threshold_id="THR-011", value=90.0, unit="days",
                              source_span="after 90 days")],
    )
    base.update(over)
    return Rule(**base)


class FakeNeptune:
    """In-memory stand-in: stores rules keyed by (namespace, rule_id)."""

    def __init__(self) -> None:
        self.store = {}
        self.updates = []

    def read_rules(self, *, namespace: str):
        return [r for (ns, _rid), r in sorted(self.store.items()) if ns == namespace]

    def read_rules_partitioned(self, *, namespace: str):
        """Everything stored here is a valid model, so nothing is ever corrupt.

        Tests that need a failure inject their own reader via ``_svc(over)``.
        """
        return self.read_rules(namespace=namespace), []

    def apply_update(self, *, sparql_update: str, rule=None):
        self.updates.append(sparql_update)
        if rule is not None:
            self.store[(rule.namespace, rule.rule_id)] = rule


def _svc(neptune: FakeNeptune, **over) -> RuleService:
    kwargs = dict(
        read_rules=lambda *, namespace: neptune.read_rules(namespace=namespace),
        read_rules_partitioned=lambda *, namespace: (
            neptune.read_rules_partitioned(namespace=namespace)
        ),
        write_rule=lambda *, rule: neptune.apply_update(
            sparql_update="INSERT DATA", rule=rule),
        delete_rule=lambda *, namespace, rule_id: neptune.apply_update(
            sparql_update="DELETE WHERE"),
        publish=lambda *, rule: rule.model_copy(
            update={"lifecycle": "PUBLISHED", "compiled_sparql": "SELECT 1"}),
        validate_fn=lambda *, rule: ([], []),
    )
    kwargs.update(over)
    return RuleService(**kwargs)


def test_create_then_get_round_trips() -> None:
    n = FakeNeptune()
    svc = _svc(n)
    svc.create(rule=_rule())
    assert svc.get(namespace="ont-1", rule_id="RULE-004").name == "lapse risk"


def test_create_forces_draft_lifecycle_even_if_the_body_claims_published() -> None:
    """A client must not be able to publish by POSTing lifecycle=PUBLISHED."""
    n = FakeNeptune()
    created = _svc(n).create(rule=_rule(lifecycle="PUBLISHED"))
    assert created.lifecycle == "DRAFT"
    assert created.compiled_sparql is None


def test_get_returns_none_for_an_unknown_rule() -> None:
    assert _svc(FakeNeptune()).get(namespace="ont-1", rule_id="nope") is None


def test_list_is_namespace_scoped() -> None:
    n = FakeNeptune()
    svc = _svc(n)
    svc.create(rule=_rule("RULE-1"))
    svc.create(rule=_rule("RULE-2", namespace="ont-2"))
    assert [r.rule_id for r in svc.list(namespace="ont-1")] == ["RULE-1"]


def test_update_rejects_an_unknown_rule() -> None:
    with pytest.raises(KeyError):
        _svc(FakeNeptune()).update(rule=_rule())


def test_update_of_a_published_rule_clears_the_compiled_sparql() -> None:
    """Editing a published rule must invalidate its compiled artefact — an
    edited rule with stale SPARQL would execute the OLD logic."""
    n = FakeNeptune()
    svc = _svc(n)
    svc.create(rule=_rule())
    svc.approve(namespace="ont-1", rule_id="RULE-004", approver="alice@x")
    svc.publish_rule(namespace="ont-1", rule_id="RULE-004")
    edited = svc.update(rule=_rule(description="changed"))
    assert edited.lifecycle == "DRAFT"
    assert edited.compiled_sparql is None


def test_approve_threshold_records_the_approver() -> None:
    n = FakeNeptune()
    svc = _svc(n)
    svc.create(rule=_rule())
    out = svc.approve_threshold(namespace="ont-1", rule_id="RULE-004",
                               threshold_id="THR-011", approver="alice@x")
    assert out.threshold_by_id(threshold_id="THR-011").approved_by == "alice@x"


def test_approve_threshold_rejects_a_blank_approver() -> None:
    n = FakeNeptune()
    svc = _svc(n)
    svc.create(rule=_rule())
    with pytest.raises(ValueError, match="approver"):
        svc.approve_threshold(namespace="ont-1", rule_id="RULE-004",
                              threshold_id="THR-011", approver="   ")


def test_approve_threshold_rejects_an_unknown_threshold_id() -> None:
    n = FakeNeptune()
    svc = _svc(n)
    svc.create(rule=_rule())
    with pytest.raises(KeyError):
        svc.approve_threshold(namespace="ont-1", rule_id="RULE-004",
                              threshold_id="THR-999", approver="alice@x")


def test_publish_delegates_to_the_injected_publish_gate() -> None:
    n = FakeNeptune()
    calls = []

    def gate(*, rule):
        calls.append(rule.rule_id)
        return rule.model_copy(update={"lifecycle": "PUBLISHED",
                                       "compiled_sparql": "SELECT 1"})

    svc = _svc(n, publish=gate)
    svc.create(rule=_rule())
    svc.approve(namespace="ont-1", rule_id="RULE-004", approver="alice@x")
    out = svc.publish_rule(namespace="ont-1", rule_id="RULE-004")
    assert calls == ["RULE-004"]
    assert out.lifecycle == "PUBLISHED"


def test_validate_flattens_the_reports_for_json() -> None:
    """The router returns this verbatim, so ValidationReport dataclasses must
    already be plain dicts here — FastAPI cannot serialise them otherwise."""
    from agents.shared.rule_validator import ValidationReport

    n = FakeNeptune()
    svc = _svc(n, validate_fn=lambda *, rule: (
        [ValidationReport(level="structural", passed=False,
                          violations=["missing ruleId"])],
        ["https://ex.org/vkg#days"],
    ))
    svc.create(rule=_rule())
    out = svc.validate(namespace="ont-1", rule_id="RULE-004")
    assert out == {
        "reports": [{"level": "structural", "passed": False,
                     "violations": ["missing ruleId"]}],
        "missing_inputs": ["https://ex.org/vkg#days"],
    }


def test_validate_rejects_an_unknown_rule() -> None:
    with pytest.raises(KeyError):
        _svc(FakeNeptune()).validate(namespace="ont-1", rule_id="nope")


def test_delete_removes_the_rule() -> None:
    n = FakeNeptune()
    svc = _svc(n)
    svc.create(rule=_rule())
    n.store.clear()  # delete_rule callable is a no-op fake; simulate removal
    svc.delete(namespace="ont-1", rule_id="RULE-004")
    assert svc.get(namespace="ont-1", rule_id="RULE-004") is None


# ── Mined-artefact import ───────────────────────────────────────────────────


_ARTEFACT = {
    "documentId": "doc-1",
    "rules": [{
        "name": "lapse risk",
        "description": "days since payment",
        "ruleType": "diagnostic",
        "scope": {"subjectClass": "https://ex.org/vkg#Holding"},
        "conditions": [{"binding": "https://ex.org/vkg#days",
                        "operator": ">", "thresholdId": "THR-1"}],
        "thresholds": [{"thresholdId": "THR-1", "value": 90.0, "unit": "days",
                        "sourceSpan": "after 90 days"}],
        "conclusion": {"outcome": "https://ex.org/vkg#LapseRisk",
                       "epistemicStatus": "candidateContributorTo"},
        "missingInputs": [],
        "sourceSentence": "A holding lapses after 90 days.",
    }],
}


def test_import_mined_creates_drafts_from_the_artefact() -> None:
    n = FakeNeptune()
    svc = _svc(n, read_artifact=lambda key: dict(_ARTEFACT, key=key))
    created = svc.import_mined(ontology_id="ont-1",
                               key="mined-rules/doc-1.json")
    assert len(created) == 1
    assert created[0].lifecycle == "DRAFT"
    assert created[0].namespace == "ont-1"
    # Went through create(), so it is actually persisted, not just returned.
    assert svc.get(namespace="ont-1", rule_id=created[0].rule_id) is not None


def test_import_mined_cannot_bypass_the_lifecycle() -> None:
    """create() re-forces DRAFT, so an artefact claiming PUBLISHED is inert."""
    artefact = {"documentId": "d",
                "rules": [dict(_ARTEFACT["rules"][0], lifecycle="PUBLISHED",
                               compiledSparql="SELECT 1")]}
    svc = _svc(FakeNeptune(), read_artifact=lambda key: artefact)
    created = svc.import_mined(ontology_id="ont-1", key="k")[0]
    assert created.lifecycle == "DRAFT"
    assert not created.compiled_sparql


def test_import_mined_without_a_reader_fails_loudly() -> None:
    """No silent no-op: a service built without S3 access must say so rather
    than report an empty import, which reads as "the document had no rules"."""
    with pytest.raises(RuntimeError):
        _svc(FakeNeptune()).import_mined(ontology_id="ont-1", key="k")


def test_import_mined_is_idempotent_for_the_same_artefact() -> None:
    """Deterministic ids mean a re-import overwrites rather than duplicating."""
    n = FakeNeptune()
    svc = _svc(n, read_artifact=lambda key: _ARTEFACT)
    svc.import_mined(ontology_id="ont-1", key="k")
    svc.import_mined(ontology_id="ont-1", key="k")
    assert len(svc.list(namespace="ont-1")) == 1


# --- threshold-id ownership -------------------------------------------------


def test_create_refuses_a_threshold_id_owned_by_another_rule() -> None:
    """Two rules claiming one threshold id makes an approval ambiguous.

    It is also the payload shape that triggered the shared-threshold-node
    corruption before threshold IRIs were rule-scoped: refusing it at the door is
    the third of the three defensive fixes.
    """
    n = FakeNeptune()
    svc = _svc(n)
    svc.create(rule=_rule("RULE-004"))
    with pytest.raises(ValueError) as caught:
        svc.create(rule=_rule("RULE-005"))
    # Name both the id and its current owner, or the author cannot act on it.
    assert "THR-011" in str(caught.value)
    assert "RULE-004" in str(caught.value)


def test_update_does_not_collide_with_its_own_thresholds() -> None:
    """The rule being updated must be excluded, or every edit would collide."""
    n = FakeNeptune()
    svc = _svc(n)
    svc.create(rule=_rule("RULE-004"))
    updated = svc.update(rule=_rule("RULE-004", name="renamed"))
    assert updated.name == "renamed"


def test_update_still_refuses_a_sibling_s_threshold_id() -> None:
    """Exempting the rule under edit must not exempt every other rule."""
    n = FakeNeptune()
    svc = _svc(n)
    svc.create(rule=_rule("RULE-004"))
    svc.create(rule=_rule("RULE-005", thresholds=[
        Threshold(threshold_id="THR-012", value=30.0, unit="days",
                  source_span="after 30 days"),
    ], conditions=[
        Condition(binding="https://ex.org/vkg#days", operator=">",
                  threshold_id="THR-012"),
    ]))
    with pytest.raises(ValueError):
        svc.update(rule=_rule("RULE-005"))


def test_create_refuses_a_threshold_id_repeated_within_one_rule() -> None:
    """A rule cannot disambiguate its own duplicate ids either."""
    n = FakeNeptune()
    duplicate = _rule("RULE-004", thresholds=[
        Threshold(threshold_id="THR-011", value=90.0, unit="days",
                  source_span="after 90 days"),
        Threshold(threshold_id="THR-011", value=30.0, unit="days",
                  source_span="after 30 days"),
    ])
    with pytest.raises(ValueError):
        _svc(n).create(rule=duplicate)


def test_a_different_namespace_may_reuse_the_id() -> None:
    """Threshold ids are namespace-scoped; a global check would block authoring."""
    n = FakeNeptune()
    svc = _svc(n)
    svc.create(rule=_rule("RULE-004"))
    other = svc.create(rule=_rule("RULE-004", namespace="ont-2"))
    assert other.namespace == "ont-2"


# --- reading around an unreadable rule -------------------------------------


def test_get_raises_rather_than_404ing_for_an_unreadable_rule() -> None:
    """Returning None becomes a 404, claiming a stored rule does not exist."""
    from agents.shared.rule_graph import RuleReadError

    svc = _svc(FakeNeptune(), read_rules_partitioned=lambda *, namespace: (
        [], [{"rule_id": "RULE-BAD", "error": "missing required unit"}]
    ))
    with pytest.raises(RuleReadError) as caught:
        svc.get(namespace="ont-1", rule_id="RULE-BAD")
    assert caught.value.rule_id == "RULE-BAD"
    assert "missing required unit" in str(caught.value)


def test_get_still_reads_a_healthy_rule_beside_an_unreadable_one() -> None:
    """The actual improvement: a corrupt SIBLING no longer blocks a good rule."""
    svc = _svc(FakeNeptune(), read_rules_partitioned=lambda *, namespace: (
        [_rule("RULE-004")],
        [{"rule_id": "RULE-BAD", "error": "missing required unit"}],
    ))
    assert svc.get(namespace="ont-1", rule_id="RULE-004").rule_id == "RULE-004"
    # An unknown id is still simply absent, not a read failure.
    assert svc.get(namespace="ont-1", rule_id="RULE-NOPE") is None


def test_list_partitioned_passes_the_failures_through() -> None:
    """The service must not drop the failures on the way to the router."""
    failures = [{"rule_id": "RULE-BAD", "error": "missing required unit"}]
    svc = _svc(FakeNeptune(), read_rules_partitioned=lambda *, namespace: (
        [_rule("RULE-004")], failures,
    ))
    rules, reported = svc.list_partitioned(namespace="ont-1")
    assert [r.rule_id for r in rules] == ["RULE-004"]
    assert reported == failures
