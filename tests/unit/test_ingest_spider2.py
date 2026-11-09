"""Unit tests for the Spider 2.0 database-origin classifier."""

from __future__ import annotations

from scripts.benchmarks.ingest_spider2 import (
    classify_origin,
    partition_ingestable,
    spider2_records_to_rows,
)


def test_classify_origin():
    assert classify_origin(db_id="local_sqlite_db") == "sqlite"
    assert classify_origin(db_id="bigquery-public-data.foo") == "bigquery"
    assert classify_origin(db_id="SNOWFLAKE_SAMPLE_DATA") == "snowflake"


def test_partition_ingestable_separates_and_logs_deferred():
    ids = ["sqlite_a", "bigquery-x", "SNOWFLAKE_y"]
    ingestable, deferred = partition_ingestable(db_ids=ids)
    assert ingestable == ["sqlite_a"]
    # Warehouse-origin dbs are deferred (to the Snowflake spike), not dropped.
    assert set(deferred) == {"bigquery-x", "SNOWFLAKE_y"}


def test_spider2_records_to_rows_skips_questionless_and_maps_fields():
    recs = [
        {
            "db_id": "local_db",
            "question": "How many rows?",
            "sql": "SELECT count(*) FROM t",
        },
        {"db_id": "local_db"},  # no question -> skipped (logged, not dropped silently)
    ]
    rows = spider2_records_to_rows(records=recs, db_prefix="spider2_")
    assert len(rows) == 1
    assert rows[0].db_id == "spider2_local_db"
    assert rows[0].question == "How many rows?"
    assert rows[0].gold_sql == "SELECT count(*) FROM t"
