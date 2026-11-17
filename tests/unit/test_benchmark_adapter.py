"""Unit tests for the benchmark row model + loader/writer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.shared.benchmark_adapter import (
    BenchmarkRow,
    load_rows,
    write_jsonl,
)


def test_from_dict_maps_known_fields():
    row = BenchmarkRow.from_dict(
        {
            "Natural_Language_Question": "How many policies?",
            "Expected_SQL_Query": "SELECT count(*) FROM holding",
            "db_id": "acme",
            "difficulty": "simple",
            "evidence": "policy = holding",
        }
    )
    assert row.question == "How many policies?"
    assert row.gold_sql == "SELECT count(*) FROM holding"
    assert row.db_id == "acme"
    assert row.difficulty == "simple"
    assert row.evidence == "policy = holding"


def test_from_dict_requires_question():
    # Fail loudly: a row with no question is a corrupt dataset, not a default.
    with pytest.raises(ValueError, match="Natural_Language_Question"):
        BenchmarkRow.from_dict({"Expected_SQL_Query": "SELECT 1"})


def test_load_rows_reads_legacy_json_list(tmp_path: Path):
    p = tmp_path / "gt.json"
    p.write_text(
        json.dumps(
            [
                {"Natural_Language_Question": "q1", "Expected_SQL_Query": "SELECT 1"},
                {"Natural_Language_Question": "q2", "Expected_SQL_Query": "SELECT 2"},
            ]
        )
    )
    rows = load_rows(path=p)
    assert [r.question for r in rows] == ["q1", "q2"]


def test_load_rows_reads_jsonl(tmp_path: Path):
    p = tmp_path / "bird.jsonl"
    p.write_text(
        '{"Natural_Language_Question": "q1", "Expected_SQL_Query": "SELECT 1", "db_id": "d"}\n'
        '{"Natural_Language_Question": "q2", "Expected_SQL_Query": "SELECT 2", "db_id": "d"}\n'
    )
    rows = load_rows(path=p)
    assert len(rows) == 2 and rows[1].db_id == "d"


def test_write_jsonl_roundtrips(tmp_path: Path):
    rows = [BenchmarkRow(question="q", gold_sql="SELECT 1", db_id="d")]
    out = tmp_path / "out.jsonl"
    write_jsonl(rows=rows, path=out)
    back = load_rows(path=out)
    assert back[0].question == "q" and back[0].db_id == "d"
    # Writer must NOT emit an Expected_SQL_Result key (design: drop embedded results).
    assert "Expected_SQL_Result" not in out.read_text()


def test_load_rows_rejects_unknown_suffix(tmp_path):
    p = tmp_path / "data.txt"
    p.write_text("whatever")
    with pytest.raises(ValueError, match="unsupported dataset suffix"):
        load_rows(path=p)


def test_to_dict_omits_empty_optional_fields():
    d = BenchmarkRow(question="q").to_dict()
    assert d == {"Natural_Language_Question": "q", "Expected_SQL_Query": ""}
    assert "db_id" not in d and "evidence" not in d
