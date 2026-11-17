"""Unit tests for the per-row benchmark scorer."""

from __future__ import annotations

import pytest

from agents.shared.benchmark_adapter import BenchmarkRow
from agents.shared.benchmark_scorer import score_row


class ScriptedAthena:
    """Returns queued (rows, ms) results per call; raises when told to."""

    def __init__(self, results):
        self._results = list(results)

    def next(self):
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _runner(scripted):
    """Adapt ScriptedAthena to the run_sql(sql=...) -> AthenaResult seam."""
    from agents.shared.athena_bench import AthenaResult

    def _run(*, sql, **_):
        rows, ms = scripted.next()
        return AthenaResult(columns=[], rows=rows, elapsed_ms=ms)

    return _run


def test_score_row_perfect_match():
    row = BenchmarkRow(question="q", gold_sql="SELECT 1", db_id="bird_x")
    scripted = ScriptedAthena([([(1,)], 40), ([(1,)], 50)])  # pred, gold
    res = score_row(
        row=row,
        predicted_sql="SELECT 1",
        run_sql=_runner(scripted),
        catalog_id="c",
        database_name="bird_x",
    )
    assert res["ex"] == 1 and res["soft_f1"] == 1.0 and res["rves"] == 1.0
    assert res["error"] == ""


def test_score_row_predicted_sql_error_scores_zero_with_reason():
    row = BenchmarkRow(question="q", gold_sql="SELECT 1", db_id="bird_x")
    scripted = ScriptedAthena([RuntimeError("SYNTAX_ERROR: bad"), ([(1,)], 50)])
    res = score_row(
        row=row,
        predicted_sql="SELEC 1",
        run_sql=_runner(scripted),
        catalog_id="c",
        database_name="bird_x",
    )
    assert res["ex"] == 0 and res["rves"] == 0.0
    assert "SYNTAX_ERROR" in res["error"]


def test_score_row_missing_gold_raises():
    # A row with no gold SQL cannot be execution-scored — fail loudly.
    row = BenchmarkRow(question="q", gold_sql="", db_id="bird_x")
    with pytest.raises(ValueError, match="gold"):
        score_row(
            row=row,
            predicted_sql="SELECT 1",
            run_sql=_runner(ScriptedAthena([])),
            catalog_id="c",
            database_name="bird_x",
        )


def test_score_row_does_not_run_gold_when_predicted_fails():
    # Lock in the negative: a predicted failure must NOT consume/run the gold query.
    row = BenchmarkRow(question="q", gold_sql="SELECT 1", db_id="bird_x")
    scripted = ScriptedAthena([RuntimeError("boom"), ([(1,)], 50)])
    score_row(
        row=row,
        predicted_sql="bad",
        run_sql=_runner(scripted),
        catalog_id="c",
        database_name="bird_x",
    )
    # The gold item is still queued — gold was never executed.
    assert len(scripted._results) == 1


def test_score_row_wrong_answer_scores_zero_ex_partial_softf1():
    # The common real path: prediction runs fine but returns the wrong rows.
    row = BenchmarkRow(question="q", gold_sql="SELECT 1", db_id="bird_x")
    # pred rows first, then gold rows. pred={(1,),(2,)}, gold={(1,)} -> ex=0,
    # rves=0 (wrong), soft_f1 in (0,1), error="".
    scripted = ScriptedAthena([([(1,), (2,)], 40), ([(1,)], 50)])
    res = score_row(
        row=row,
        predicted_sql="SELECT x",
        run_sql=_runner(scripted),
        catalog_id="c",
        database_name="bird_x",
    )
    assert res["ex"] == 0
    assert res["rves"] == 0.0
    assert 0.0 < res["soft_f1"] < 1.0
    assert res["error"] == ""


def test_score_row_gold_failure_propagates():
    # A broken gold query is a benchmark-data bug — it must NOT be swallowed.
    row = BenchmarkRow(question="q", gold_sql="SELECT bad", db_id="bird_x")
    scripted = ScriptedAthena([([(1,)], 40), RuntimeError("gold broke")])
    with pytest.raises(RuntimeError, match="gold broke"):
        score_row(
            row=row,
            predicted_sql="SELECT 1",
            run_sql=_runner(scripted),
            catalog_id="c",
            database_name="bird_x",
        )
