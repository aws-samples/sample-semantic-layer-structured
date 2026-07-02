"""Unit tests for execute_sql_query's deterministic transient-retry +
ProjectionExpression handling (Fixes B and C).

- Transient connector errors (DynamoDB connector cold-start: 409
  CodeArtifactUserPendingException / "Lambda is initializing", throttling) are
  RETRIED with bounded backoff — the LLM cannot fix these by rewriting SQL.
- Deterministic SQL errors (SCHEMA_NOT_FOUND, syntax) are NOT retried.
- The ProjectionExpression-size error (SELECT */COUNT(*) over a wide DynamoDB
  table) is NOT retried (would loop) and surfaces an actionable error.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agents.metadata_query_agent import main

# The raw function behind the @tool (Strands DecoratedFunctionTool exposes it as
# _tool_func; fall back to __wrapped__ or the object itself if already unwrapped).
_execute = getattr(main.execute_sql_query, "_tool_func", None) \
    or getattr(main.execute_sql_query, "__wrapped__", main.execute_sql_query)


def _fake_athena(state_sequence):
    """Build a fake Athena client whose successive get_query_execution calls return
    the given states. ``state_sequence`` is a list of (State, StateChangeReason).

    start_query_execution returns a fixed execution id; each FAILED→retry restarts
    the sequence position is handled by the caller via a fresh client per attempt
    is NOT needed — we model one client and feed states per poll.
    """
    client = MagicMock()
    client.start_query_execution.return_value = {"QueryExecutionId": "qid-1"}
    seq = list(state_sequence)
    calls = {"i": 0}

    def _get_qe(**_kw):
        st, reason = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        return {"QueryExecution": {"Status": {"State": st, "StateChangeReason": reason}}}

    client.get_query_execution.side_effect = _get_qe
    # Empty result set for a SUCCEEDED path.
    client.get_paginator.return_value.paginate.return_value = [
        {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "c"}]}]}}
    ]
    return client


def _wire(monkeypatch, athena_client):
    """Stub session/SSM/state so _execute runs against the fake Athena client."""
    fake_session = MagicMock()

    def _client(name, *a, **k):
        if name == "athena":
            return athena_client
        return MagicMock()  # ssm etc.

    fake_session.client.side_effect = _client
    monkeypatch.setattr(main, "get_boto_session", lambda: fake_session)
    # Fresh state each call so the cache-guard doesn't short-circuit.
    monkeypatch.setattr(main, "_get_state",
                        lambda: {"query_executed": False, "cached_results": {}})
    # No real sleeping in tests — execute_sql_query does `import time` locally, so
    # patch the stdlib module's sleep directly.
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)


def test_transient_then_success_retries(monkeypatch):
    """A FAILED-with-cold-start poll on attempt 1, SUCCEEDED on attempt 2 → 2
    start_query_execution calls, returns a result (not an error)."""
    client = _fake_athena([
        ("FAILED", "CodeArtifactUserPendingException: Lambda is initializing your function"),
        ("SUCCEEDED", ""),
    ])
    _wire(monkeypatch, client)
    out = json.loads(_execute("SELECT partyid FROM t LIMIT 1", "default", "dynamodb_catalog"))
    assert "error" not in out
    assert client.start_query_execution.call_count == 2  # retried once


def test_transient_exhausts_after_three_attempts(monkeypatch):
    """Persistent transient failure → 3 attempts then a structured error."""
    client = _fake_athena([
        ("FAILED", "CodeArtifactUserPendingException: Lambda is initializing"),
    ])
    _wire(monkeypatch, client)
    out = json.loads(_execute("SELECT partyid FROM t", "default", "dynamodb_catalog"))
    assert "error" in out
    assert client.start_query_execution.call_count == 3


def test_deterministic_error_not_retried(monkeypatch):
    """SCHEMA_NOT_FOUND is deterministic → surfaced immediately, no retry."""
    client = _fake_athena([
        ("FAILED", "SCHEMA_NOT_FOUND: Schema 'x' does not exist"),
    ])
    _wire(monkeypatch, client)
    out = json.loads(_execute("SELECT a FROM x", "x", "dynamodb_catalog"))
    assert "error" in out
    assert client.start_query_execution.call_count == 1  # no retry


def test_projection_size_error_actionable_no_retry(monkeypatch):
    """SELECT */COUNT(*) over a wide DDB table → projection-size error: not retried,
    actionable error mentioning explicit columns."""
    client = _fake_athena([
        ("FAILED", "Invalid ProjectionExpression: Expression size has exceeded "
                   "the maximum allowed size"),
    ])
    _wire(monkeypatch, client)
    out = json.loads(_execute("SELECT * FROM wide", "default", "dynamodb_catalog"))
    assert "error" in out
    assert client.start_query_execution.call_count == 1  # not retried
    assert "column" in out["error"].lower()  # actionable: narrow the projection


def test_success_first_try_no_retry(monkeypatch):
    client = _fake_athena([("SUCCEEDED", "")])
    _wire(monkeypatch, client)
    out = json.loads(_execute("SELECT a FROM t", "default", "dynamodb_catalog"))
    assert "error" not in out
    assert client.start_query_execution.call_count == 1
