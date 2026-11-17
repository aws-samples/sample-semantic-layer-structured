"""Unit tests for the generic Athena run-and-fetch wrapper."""

from __future__ import annotations

import pytest

from agents.shared.athena_bench import run_sql


class FakeAthena:
    """Minimal boto3-athena stand-in that SUCCEEDS and returns two rows."""

    def __init__(self):
        self.started = None

    def start_query_execution(self, **kwargs):
        self.started = kwargs
        return {"QueryExecutionId": "qid-1"}

    def get_query_execution(self, QueryExecutionId):
        return {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "Statistics": {"EngineExecutionTimeInMillis": 42},
            }
        }

    def get_query_results(self, QueryExecutionId, NextToken=None):
        return {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": "id"}, {"Name": "name"}]},
                "Rows": [
                    {"Data": [{"VarCharValue": "id"}, {"VarCharValue": "name"}]},
                    {"Data": [{"VarCharValue": "1"}, {"VarCharValue": "a"}]},
                    {"Data": [{"VarCharValue": "2"}, {"VarCharValue": "b"}]},
                ],
            }
        }


def test_run_sql_returns_tuples_and_elapsed():
    ath = FakeAthena()
    res = run_sql(
        athena=ath,
        sql="SELECT id, name FROM t",
        workgroup="wg",
        output_loc="",
        database_name="bird_california_schools",
        catalog_id="s3tablescatalog/bench",
        poll_interval_s=0,
    )
    assert res.rows == [("1", "a"), ("2", "b")]
    assert res.columns == ["id", "name"]
    assert res.elapsed_ms == 42
    # Federated catalog + database must be threaded into the execution context.
    ctx = ath.started["QueryExecutionContext"]
    assert ctx["Database"] == "bird_california_schools"
    assert ctx["Catalog"] == "s3tablescatalog/bench"


class FailAthena(FakeAthena):
    def get_query_execution(self, QueryExecutionId):
        return {
            "QueryExecution": {
                "Status": {"State": "FAILED", "StateChangeReason": "boom"}
            }
        }


def test_run_sql_raises_on_failed():
    with pytest.raises(RuntimeError, match="boom"):
        run_sql(
            athena=FailAthena(),
            sql="SELECT 1",
            workgroup="wg",
            output_loc="",
            poll_interval_s=0,
        )


class RunningAthena(FakeAthena):
    """Never reaches SUCCEEDED — forces the timeout path."""

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "RUNNING"}}}


def test_run_sql_times_out():
    with pytest.raises(TimeoutError):
        run_sql(
            athena=RunningAthena(),
            sql="SELECT 1",
            workgroup="wg",
            output_loc="",
            poll_interval_s=0,
            max_wait_s=0,
        )


class NullCellAthena(FakeAthena):
    """A row with a missing VarCharValue (Athena NULL) — must yield None, not KeyError."""

    def get_query_results(self, QueryExecutionId, NextToken=None):
        return {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": "id"}, {"Name": "name"}]},
                "Rows": [
                    {"Data": [{"VarCharValue": "id"}, {"VarCharValue": "name"}]},
                    {"Data": [{"VarCharValue": "1"}, {}]},  # name is NULL
                ],
            }
        }


def test_run_sql_handles_null_cell():
    res = run_sql(
        athena=NullCellAthena(),
        sql="SELECT id, name FROM t",
        workgroup="wg",
        output_loc="",
        poll_interval_s=0,
    )
    assert res.rows == [("1", None)]


class PagedAthena(FakeAthena):
    """Two pages: page 1 has header + 1 row + NextToken; page 2 has 1 row, no token."""

    def get_query_results(self, QueryExecutionId, NextToken=None):
        if NextToken is None:
            return {
                "NextToken": "tok2",
                "ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Name": "id"}]},
                    "Rows": [
                        {"Data": [{"VarCharValue": "id"}]},  # header (page 1 only)
                        {"Data": [{"VarCharValue": "1"}]},
                    ],
                },
            }
        return {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": "id"}]},
                "Rows": [
                    {"Data": [{"VarCharValue": "2"}]},  # page 2: NO header
                ],
            }
        }


def test_run_sql_paginates_all_pages():
    # Must drain NextToken pages — no silent truncation at the page boundary.
    res = run_sql(
        athena=PagedAthena(),
        sql="SELECT id FROM t",
        workgroup="wg",
        output_loc="",
        poll_interval_s=0,
    )
    assert res.rows == [("1",), ("2",)]


def test_run_sql_sends_result_configuration_only_when_output_loc_set():
    a1 = FakeAthena()
    run_sql(athena=a1, sql="SELECT 1", workgroup="wg", output_loc="", poll_interval_s=0)
    assert "ResultConfiguration" not in a1.started
    a2 = FakeAthena()
    run_sql(
        athena=a2,
        sql="SELECT 1",
        workgroup="wg",
        output_loc="s3://bkt/out/",
        poll_interval_s=0,
    )
    assert a2.started["ResultConfiguration"] == {"OutputLocation": "s3://bkt/out/"}
