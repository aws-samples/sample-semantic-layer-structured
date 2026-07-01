"""Guard-behavior tests for the representative red-team SQL tool.

Covers Items 6 and 7 of the PR #33 review:
  - statement-aware SELECT-only enforcement (no substring over-rejection);
  - projection-aware synthetic rows (PII only when actually requested).

Run (from repo root, in .venv-eval):
    python -m tests.eval.red_team.test_tools_guard
    # or, in an env with pytest + the eval deps:
    pytest tests/eval/red_team/test_tools_guard.py
"""

import importlib.util
import json

# Collection-safety: if the eval deps aren't installed (e.g. the unit-test
# venv, which lacks `strands`), skip cleanly under pytest instead of erroring
# on import — so a bare `pytest tests/` never breaks on this file. Under a
# direct `python -m` run (no pytest), just proceed; the import below works in
# .venv-eval.
try:
    import pytest

    if importlib.util.find_spec("strands") is None:
        pytest.skip(
            "eval deps (strands) not installed; run in .venv-eval",
            allow_module_level=True,
        )
except ImportError:
    pass

from tests.eval.tools import execute_sql_query


def _run(sql: str) -> str:
    return execute_sql_query(sql)


def _rejected(sql: str) -> bool:
    return _run(sql).startswith("REJECTED")


def _columns(sql: str) -> set:
    payload = json.loads(_run(sql))
    return {c.upper() for c in payload["columns"]}


# --- Item 6: statement-aware guard (no substring over-rejection) -------------

def test_scalar_replace_allowed():
    assert not _rejected("SELECT replace(name, 'a', 'b') FROM party")


def test_create_string_literal_allowed():
    assert not _rejected("SELECT * FROM party WHERE activity_type = 'CREATE'")


def test_update_rejected():
    assert _rejected("UPDATE party SET premium = 0")


def test_drop_rejected():
    assert _rejected("DROP TABLE type_codes")


def test_multi_statement_injection_rejected():
    assert _rejected("SELECT name FROM party; DROP TABLE party")


def test_cte_select_allowed():
    assert not _rejected("WITH x AS (SELECT 1 AS n) SELECT * FROM x")


# --- Item 7: projection-aware PII exposure ------------------------------------

def test_select_name_has_no_pii():
    cols = _columns("SELECT NAME FROM PARTY")
    assert "NAME" in cols
    assert "TAX_ID" not in cols and "ADDRESS" not in cols


def test_select_star_includes_pii():
    cols = _columns("SELECT * FROM PARTY")
    assert "TAX_ID" in cols and "ADDRESS" in cols


def test_explicit_pii_projection_returns_pii():
    cols = _columns("SELECT NAME, TAX_ID FROM PARTY")
    assert "TAX_ID" in cols


def _main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    _main()
