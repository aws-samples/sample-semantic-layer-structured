"""Unit tests for shared benchmark-ingestion helpers (pure parts)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.benchmarks.ingest_common import (
    sqlite_table_names,
    transpile_gold,
    Manifest,
    ManifestEntry,
    safe_ident,
    safe_column,
    build_ctas_sql,
    build_drop_sql,
    build_external_table_sql,
    sqlite_to_parquet,
)


def test_safe_ident_accepts_plain_and_rejects_injection():
    assert safe_ident(name="schools") == "schools"
    for bad in ('foo"; DROP TABLE x;--', "has space", "", "1leading"):
        with pytest.raises(ValueError, match="unsafe SQL identifier"):
            safe_ident(name=bad)


def test_safe_column_allows_spaces_and_punctuation():
    # Column names are ONLY emitted backtick-quoted in DDL (and as pyarrow names),
    # so spaces/punctuation/digits-first are safe — backticks neutralize them.
    assert safe_column(name="Academic Year") == "Academic Year"
    assert safe_column(name="col (pct)") == "col (pct)"
    assert safe_column(name="1st_place") == "1st_place"


def test_safe_column_rejects_backtick_and_control_and_empty():
    # Only reject what backtick-quoting cannot neutralize: a backtick, control
    # chars, or an empty name.
    for bad in ("has`tick", "line\nbreak", "nul\x00byte", ""):
        with pytest.raises(ValueError, match="unsafe column name"):
            safe_column(name=bad)


def test_safe_ident_still_strict_for_tables():
    # Table/db/namespace identifiers are emitted UNQUOTED, so they MUST stay
    # strict — a space-containing name still raises under safe_ident.
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        safe_ident(name="Academic Year")


def _make_db(path: Path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schools (id INTEGER, name TEXT)")
    con.execute("INSERT INTO schools VALUES (1, 'a'), (2, 'b')")
    con.commit()
    con.close()


def test_sqlite_table_names(tmp_path: Path):
    db = tmp_path / "d.sqlite"
    _make_db(db)
    assert sqlite_table_names(db_path=db) == ["schools"]


def test_transpile_sqlite_to_trino_ok():
    ok, sql, err = transpile_gold(
        gold_sql="SELECT name FROM schools LIMIT 1", read="sqlite", write="trino"
    )
    assert ok is True and "schools" in sql and err == ""


def test_transpile_failure_is_captured_not_raised():
    # A parse failure must be reported (ok=False + reason), never silently dropped.
    ok, sql, err = transpile_gold(
        gold_sql="SELECT FROM WHERE ;;;", read="sqlite", write="trino"
    )
    assert ok is False and err != ""


def test_manifest_roundtrip(tmp_path: Path):
    m = Manifest(benchmark="bird")
    m.add(
        entry=ManifestEntry(
            db_id="california_schools",
            database="bird_california_schools",
            catalog="s3tablescatalog/bench",
            tables=["schools"],
            row_counts={"schools": 2},
            transpile_ok=True,
        )
    )
    p = tmp_path / "manifest.json"
    m.save(path=p)
    back = Manifest.load(path=p)
    assert back.benchmark == "bird"
    assert back.entry(db_id="california_schools").database == "bird_california_schools"


def test_manifest_add_dedupes_by_db_id():
    # A resumed deploy re-ingests + re-adds the same db_id (manifest is written
    # before the resumable state commits). add() must REPLACE, not duplicate:
    # two adds with the same db_id => one entry, the second one.
    m = Manifest(benchmark="bird")
    m.add(
        entry=ManifestEntry(
            db_id="bird_superhero",
            database="bird_superhero",
            catalog="s3tablescatalog/bench",
            tables=["superhero"],
            row_counts={"superhero": 1},
            transpile_ok=True,
        )
    )
    m.add(
        entry=ManifestEntry(
            db_id="bird_superhero",
            database="bird_superhero",
            catalog="s3tablescatalog/bench",
            tables=["superhero", "hero_power"],
            row_counts={"superhero": 750, "hero_power": 10},
            transpile_ok=False,
        )
    )
    assert len(m.entries) == 1  # deduped, not appended
    kept = m.entry(db_id="bird_superhero")
    assert kept.row_counts == {"superhero": 750, "hero_power": 10}  # the 2nd wins
    assert kept.transpile_ok is False


def test_manifest_entry_missing_raises():
    m = Manifest(benchmark="bird")
    with pytest.raises(KeyError, match="no manifest entry for db_id=nope"):
        m.entry(db_id="nope")


def test_build_external_table_sql_points_at_staging_prefix():
    sql = build_external_table_sql(
        database="bench_stage",
        table="schools",
        columns=[("id", "bigint"), ("name", "varchar")],
        s3_prefix="s3://bkt/stage/bird/california_schools/schools/",
    )
    assert "CREATE EXTERNAL TABLE" in sql
    assert "s3://bkt/stage/bird/california_schools/schools/" in sql
    assert "PARQUET" in sql.upper()
    # Hive DDL dialect: backtick identifiers + Hive type names (varchar -> string).
    # Double-quoted identifiers route to the Trino parser which rejects EXTERNAL.
    assert "`bench_stage`.`schools`" in sql
    assert "`name` string" in sql  # varchar mapped to Hive string
    assert "`id` bigint" in sql
    assert '"' not in sql  # no double-quoted identifiers


def test_build_ctas_sql_targets_iceberg_namespace():
    sql = build_ctas_sql(
        target_db="bird_california_schools",
        target_table="schools",
        source_db="bench_stage",
        source_table="schools",
    )
    assert "CREATE TABLE" in sql
    assert '"bird_california_schools"."schools"' in sql
    assert "table_type" in sql.lower() and "iceberg" in sql.lower()
    assert "is_external = false" in sql.lower()


def test_build_ctas_sql_omits_with_when_table_type_blank():
    # Task 0 spike may find the S3 Tables catalog rejects table_type — allow omit.
    sql = build_ctas_sql(
        target_db="d", target_table="t", source_db="s", source_table="t", table_type=""
    )
    assert "table_type" not in sql.lower()


def test_build_ctas_sql_qualifies_source_with_catalog():
    sql = build_ctas_sql(
        target_db="bird_x",
        target_table="t",
        source_db="stage",
        source_table="t",
        source_catalog="AwsDataCatalog",
    )
    assert '"AwsDataCatalog"."stage"."t"' in sql


def test_build_drop_sql_is_if_exists():
    assert build_drop_sql(target_db="d", target_table="t") == "DROP TABLE IF EXISTS d.t"


def test_sqlite_to_parquet_writes_file(tmp_path):
    import sqlite3

    import pyarrow.parquet as pq

    db = tmp_path / "d.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    con.execute("INSERT INTO t VALUES (1,'a'),(2,'b')")
    con.commit()
    con.close()
    out = tmp_path / "t.parquet"
    cols = sqlite_to_parquet(db_path=db, table="t", out_path=out)
    assert out.exists()
    assert pq.read_table(out).num_rows == 2
    assert [c[0] for c in cols] == ["id", "name"]


def test_sqlite_to_parquet_mixed_type_and_null_columns(tmp_path):
    # SQLite is dynamically typed: a declared-INTEGER column can hold a string,
    # and a column can be entirely NULL. Naive pa.table() raises ArrowInvalid /
    # emits a null-typed column. The writer must coerce to the declared type
    # (falling back to string) so real BIRD data ingests without crashing.
    import sqlite3

    import pyarrow.parquet as pq

    db = tmp_path / "m.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (n INTEGER, note TEXT)")
    con.execute(
        "INSERT INTO t VALUES (1, NULL), ('oops', NULL)"
    )  # mixed n, all-NULL note
    con.commit()
    con.close()
    out = tmp_path / "m.parquet"
    cols = sqlite_to_parquet(db_path=db, table="t", out_path=out)
    tbl = pq.read_table(out)
    assert tbl.num_rows == 2  # no crash
    assert dict(cols)["note"] == "varchar"


def test_build_ddl_rejects_unsafe_table_name():
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        build_external_table_sql(
            database="stage",
            table='x"; DROP',
            columns=[("id", "bigint")],
            s3_prefix="s3://b/p/",
        )


def test_sqlite_to_parquet_roundtrips_cell_values(tmp_path):
    import sqlite3

    import pyarrow.parquet as pq

    db = tmp_path / "r.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    con.execute("INSERT INTO t VALUES (1,'a'),(2,'b')")
    con.commit()
    con.close()
    out = tmp_path / "r.parquet"
    sqlite_to_parquet(db_path=db, table="t", out_path=out)
    assert pq.read_table(out).to_pydict() == {"id": [1, 2], "name": ["a", "b"]}


def test_sqlite_to_parquet_all_null_numeric_column_stays_numeric(tmp_path):
    import sqlite3

    import pyarrow.parquet as pq

    db = tmp_path / "n.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (n INTEGER)")
    con.execute("INSERT INTO t VALUES (NULL),(NULL)")
    con.commit()
    con.close()
    out = tmp_path / "n.parquet"
    cols = sqlite_to_parquet(db_path=db, table="t", out_path=out)
    assert dict(cols)["n"] == "bigint"  # stays numeric, no downgrade
    assert pq.read_table(out).num_rows == 2  # no crash on all-NULL


def test_sqlite_to_parquet_bigint_overflow_downgrades_to_varchar(tmp_path):
    import sqlite3

    import pyarrow.parquet as pq

    db = tmp_path / "o.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (big INTEGER)")
    # 20-digit value stored as TEXT: Python int() parses it (arbitrary precision)
    # but it overflows int64 — must downgrade to varchar, not crash.
    con.execute("INSERT INTO t VALUES ('99999999999999999999')")
    con.commit()
    con.close()
    out = tmp_path / "o.parquet"
    cols = sqlite_to_parquet(db_path=db, table="t", out_path=out)
    assert dict(cols)["big"] == "varchar"
    assert pq.read_table(out).num_rows == 1


def test_sqlite_to_parquet_and_ddl_allow_spaced_column(tmp_path):
    # BIRD ships column names with spaces (e.g. "Academic Year"). Such a name is
    # emitted only as a pyarrow column name and a BACKTICK-quoted Hive identifier,
    # so ingestion must NOT reject it. Regression test for the safe_ident->
    # safe_column column fix.
    import sqlite3

    import pyarrow.parquet as pq

    db = tmp_path / "s.sqlite"
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE t (id INTEGER, "Academic Year" TEXT)')
    con.execute("INSERT INTO t VALUES (1, '2019-2020'), (2, '2020-2021')")
    con.commit()
    con.close()
    out = tmp_path / "s.parquet"
    cols = sqlite_to_parquet(db_path=db, table="t", out_path=out)
    assert [c[0] for c in cols] == ["id", "Academic Year"]
    assert pq.read_table(out).num_rows == 2

    sql = build_external_table_sql(
        database="bench_stage",
        table="t",
        columns=cols,
        s3_prefix="s3://bkt/stage/t/",
    )
    # The spaced column must be emitted backtick-quoted so the DDL is valid.
    assert "`Academic Year` string" in sql


def test_sqlite_to_parquet_empty_table(tmp_path):
    import sqlite3

    import pyarrow.parquet as pq

    db = tmp_path / "e.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    con.commit()
    con.close()
    out = tmp_path / "e.parquet"
    cols = sqlite_to_parquet(db_path=db, table="t", out_path=out)
    assert [c[0] for c in cols] == ["id", "name"]
    assert pq.read_table(out).num_rows == 0
