"""Unit tests for the AgentCore eval-config custom-resource handler.

Two behaviours are pinned:

1. ``_evaluator_list``'s ``BuiltinEvaluatorIds`` override — replaces the default
   built-in set so the two query-agent configs can drop the un-editable
   ``Builtin.GoalSuccessRate`` in favour of the custom reference-free GoalSuccess
   judge. The subtle part is the empty-list-vs-absent distinction and tolerating
   CFN's two serialization forms (a real list, or a JSON-encoded string).

2. The async onEvent + isComplete waiter contract — on_event issues the delete
   and defers; is_complete performs the create and returns IsComplete=False
   (retry) while the name is still held by an in-flight delete. The load-bearing
   invariant is that an Update NEVER reuses a resource by name on
   ConflictException (that net-deletes the resource — the 2026-07-01 outage),
   while a Create does reuse an orphaned same-name resource idempotently.

The handler lives under ``cdk/lib/stacks/backend/agentcore-eval-handler/`` (not
on an importable package path), so it is loaded by file path. It imports boto3
at module load but the tested functions take an injected fake client.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_HANDLER_PATH = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "cdk", "lib", "stacks", "backend",
    "agentcore-eval-handler", "index.py",
)


@pytest.fixture(scope="module")
def handler():
    """Load the handler module from its file path (it's not on sys.path)."""
    spec = importlib.util.spec_from_file_location("eval_handler", _HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ids(evaluators):
    """Extract the ordered evaluatorId list from the handler's output."""
    return [e["evaluatorId"] for e in evaluators]


def test_default_builtins_when_no_override(handler):
    """Absent BuiltinEvaluatorIds → both default built-ins (GoalSuccessRate+Correctness)."""
    assert _ids(handler._evaluator_list({})) == [
        "Builtin.GoalSuccessRate",
        "Builtin.Correctness",
    ]


def test_empty_override_falls_through_to_default(handler):
    """An EMPTY BuiltinEvaluatorIds list is falsy → default set, NOT an empty set.

    The 3 non-query configs pass [] and must keep both built-ins; only a
    non-empty override replaces them.
    """
    assert _ids(handler._evaluator_list({"BuiltinEvaluatorIds": []})) == [
        "Builtin.GoalSuccessRate",
        "Builtin.Correctness",
    ]


def test_nonempty_override_replaces_default(handler):
    """The query configs pass ['Builtin.Correctness'] → GoalSuccessRate dropped."""
    assert _ids(
        handler._evaluator_list({"BuiltinEvaluatorIds": ["Builtin.Correctness"]})
    ) == ["Builtin.Correctness"]


def test_override_then_extra_custom_ids_appended(handler):
    """Custom ExtraEvaluatorIds append AFTER the (overridden) built-ins, in order."""
    out = handler._evaluator_list({
        "BuiltinEvaluatorIds": ["Builtin.Correctness"],
        "ExtraEvaluatorIds": ["goal-id", "sql-id", "order-id"],
    })
    assert _ids(out) == ["Builtin.Correctness", "goal-id", "sql-id", "order-id"]


def test_json_string_serialization_tolerated(handler):
    """CFN may deliver the list as a JSON STRING; both props must parse it."""
    out = handler._evaluator_list({
        "BuiltinEvaluatorIds": '["Builtin.Correctness"]',
        "ExtraEvaluatorIds": '["goal-id"]',
    })
    assert _ids(out) == ["Builtin.Correctness", "goal-id"]


def test_falsy_extra_ids_are_skipped(handler):
    """Empty-string ids (CFN token that resolved to nothing) are filtered out."""
    out = handler._evaluator_list({
        "BuiltinEvaluatorIds": ["Builtin.Correctness"],
        "ExtraEvaluatorIds": ["good-id", ""],
    })
    assert _ids(out) == ["Builtin.Correctness", "good-id"]


# ── Async onEvent + isComplete waiter behavior ────────────────────────────────
# The handler is a CDK provider-framework async custom resource: on_event issues
# the delete and returns immediately; is_complete is polled by the waiter state
# machine, performing the create and returning IsComplete=False (retry) while the
# name is still held by the in-flight delete. These tests pin the invariants that
# prevent the net-delete bug (query configs went offline 2026-07-01).


class _Conflict(Exception):
    """Stand-in for client.exceptions.ConflictException."""


class _FakeExceptions:
    """Mimics the boto3 client's ``.exceptions`` namespace."""

    ConflictException = _Conflict


class _FakeClient:
    """Minimal fake bedrock-agentcore-control client recording calls.

    ``create_behavior`` / ``eval_create_behavior`` are callables invoked on each
    create attempt so a test can raise ConflictException, raise a permissions
    error, or return a success payload.
    """

    def __init__(self, *, create_behavior=None, list_configs=None, list_evaluators=None):
        self.exceptions = _FakeExceptions()
        self.deleted_config_ids: list = []
        self.deleted_evaluator_ids: list = []
        self._create_behavior = create_behavior
        self._list_configs = list_configs or []
        self._list_evaluators = list_evaluators or []

    # config API
    def delete_online_evaluation_config(self, *, onlineEvaluationConfigId):
        self.deleted_config_ids.append(onlineEvaluationConfigId)

    def create_online_evaluation_config(self, **kwargs):
        return self._create_behavior(kwargs)

    def list_online_evaluation_configs(self):
        return {"onlineEvaluationConfigs": self._list_configs}

    # evaluator API
    def delete_evaluator(self, *, evaluatorId):
        self.deleted_evaluator_ids.append(evaluatorId)

    def create_evaluator(self, **kwargs):
        return self._create_behavior(kwargs)

    def list_evaluators(self, **kwargs):
        return {"evaluators": self._list_evaluators}


_CONFIG_PROPS = {
    "Kind": "config",
    "ConfigName": "proj_metadata_eval",
    "SamplingRate": 100,
    "LogGroupName": "/aws/x",
    "ServiceName": "svc.DEFAULT",
    "ExecutionRoleArn": "arn:aws:iam::1:role/x",
}


def test_on_event_update_deletes_old_id_and_defers_create(handler):
    """on_event(Update) deletes the current PhysicalResourceId and creates nothing."""
    client = _FakeClient(create_behavior=lambda _k: pytest.fail("create must not run in on_event"))
    out = handler._on_config_event(
        {"RequestType": "Update", "PhysicalResourceId": "old-cfg-id",
         "ResourceProperties": _CONFIG_PROPS},
        client,
    )
    assert client.deleted_config_ids == ["old-cfg-id"]
    # Empty result → framework keeps the existing PhysicalResourceId until is_complete.
    assert out == {}


def test_on_event_create_deletes_nothing(handler):
    """on_event(Create) issues no delete (there is no prior resource)."""
    client = _FakeClient(create_behavior=lambda _k: pytest.fail("create must not run in on_event"))
    handler._on_config_event(
        {"RequestType": "Create", "ResourceProperties": _CONFIG_PROPS}, client
    )
    assert client.deleted_config_ids == []


def test_is_complete_delete_is_immediately_complete(handler):
    """is_complete(Delete) completes at once — on_event already issued the delete."""
    client = _FakeClient()
    out = handler._is_complete_config_event(
        {"RequestType": "Delete", "PhysicalResourceId": "cfg", "ResourceProperties": _CONFIG_PROPS},
        client,
    )
    assert out == {"IsComplete": True}


def test_is_complete_create_success_returns_new_id(handler):
    """A successful create returns IsComplete + the new id as PhysicalResourceId/Data."""
    client = _FakeClient(
        create_behavior=lambda _k: {"onlineEvaluationConfigId": "new-cfg-id"}
    )
    out = handler._is_complete_config_event(
        {"RequestType": "Update", "PhysicalResourceId": "old", "ResourceProperties": _CONFIG_PROPS},
        client,
    )
    assert out == {"IsComplete": True, "PhysicalResourceId": "new-cfg-id",
                   "Data": {"ConfigId": "new-cfg-id"}}


def test_is_complete_update_conflict_waits_never_reuses_by_name(handler):
    """CRITICAL: Update + ConflictException returns IsComplete=False and does NOT
    resolve the name to an existing id — resolving would net-delete the resource
    (the 2026-07-01 outage). The waiter simply retries until the name frees."""
    # A stale same-name config is present in the list; the handler must IGNORE it on Update.
    client = _FakeClient(
        create_behavior=lambda _k: (_ for _ in ()).throw(_Conflict()),
        list_configs=[{"onlineEvaluationConfigName": "proj_metadata_eval",
                       "onlineEvaluationConfigId": "stale-corpse-id"}],
    )
    out = handler._is_complete_config_event(
        {"RequestType": "Update", "PhysicalResourceId": "old", "ResourceProperties": _CONFIG_PROPS},
        client,
    )
    assert out == {"IsComplete": False}


def test_is_complete_create_conflict_reuses_orphan_by_name(handler):
    """Create + ConflictException DOES reuse an orphaned same-name config (idempotent)."""
    client = _FakeClient(
        create_behavior=lambda _k: (_ for _ in ()).throw(_Conflict()),
        list_configs=[{"onlineEvaluationConfigName": "proj_metadata_eval",
                       "onlineEvaluationConfigId": "orphan-id"}],
    )
    out = handler._is_complete_config_event(
        {"RequestType": "Create", "ResourceProperties": _CONFIG_PROPS}, client
    )
    assert out == {"IsComplete": True, "PhysicalResourceId": "orphan-id",
                   "Data": {"ConfigId": "orphan-id"}}


def test_is_complete_create_conflict_no_orphan_waits(handler):
    """Create + ConflictException with the name still mid-delete (no reusable
    orphan found) waits for the name to free rather than fabricating an id."""
    client = _FakeClient(
        create_behavior=lambda _k: (_ for _ in ()).throw(_Conflict()),
        list_configs=[],
    )
    out = handler._is_complete_config_event(
        {"RequestType": "Create", "ResourceProperties": _CONFIG_PROPS}, client
    )
    assert out == {"IsComplete": False}


def test_is_complete_permissions_error_waits(handler):
    """An IAM-propagation error ('permissions' in message) returns IsComplete=False
    so the waiter retries, rather than failing the deploy."""
    client = _FakeClient(
        create_behavior=lambda _k: (_ for _ in ()).throw(Exception("not authorized: permissions"))
    )
    out = handler._is_complete_config_event(
        {"RequestType": "Update", "PhysicalResourceId": "old", "ResourceProperties": _CONFIG_PROPS},
        client,
    )
    assert out == {"IsComplete": False}


def test_is_complete_unexpected_error_propagates(handler):
    """A non-conflict, non-permissions error must NOT be swallowed as a retry —
    it surfaces so a genuine misconfiguration fails the deploy loudly."""
    client = _FakeClient(
        create_behavior=lambda _k: (_ for _ in ()).throw(ValueError("bad arn"))
    )
    with pytest.raises(ValueError):
        handler._is_complete_config_event(
            {"RequestType": "Update", "PhysicalResourceId": "old",
             "ResourceProperties": _CONFIG_PROPS},
            client,
        )


_EVAL_PROPS = {
    "Kind": "evaluator",
    "EvaluatorName": "proj_sql_grounded",
    "Level": "SESSION",
    "Instructions": "...",
    "JudgeModelId": "global.anthropic.claude-sonnet-5",
    "MaxTokens": 4096,
}


# Evaluators use the CFN-REPLACEMENT strategy (content-hashed names), NOT the
# config waiter strategy. The stack gives a changed-content evaluator a NEW name,
# so CFN creates-new → config re-points → deletes-old-last (unlocked by then).
# Hence: on_event NEVER deletes on Update (that delete-while-referenced is the
# "Cannot delete a locked evaluator" ValidationException that took configs offline
# 2026-07-01), and reuse-by-name on conflict is SAFE (same name ⟹ same content).


def test_evaluator_on_event_update_does_NOT_delete(handler):
    """CRITICAL: on_event(Update) must NOT delete the old evaluator — it is still
    referenced by a live config (locked). CFN deletes it later, after the config
    re-points to the new-named replacement and it unlocks."""
    client = _FakeClient(create_behavior=lambda _k: pytest.fail("no create in on_event"))
    handler._on_evaluator_event(
        {"RequestType": "Update", "PhysicalResourceId": "old-eval", "ResourceProperties": _EVAL_PROPS},
        client,
    )
    assert client.deleted_evaluator_ids == []  # <-- the fix: no delete on Update


def test_evaluator_on_event_delete_deletes_by_id(handler):
    """on_event(Delete) — a genuine CFN Delete (old replacement being reaped) does
    delete the evaluator by id; it is unreferenced by now, so the delete succeeds."""
    client = _FakeClient()
    handler._on_evaluator_event(
        {"RequestType": "Delete", "PhysicalResourceId": "old-eval", "ResourceProperties": _EVAL_PROPS},
        client,
    )
    assert client.deleted_evaluator_ids == ["old-eval"]


def test_evaluator_is_complete_success_returns_new_id(handler):
    """A successful evaluator create returns the new id — the changed id is what
    drives CFN's replacement (config re-points, old deleted last)."""
    client = _FakeClient(create_behavior=lambda _k: {"evaluatorId": "new-eval-id"})
    out = handler._is_complete_evaluator_event(
        {"RequestType": "Create", "ResourceProperties": _EVAL_PROPS}, client
    )
    assert out == {"IsComplete": True, "PhysicalResourceId": "new-eval-id",
                   "Data": {"EvaluatorId": "new-eval-id"}}


def test_evaluator_conflict_reuses_by_name_even_on_update(handler):
    """Reuse-by-name on ConflictException is SAFE for evaluators (unlike configs):
    same content-hashed name ⟹ byte-identical content, so an existing evaluator
    with that name is the one we want. Tolerates idempotent re-deploy / rollback."""
    client = _FakeClient(
        create_behavior=lambda _k: (_ for _ in ()).throw(_Conflict()),
        list_evaluators=[{"evaluatorName": "proj_sql_grounded", "evaluatorId": "identical-existing"}],
    )
    out = handler._is_complete_evaluator_event(
        {"RequestType": "Update", "PhysicalResourceId": "old-eval", "ResourceProperties": _EVAL_PROPS},
        client,
    )
    assert out == {"IsComplete": True, "PhysicalResourceId": "identical-existing",
                   "Data": {"EvaluatorId": "identical-existing"}}


def test_evaluator_conflict_no_existing_waits(handler):
    """ConflictException with the name held by an in-flight delete of an identical
    orphan (not yet listable) → wait for the waiter to retry."""
    client = _FakeClient(
        create_behavior=lambda _k: (_ for _ in ()).throw(_Conflict()),
        list_evaluators=[],
    )
    out = handler._is_complete_evaluator_event(
        {"RequestType": "Create", "ResourceProperties": _EVAL_PROPS}, client
    )
    assert out == {"IsComplete": False}
