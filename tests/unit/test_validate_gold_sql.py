"""Gold-SQL validity probe: per-db scenario ids, catalog binding, failure capture.

The point of this script is to separate two causes that AgentCore reports
identically (``score: null``): a trace that had not landed (real telemetry loss)
versus a gold query that cannot run on Athena at all (a permanent dataset defect).
If the scenario ids it emits do not match the ids notebook 12 assigns, the sidecar
silently excludes the WRONG rows — so that mapping is the primary thing under test.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts" / "benchmarks" / "validate_gold_sql.py"
)


def _load_module() -> Any:
    """Import validate_gold_sql.py under a unique module name.

    Returns:
        The imported module object.
    """
    spec = importlib.util.spec_from_file_location("validate_gold_sql_mod", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = _load_module()


def _write_benchmark(tmp_path: Path, *, rows: list, entries: list) -> Path:
    """Write a minimal benchmark tree (jsonl + manifest) and return the repo root.

    Args:
        tmp_path: pytest temp dir standing in for the repo root.
        rows: Records for ``<benchmark>.jsonl``.
        entries: Manifest entries.

    Returns:
        The fake repo root path.
    """
    bench_dir = tmp_path / "data" / "eval" / "benchmarks" / "bird"
    bench_dir.mkdir(parents=True)
    with (bench_dir / "bird.jsonl").open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    (bench_dir / "manifest.json").write_text(
        json.dumps({"benchmark": "bird", "entries": entries})
    )
    return tmp_path


def _row(db: str, q: str, sql: str) -> Dict[str, str]:
    """Build one benchmark jsonl record.

    Args:
        db: The db_id.
        q: The natural-language question.
        sql: The gold SQL.

    Returns:
        A record in the benchmark's jsonl shape.
    """
    return {
        "Natural_Language_Question": q,
        "Expected_SQL_Query": sql,
        "db_id": db,
        "difficulty": "simple",
        "evidence": "",
    }


def test_scenario_ids_are_indexed_within_each_database(tmp_path: Path) -> None:
    """gt-row-NN must restart at 00 per db_id — notebook 12 scopes a run to one db.

    This is the correctness crux: BIRD's jsonl interleaves databases, so a
    file-global index would map every exclusion onto the wrong row.
    """
    root = _write_benchmark(
        tmp_path,
        rows=[
            _row("bird_a", "qa1", "SELECT 1"),
            _row("bird_b", "qb1", "SELECT 1"),
            _row("bird_a", "qa2", "SELECT 1"),
            _row("bird_b", "qb2", "SELECT 1"),
            _row("bird_a", "qa3", "SELECT 1"),
        ],
        entries=[
            {"db_id": "bird_a", "catalog": "cat_a"},
            {"db_id": "bird_b", "catalog": "cat_b"},
        ],
    )
    with patch.object(MOD, "REPO_ROOT", root):
        rows = MOD.load_rows(benchmark="bird")

    got = [(r["db_id"], r["scenario_id"], r["question"]) for r in rows]
    assert got == [
        ("bird_a", "gt-row-00", "qa1"),
        ("bird_b", "gt-row-00", "qb1"),
        ("bird_a", "gt-row-01", "qa2"),
        ("bird_b", "gt-row-01", "qb2"),
        ("bird_a", "gt-row-02", "qa3"),
    ]


def test_load_catalogs_maps_every_db(tmp_path: Path) -> None:
    """The probe must run each query in ITS OWN catalog, not a shared default."""
    root = _write_benchmark(
        tmp_path,
        rows=[_row("bird_a", "q", "SELECT 1")],
        entries=[
            {"db_id": "bird_a", "catalog": "cat_a"},
            {"db_id": "bird_b", "catalog": "cat_b"},
        ],
    )
    with patch.object(MOD, "REPO_ROOT", root):
        assert MOD.load_catalogs(benchmark="bird") == {
            "bird_a": "cat_a", "bird_b": "cat_b",
        }


def test_missing_jsonl_raises(tmp_path: Path) -> None:
    """A missing dataset must fail loudly, not silently probe zero rows."""
    (tmp_path / "data" / "eval" / "benchmarks" / "bird").mkdir(parents=True)
    with patch.object(MOD, "REPO_ROOT", tmp_path):
        with pytest.raises(FileNotFoundError, match="ingest script"):
            MOD.load_rows(benchmark="bird")


def _athena_stub(states: list, *, reason: str = "") -> MagicMock:
    """Build a boto3 Athena client stub that walks the given states.

    Args:
        states: The sequence of query states to return from get_query_execution.
        reason: StateChangeReason to attach on the terminal poll.

    Returns:
        A MagicMock Athena client.
    """
    client = MagicMock()
    client.start_query_execution.return_value = {"QueryExecutionId": "q1"}
    client.get_query_execution.side_effect = [
        {"QueryExecution": {"Status": {"State": s, "StateChangeReason": reason}}}
        for s in states
    ]
    return client


def test_probe_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gold query that SUCCEEDS marks the row answerable with no reason text."""
    client = _athena_stub(["RUNNING", "SUCCEEDED"])
    monkeypatch.setattr(MOD, "POLL_INTERVAL_SECS", 0)
    monkeypatch.setattr(
        MOD.boto3, "Session", lambda: MagicMock(client=lambda _s: client)
    )
    row = {"db_id": "bird_a", "scenario_id": "gt-row-00", "gold_sql": "SELECT 1"}
    _, ok, reason = MOD.probe_one(row=row, catalog="cat_a", workgroup="primary")
    assert ok is True
    assert reason == ""
    # The query must be bound to the row's own database AND catalog.
    ctx = client.start_query_execution.call_args.kwargs["QueryExecutionContext"]
    assert ctx == {"Database": "bird_a", "Catalog": "cat_a"}


def test_probe_captures_the_athena_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FAILED gold query records WHY — the reason is the evidence for exclusion.

    Without the reason text an exclusion is unauditable: we could not have shown
    that BIRD's failures are varchar typing rather than our own ingestion.
    """
    client = _athena_stub(
        ["FAILED"], reason="TYPE_MISMATCH: Cannot apply operator: varchar = integer"
    )
    monkeypatch.setattr(MOD, "POLL_INTERVAL_SECS", 0)
    monkeypatch.setattr(
        MOD.boto3, "Session", lambda: MagicMock(client=lambda _s: client)
    )
    row = {"db_id": "bird_a", "scenario_id": "gt-row-00", "gold_sql": "SELECT x = 1"}
    _, ok, reason = MOD.probe_one(row=row, catalog="cat_a", workgroup="primary")
    assert ok is False
    assert "TYPE_MISMATCH" in reason


def test_probe_treats_a_rejected_submission_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQL Athena refuses to even start is invalid gold, not a crash."""
    client = MagicMock()
    client.start_query_execution.side_effect = ValueError("bad syntax")
    monkeypatch.setattr(
        MOD.boto3, "Session", lambda: MagicMock(client=lambda _s: client)
    )
    row = {"db_id": "bird_a", "scenario_id": "gt-row-00", "gold_sql": "SELCT"}
    _, ok, reason = MOD.probe_one(row=row, catalog="cat_a", workgroup="primary")
    assert ok is False
    assert "rejected" in reason


def test_probe_does_not_hang_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gold query that never settles is unusable and must be reported, not awaited."""
    client = MagicMock()
    client.start_query_execution.return_value = {"QueryExecutionId": "q1"}
    client.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "RUNNING"}}
    }
    monkeypatch.setattr(MOD, "POLL_INTERVAL_SECS", 0)
    monkeypatch.setattr(MOD, "POLL_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(
        MOD.boto3, "Session", lambda: MagicMock(client=lambda _s: client)
    )
    row = {"db_id": "bird_a", "scenario_id": "gt-row-00", "gold_sql": "SELECT 1"}
    _, ok, reason = MOD.probe_one(row=row, catalog="cat_a", workgroup="primary")
    assert ok is False
    assert "did not settle" in reason
