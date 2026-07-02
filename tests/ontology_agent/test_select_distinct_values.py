from unittest.mock import patch
from agents.ontology_agent import main


def test_distinct_probe_returns_values_and_truncation():
    fake = {"success": True, "columns": ["v", "c"],
            "rows": [{"v": "Active", "c": "9"}, {"v": "Closed", "c": "4"}],
            "error": None}
    with patch.object(main, "_resolve_catalog", return_value=("default", "holdings", "cat")), \
         patch.object(main, "_run_athena_query", return_value=fake) as q:
        out = main.select_distinct_values("db", "holdings", "holding_status", "cat")
    assert out["values"] == ["Active", "Closed"]
    assert out["distinct_count"] == 2
    assert out["truncated"] is False
    sql = q.call_args[0][0]
    assert "GROUP BY" in sql and "LIMIT 26" in sql


def test_distinct_probe_flags_truncation_at_limit_plus_one():
    rows = [{"v": f"V{i}", "c": "1"} for i in range(26)]
    fake = {"success": True, "columns": ["v", "c"], "rows": rows, "error": None}
    with patch.object(main, "_resolve_catalog", return_value=("default", "t", "cat")), \
         patch.object(main, "_run_athena_query", return_value=fake):
        out = main.select_distinct_values("db", "t", "col", "cat")
    assert out["truncated"] is True
    assert len(out["values"]) == 25  # overflow row dropped


def test_distinct_probe_failsoft_on_query_error():
    fake = {"success": False, "columns": [], "rows": [], "error": "boom"}
    with patch.object(main, "_resolve_catalog", return_value=("default", "t", "cat")), \
         patch.object(main, "_run_athena_query", return_value=fake):
        out = main.select_distinct_values("db", "t", "col", "cat")
    assert out == {"values": [], "distinct_count": 0, "truncated": False,
                   "sample_rows": 0, "error": "boom"}


def test_distinct_probe_skips_null_values():
    fake = {"success": True, "columns": ["v", "c"],
            "rows": [{"v": "Active", "c": "9"}, {"v": None, "c": "1"}], "error": None}
    with patch.object(main, "_resolve_catalog", return_value=("default", "t", "cat")), \
         patch.object(main, "_run_athena_query", return_value=fake):
        out = main.select_distinct_values("db", "t", "col", "cat")
    assert out["values"] == ["Active"]


def test_distinct_probe_returns_sample_rows():
    from unittest.mock import patch
    from agents.ontology_agent import main
    fake = {"success": True, "columns": ["v", "c"],
            "rows": [{"v": "Active", "c": "150"}, {"v": "Closed", "c": "50"}],
            "error": None}
    with patch.object(main, "_resolve_catalog", return_value=("default", "t", "cat")), \
         patch.object(main, "_run_athena_query", return_value=fake):
        out = main.select_distinct_values("db", "t", "holding_status", "cat")
    assert out["sample_rows"] == 200
    assert out["distinct_count"] == 2


def test_distinct_probe_failsoft_has_sample_rows_zero():
    from unittest.mock import patch
    from agents.ontology_agent import main
    fake = {"success": False, "columns": [], "rows": [], "error": "boom"}
    with patch.object(main, "_resolve_catalog", return_value=("default", "t", "cat")), \
         patch.object(main, "_run_athena_query", return_value=fake):
        out = main.select_distinct_values("db", "t", "c", "cat")
    assert out["sample_rows"] == 0
