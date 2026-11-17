"""Unit tests for the BIRD dataset → BenchmarkRow builder."""

from __future__ import annotations

from scripts.benchmarks.ingest_bird import bird_records_to_rows


def test_bird_records_to_rows_maps_fields_and_prefixes_db():
    recs = [
        {
            "db_id": "california_schools",
            "question": "How many schools?",
            "evidence": "school = campus",
            "SQL": "SELECT count(*) FROM schools",
            "difficulty": "simple",
        }
    ]
    rows = bird_records_to_rows(records=recs, db_prefix="bird_")
    assert rows[0].question == "How many schools?"
    assert rows[0].gold_sql == "SELECT count(*) FROM schools"
    assert rows[0].evidence == "school = campus"
    assert rows[0].difficulty == "simple"
    # db_id is namespaced so BIRD tables never collide with the insurance schema.
    assert rows[0].db_id == "bird_california_schools"
