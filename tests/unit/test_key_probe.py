"""Tests for the build-time primary-key uniqueness probe.

The probe is what makes a declared key a MEASURED fact instead of another name
guess, so these tests concentrate on the ways a probe could wrongly claim a key:
NULLs hidden by ``COUNT(DISTINCT)``, an empty table where every count is 0, a
failed query read as evidence, and a concatenation collision that makes a
non-unique pair look unique.
"""

from typing import Dict, List

import pytest

from agents.ontology_agent.key_probe import (
    build_pair_probe_sql,
    build_single_column_probe_sql,
    discover_primary_key,
    probe_unique_columns,
)


def _probe_from(rows: List[Dict[str, object]], captured: List[str]):
    """Build a RunProbe returning fixed rows and recording the SQL it saw.

    Args:
        rows: The rows every call returns.
        captured: List the executed SQL is appended to.

    Returns:
        A RunProbe callable.
    """

    def _run(sql: str) -> List[Dict[str, object]]:
        """Record *sql* and return the canned rows."""
        captured.append(sql)
        return rows

    return _run


def _sequenced_probe(responses: List[List[Dict[str, object]]], captured: List[str]):
    """Build a RunProbe returning a different result per successive call.

    Args:
        responses: One result list per call, in order.
        captured: List the executed SQL is appended to.

    Returns:
        A RunProbe callable.
    """
    state = {"i": 0}

    def _run(sql: str) -> List[Dict[str, object]]:
        """Record *sql* and return the next canned result."""
        captured.append(sql)
        result = responses[state["i"]]
        state["i"] += 1
        return result

    return _run


def test_single_column_sql_measures_distinct_and_non_null_per_column() -> None:
    """One scan must answer both halves of the test for every column."""
    sql = build_single_column_probe_sql(
        database="db", table="t", columns=["id", "name"]
    )
    assert 'COUNT(*) AS total' in sql
    assert 'COUNT(DISTINCT "id") AS d0' in sql
    # The non-null count is what detects NULLs, which COUNT(DISTINCT) ignores.
    assert 'COUNT("id") AS n0' in sql
    assert 'COUNT(DISTINCT "name") AS d1' in sql
    assert 'COUNT("name") AS n1' in sql
    assert 'FROM "db"."t"' in sql


def test_identifiers_are_quoted_so_a_reserved_word_table_parses() -> None:
    """An unquoted reserved-word name would make the probe fail, not find no key."""
    sql = build_single_column_probe_sql(
        database="bird_card_games", table="sets", columns=["id"]
    )
    assert 'FROM "bird_card_games"."sets"' in sql


def test_embedded_double_quote_in_a_column_name_is_escaped() -> None:
    """A stray quote must be doubled, not allowed to terminate the identifier."""
    sql = build_single_column_probe_sql(database="db", table="t", columns=['we"ird'])
    assert '"we""ird"' in sql


def test_empty_column_list_raises_rather_than_reporting_no_key() -> None:
    """An aggregate-less probe would be indistinguishable from 'no key exists'."""
    with pytest.raises(ValueError, match="at least one candidate column"):
        build_single_column_probe_sql(database="db", table="t", columns=[])


def test_unique_null_free_column_is_found() -> None:
    """The straightforward case: distinct count and non-null count both equal n."""
    captured: List[str] = []
    probe = _probe_from(
        [{"total": "3", "d0": "3", "n0": "3", "d1": "2", "n1": "3"}], captured
    )
    unique, null_free, row_count = probe_unique_columns(
        database="db", table="t", columns=["id", "grade"], run_probe=probe
    )
    assert unique == ["id"]
    assert null_free == ["id", "grade"]
    assert row_count == 3


def test_column_with_nulls_is_not_a_key_even_when_values_are_distinct() -> None:
    """COUNT(DISTINCT) ignores NULLs, so the count test alone would pass here.

    Ontop cannot template a NULL into an IRI, so those rows would silently
    disappear from the class.
    """
    captured: List[str] = []
    # 4 rows, 3 distinct non-null values, 1 NULL: d == 3, n == 3, total == 4.
    probe = _probe_from([{"total": "4", "d0": "3", "n0": "3"}], captured)
    unique, null_free, _ = probe_unique_columns(
        database="db", table="t", columns=["id"], run_probe=probe
    )
    assert unique == []
    assert null_free == []


def test_empty_table_yields_no_key_because_every_count_is_zero() -> None:
    """With total == 0, a naive 0 == 0 test would mark EVERY column unique."""
    captured: List[str] = []
    probe = _probe_from([{"total": "0", "d0": "0", "n0": "0"}], captured)
    unique, _null_free, row_count = probe_unique_columns(
        database="db", table="t", columns=["anything"], run_probe=probe
    )
    assert unique == []
    assert row_count == 0
    assert discover_primary_key(
        database="db", table="t", columns=["anything"], run_probe=probe
    ) == []


def test_failed_probe_yields_no_key_rather_than_a_guess() -> None:
    """A query that returns nothing must not be read as evidence of uniqueness."""
    captured: List[str] = []
    probe = _probe_from([], captured)
    assert discover_primary_key(
        database="db", table="t", columns=["id"], run_probe=probe
    ) == []


def test_non_numeric_cell_is_not_treated_as_zero() -> None:
    """An unparseable aggregate means 'unknown', never 0 (which could pass)."""
    captured: List[str] = []
    probe = _probe_from([{"total": "5", "d0": "oops", "n0": "5"}], captured)
    unique, _null_free, _ = probe_unique_columns(
        database="db", table="t", columns=["id"], run_probe=probe
    )
    assert unique == []


def test_first_unique_column_in_catalog_order_wins_deterministically() -> None:
    """A stable tie-break matters: a changed key changes every subject IRI."""
    captured: List[str] = []
    rows = [{"total": "2", "d0": "2", "n0": "2", "d1": "2", "n1": "2"}]
    first = discover_primary_key(
        database="db", table="t", columns=["a", "b"], run_probe=_probe_from(rows, captured)
    )
    second = discover_primary_key(
        database="db", table="t", columns=["a", "b"], run_probe=_probe_from(rows, captured)
    )
    assert first == ["a"]
    assert first == second


def test_composite_key_is_found_when_no_single_column_is_unique() -> None:
    """The junction-table shape, and exactly where a single guess is most wrong."""
    captured: List[str] = []
    # Pass 1: neither student_id nor course_id is unique, both null-free.
    # Pass 2: the pair is unique (d0 == total).
    probe = _sequenced_probe(
        [
            [{"total": "4", "d0": "2", "n0": "4", "d1": "2", "n1": "4"}],
            [{"total": "4", "d0": "4"}],
        ],
        captured,
    )
    key = discover_primary_key(
        database="uni",
        table="enrolment",
        columns=["student_id", "course_id"],
        run_probe=probe,
    )
    assert key == ["student_id", "course_id"]
    # The pair probe must have run as a second query.
    assert len(captured) == 2


def test_pair_probe_separates_columns_so_values_cannot_collide() -> None:
    """Without a separator ('a','bc') and ('ab','c') would concatenate equal.

    That collision would understate the distinct count and make a non-unique
    pair look unique.
    """
    sql = build_pair_probe_sql(database="db", table="t", pairs=[("a", "b")])
    assert "CHR(31)" in sql
    assert 'CAST("a" AS VARCHAR)' in sql
    assert 'CAST("b" AS VARCHAR)' in sql


def test_pair_candidates_exclude_columns_already_unique_alone() -> None:
    """A pair containing a unique column is trivially unique but a worse key."""
    captured: List[str] = []
    # `id` is unique alone, so discover_primary_key returns it and never pairs.
    probe = _probe_from(
        [{"total": "3", "d0": "3", "n0": "3", "d1": "1", "n1": "3"}], captured
    )
    key = discover_primary_key(
        database="db", table="t", columns=["id", "kind"], run_probe=probe
    )
    assert key == ["id"]
    assert len(captured) == 1, "must not run a pair probe once a single key is found"


def test_columns_with_nulls_are_never_paired() -> None:
    """Concatenating a NULL yields NULL, which COUNT(DISTINCT) then ignores."""
    captured: List[str] = []
    # Both columns have NULLs (n < total) and neither is unique → no pair is
    # eligible, so no second query runs.
    probe = _probe_from(
        [{"total": "4", "d0": "2", "n0": "3", "d1": "2", "n1": "3"}], captured
    )
    key = discover_primary_key(
        database="db", table="t", columns=["a", "b"], run_probe=probe
    )
    assert key == []
    assert len(captured) == 1


def test_wide_table_is_batched_but_every_column_is_measured() -> None:
    """A skipped column is a key we would silently fail to find."""
    captured: List[str] = []
    columns = [f"c{i}" for i in range(70)]
    # Every batch reports no uniqueness; we only care that all columns appear.
    probe = _probe_from([{"total": "10"}], captured)
    probe_unique_columns(
        database="db", table="t", columns=columns, run_probe=probe
    )
    assert len(captured) > 1, "70 columns should not fit in one batch"
    combined = " ".join(captured)
    for column in columns:
        assert f'"{column}"' in combined, f"{column} was never probed"
