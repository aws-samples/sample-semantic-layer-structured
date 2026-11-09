"""Unit tests for the generic Snowflake run-and-fetch wrapper.

Hermetic: every test drives a hand-rolled connection/cursor stub, so the suite
needs neither ``snowflake-connector-python`` nor network access — the driver is
imported lazily inside ``connect()``, which these tests do not exercise.

The point of this module is that a Snowflake run scores identically to an Athena
run, so the assertions here are mostly about matching ``athena_bench``'s contract:
tuple rows, drained result sets, ``RuntimeError`` on failure, ``TimeoutError`` on
cancellation.
"""

from __future__ import annotations

import pytest

from agents.shared.exec_metrics import execution_accuracy
from agents.shared.snowflake_bench import SnowflakeResult, run_sql


class FakeCursor:
    """Minimal Snowflake cursor stub returning two rows of native Python types."""

    def __init__(self, rows=None, description=None, fail=None):
        self.executed = []
        self.closed = False
        self.sfqid = "01b2c3d4-0000-0000"
        self._rows = list(
            rows if rows is not None else [(1, "a"), (2, "b")]
        )
        self.description = description or [("ID", 0), ("NAME", 2)]
        self._fail = fail

    def execute(self, sql, timeout=None):
        self.executed.append((sql, timeout))
        if self._fail is not None and not sql.startswith("USE "):
            raise self._fail
        return self

    def fetchmany(self, size):
        batch, self._rows = self._rows[:size], self._rows[size:]
        return batch

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        self.closed = True


class FakeConn:
    """Connection stub handing out a scripted cursor, then stats cursors."""

    def __init__(self, cursor=None, stats_row=None):
        self._cursor = cursor or FakeCursor()
        self._stats_row = stats_row
        self.cursors = []

    def cursor(self):
        if self.cursors and self._stats_row is not None:
            # Second and later cursors serve the QUERY_HISTORY stats lookup.
            cur = FakeCursor(rows=[self._stats_row], description=[("EXECUTION_TIME", 0)])
            self.cursors.append(cur)
            return cur
        self.cursors.append(self._cursor)
        return self._cursor


def test_run_sql_returns_tuples_and_wall_clock_elapsed():
    cur = FakeCursor()
    conn = FakeConn(cursor=cur)
    res = run_sql(
        conn=conn,
        sql="SELECT id, name FROM t",
        database_name="SPIDER2_LOCAL",
        schema_name="PUBLIC",
    )
    assert res.rows == [(1, "a"), (2, "b")]
    assert res.columns == ["ID", "NAME"]
    assert res.query_id == "01b2c3d4-0000-0000"
    # Without server_stats the clock is client wall-clock, and that must be stated
    # rather than inferred — R-VES against Athena is only valid on engine time.
    assert res.server_time is False
    assert res.elapsed_ms >= 0
    assert res.bytes_scanned is None


def test_run_sql_sets_session_context_before_the_query():
    # The Snowflake analogue of Athena's QueryExecutionContext: without it the
    # query resolves against whatever database the connection happened to open on.
    cur = FakeCursor()
    run_sql(
        conn=FakeConn(cursor=cur),
        sql="SELECT 1",
        database_name="SPIDER2_DB",
        schema_name="MAIN",
    )
    statements = [s for s, _ in cur.executed]
    assert statements[:2] == ["USE DATABASE SPIDER2_DB", "USE SCHEMA MAIN"]
    assert statements[2] == "SELECT 1"


def test_run_sql_omits_use_statements_when_no_context_given():
    cur = FakeCursor()
    run_sql(conn=FakeConn(cursor=cur), sql="SELECT 1")
    assert [s for s, _ in cur.executed] == ["SELECT 1"]


@pytest.mark.parametrize(
    "bad", ["FOO; DROP TABLE t", "foo bar", 'a"b', "db--x", ""]
)
def test_run_sql_refuses_non_identifier_database(bad):
    # USE takes an identifier, so there is no bind parameter to hide behind; an
    # unvalidated manifest value would be directly injectable. Empty string is the
    # documented "no context" case and must NOT raise.
    cur = FakeCursor()
    if bad == "":
        run_sql(conn=FakeConn(cursor=cur), sql="SELECT 1", database_name=bad)
        return
    with pytest.raises(ValueError, match="not a bare Snowflake identifier"):
        run_sql(conn=FakeConn(cursor=cur), sql="SELECT 1", database_name=bad)


def test_run_sql_raises_runtime_error_on_failure():
    # Same exception type athena_bench raises, so a caller can swap executors
    # without reworking its error handling.
    boom = Exception("SQL compilation error: invalid identifier 'NOPE'")
    conn = FakeConn(cursor=FakeCursor(fail=boom))
    with pytest.raises(RuntimeError, match="invalid identifier"):
        run_sql(conn=conn, sql="SELECT nope FROM t")


def test_run_sql_raises_timeout_error_on_cancellation():
    class Cancelled(Exception):
        errno = 604

    conn = FakeConn(cursor=FakeCursor(fail=Cancelled("SQL execution canceled")))
    with pytest.raises(TimeoutError, match="unfinished in"):
        run_sql(conn=conn, sql="SELECT 1", max_wait_s=5)


def test_run_sql_passes_max_wait_as_statement_timeout():
    # Snowflake enforces this server-side; without it a runaway benchmark query
    # would burn warehouse credits until the driver's own socket timeout.
    cur = FakeCursor()
    run_sql(conn=FakeConn(cursor=cur), sql="SELECT 1", max_wait_s=30)
    assert cur.executed[-1] == ("SELECT 1", 30)


def test_run_sql_drains_every_batch():
    # Mirrors athena_bench's NextToken drain: a truncated result set corrupts EX
    # scoring, turning a correct answer into what looks like a model failure.
    rows = [(i,) for i in range(2500)]
    cur = FakeCursor(rows=rows, description=[("N", 0)])
    res = run_sql(conn=FakeConn(cursor=cur), sql="SELECT n FROM t", fetch_size=1000)
    assert len(res.rows) == 2500
    assert res.rows[-1] == (2499,)


def test_run_sql_closes_the_cursor_even_when_the_query_fails():
    cur = FakeCursor(fail=Exception("nope"))
    with pytest.raises(RuntimeError):
        run_sql(conn=FakeConn(cursor=cur), sql="SELECT 1")
    assert cur.closed is True


def test_server_stats_replaces_wall_clock_and_flags_the_clock():
    cur = FakeCursor()
    conn = FakeConn(cursor=cur, stats_row=(1234, 987654, 0.0042))
    res = run_sql(conn=conn, sql="SELECT 1", server_stats=True)
    assert res.elapsed_ms == 1234
    assert res.server_time is True
    assert res.bytes_scanned == 987654
    assert res.credits_cloud_services == pytest.approx(0.0042)


def test_server_stats_falls_back_to_wall_clock_when_unavailable():
    # The QUERY_HISTORY row can lag the query, and INFORMATION_SCHEMA needs a
    # database in session context. Losing telemetry must not fail the row.
    class NoStatsConn(FakeConn):
        def cursor(self):
            if self.cursors:
                raise Exception("INFORMATION_SCHEMA unavailable")
            self.cursors.append(self._cursor)
            return self._cursor

    res = run_sql(conn=NoStatsConn(cursor=FakeCursor()), sql="SELECT 1", server_stats=True)
    assert res.server_time is False
    assert res.elapsed_ms >= 0
    assert res.bytes_scanned is None


def test_result_scores_through_exec_metrics_against_athena_string_rows():
    # The whole reason this class mirrors AthenaResult: exec_metrics must score a
    # Snowflake run (native ints/Decimals) identically to an Athena run (strings).
    from decimal import Decimal

    sf = SnowflakeResult(
        columns=["n", "amt"], rows=[(1, Decimal("1.50")), (2, Decimal("0.30"))], elapsed_ms=5
    )
    athena_rows = [("1", "1.5"), ("2", "0.3")]
    assert execution_accuracy(pred_rows=sf.rows, gold_rows=athena_rows) == 1
