"""HTTP contract of the /rules router."""
import os
import sys
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# `routers.*` / `services.*` live under the REST-API package root, which is not
# on sys.path by default (only the repo root is, via pyproject's `pythonpath`).
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

from agents.shared.rule_models import (  # noqa: E402
    Condition, Conclusion, Rule, RuleScope, Threshold,
)
from agents.shared.rule_graph import RuleReadError  # noqa: E402
from routers.rules import (  # noqa: E402
    build_router, register_rule_error_handlers,
)


def _rule(**over) -> Rule:
    base = dict(
        rule_id="RULE-004", namespace="ont-1", name="lapse risk",
        description="d", rule_type="diagnostic", level="contextualised",
        scope=RuleScope(subject_class="https://ex.org/vkg#Holding"),
        conditions=[Condition(binding="https://ex.org/vkg#days", operator=">",
                              threshold_id="THR-011")],
        conclusion=Conclusion(outcome="https://ex.org/vkg#LapseRisk"),
        thresholds=[Threshold(threshold_id="THR-011", value=90.0, unit="days",
                              source_span="after 90 days")],
    )
    base.update(over)
    return Rule(**base)


@pytest.fixture()
def client_and_svc():
    svc = MagicMock()
    # MagicMock would return a MagicMock for the (rules, failures) pair, which is
    # not unpackable — give the list route a healthy default.
    svc.list_partitioned.return_value = ([], [])
    app = FastAPI()
    app.include_router(build_router(lambda: svc), prefix="/rules")
    # Mirror main.py: the handler is registered on the app, not the router.
    register_rule_error_handlers(app)
    # raise_server_exceptions=False so the registered 500 handler's body is
    # observable instead of the exception being re-raised into the test.
    return TestClient(app, raise_server_exceptions=False), svc


def test_create_returns_201(client_and_svc) -> None:
    client, svc = client_and_svc
    svc.create.return_value = _rule()
    resp = client.post("/rules/namespaces/ont-1/rules",
                       json=_rule().model_dump(mode="json"))
    assert resp.status_code == 201
    assert resp.json()["rule_id"] == "RULE-004"


def test_create_rejects_a_namespace_mismatch(client_and_svc) -> None:
    client, _svc = client_and_svc
    resp = client.post("/rules/namespaces/other/rules",
                       json=_rule().model_dump(mode="json"))
    assert resp.status_code == 400


def test_get_returns_404_when_absent(client_and_svc) -> None:
    client, svc = client_and_svc
    svc.get.return_value = None
    assert client.get("/rules/namespaces/ont-1/rules/nope").status_code == 404


def test_list_is_namespace_scoped(client_and_svc) -> None:
    client, svc = client_and_svc
    svc.list_partitioned.return_value = ([_rule()], [])
    resp = client.get("/rules/namespaces/ont-1/rules")
    assert resp.status_code == 200
    assert [r["rule_id"] for r in resp.json()["rules"]] == ["RULE-004"]
    svc.list_partitioned.assert_called_once_with(namespace="ont-1")


def test_list_reports_an_unreadable_rule_instead_of_500ing(client_and_svc) -> None:
    """One corrupt rule must not blank the whole namespace's list.

    And it must be REPORTED: a rule quietly missing from the list is the failure
    mode that made the threshold-IRI incident look like data loss.
    """
    client, svc = client_and_svc
    svc.list_partitioned.return_value = (
        [_rule()], [{"rule_id": "RULE-BAD", "error": "missing required unit"}],
    )
    resp = client.get("/rules/namespaces/ont-1/rules")
    assert resp.status_code == 200
    body = resp.json()
    assert [r["rule_id"] for r in body["rules"]] == ["RULE-004"]
    assert body["unreadable"] == [
        {"rule_id": "RULE-BAD", "error": "missing required unit"}
    ]


def test_getting_an_unreadable_rule_500s_naming_it_rather_than_404ing(
    client_and_svc,
) -> None:
    """A 404 would claim the rule does not exist when it does.

    500 also means the caller is not told to fix a payload they never sent.
    """
    client, svc = client_and_svc
    svc.get.side_effect = RuleReadError(
        "rule RULE-BAD: threshold THR-011 is missing required unit",
        rule_id="RULE-BAD",
    )
    resp = client.get("/rules/namespaces/ont-1/rules/RULE-BAD")
    assert resp.status_code == 500
    body = resp.json()
    assert body["rule_id"] == "RULE-BAD"
    assert "missing required unit" in body["detail"]


def test_approve_threshold_passes_the_caller_identity(client_and_svc) -> None:
    client, svc = client_and_svc
    svc.approve_threshold.return_value = _rule()
    resp = client.post(
        "/rules/namespaces/ont-1/rules/RULE-004/thresholds/THR-011:approve",
        json={"approver": "alice@example.com"})
    assert resp.status_code == 200
    svc.approve_threshold.assert_called_once_with(
        namespace="ont-1", rule_id="RULE-004", threshold_id="THR-011",
        approver="alice@example.com")


def test_approve_threshold_rejects_a_blank_approver(client_and_svc) -> None:
    client, svc = client_and_svc
    svc.approve_threshold.side_effect = ValueError("approver must be non-blank")
    resp = client.post(
        "/rules/namespaces/ont-1/rules/RULE-004/thresholds/THR-011:approve",
        json={"approver": " "})
    assert resp.status_code == 422


def test_publish_surfaces_a_blocked_gate_as_422_with_the_reason(client_and_svc) -> None:
    """The reason must reach the admin UI verbatim — a bare 500 is useless
    to someone trying to fix a rule."""
    from agents.shared.rule_publisher import PublishBlocked
    client, svc = client_and_svc
    svc.publish_rule.side_effect = PublishBlocked(
        "validation failed — operational: binding X has no mapping")
    resp = client.post("/rules/namespaces/ont-1/rules/RULE-004:publish")
    assert resp.status_code == 422
    assert "no mapping" in resp.json()["detail"]


def test_publish_returns_404_for_an_unknown_rule(client_and_svc) -> None:
    client, svc = client_and_svc
    svc.publish_rule.side_effect = KeyError("nope")
    assert client.post(
        "/rules/namespaces/ont-1/rules/nope:publish").status_code == 404


def test_validate_returns_the_four_reports(client_and_svc) -> None:
    client, svc = client_and_svc
    svc.validate.return_value = {
        "reports": [{"level": "structural", "passed": True, "violations": []}],
        "missing_inputs": [],
    }
    resp = client.post("/rules/namespaces/ont-1/rules/RULE-004:validate")
    assert resp.status_code == 200
    assert resp.json()["reports"][0]["level"] == "structural"


def test_delete_returns_204(client_and_svc) -> None:
    client, svc = client_and_svc
    assert client.delete(
        "/rules/namespaces/ont-1/rules/RULE-004").status_code == 204


def test_import_passes_the_artefact_key_through(client_and_svc) -> None:
    """The pipeline reports `minedRulesKey`; the admin hands that back verbatim
    rather than the router guessing a key from a document id."""
    client, svc = client_and_svc
    svc.import_mined.return_value = [_rule()]
    resp = client.post("/rules/namespaces/ont-1/rules:import",
                       json={"key": "mined-rules/doc-1.json"})
    assert resp.status_code == 201
    svc.import_mined.assert_called_once_with(
        ontology_id="ont-1", key="mined-rules/doc-1.json")
    assert resp.json()["count"] == 1


def test_import_surfaces_a_malformed_artefact_as_422(client_and_svc) -> None:
    """A threshold with no source span is the producer's bug, not the caller's —
    but the reviewer still needs to see WHICH artefact is unusable."""
    client, svc = client_and_svc
    svc.import_mined.side_effect = ValueError("threshold THR-1 has no sourceSpan")
    resp = client.post("/rules/namespaces/ont-1/rules:import",
                       json={"key": "mined-rules/doc-1.json"})
    assert resp.status_code == 422
    assert "sourceSpan" in resp.json()["detail"]


def test_import_returns_404_for_a_missing_artefact(client_and_svc) -> None:
    client, svc = client_and_svc
    svc.import_mined.side_effect = KeyError("mined-rules/nope.json")
    resp = client.post("/rules/namespaces/ont-1/rules:import",
                       json={"key": "mined-rules/nope.json"})
    assert resp.status_code == 404
