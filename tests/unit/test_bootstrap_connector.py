"""Unit tests for scripts/snowflake/bootstrap_connector.py.

Only the pure helpers are covered: everything else opens a Snowflake session, which is
what the script's own `--verify-only` mode is for.

The module imports `snowflake.connector` and `cryptography` at top level, and
snowflake-connector-python is deliberately NOT a project dependency (the script runs from
a throwaway venv — see cdk/README.md §3). Stub modules are therefore installed in
`sys.modules` before the import so the pure functions are reachable without the driver.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module() -> types.ModuleType:
    """Import bootstrap_connector with its optional third-party imports stubbed out.

    :return: the imported module object
    """
    if "snowflake.connector" not in sys.modules:
        snowflake = types.ModuleType("snowflake")
        connector = types.ModuleType("snowflake.connector")
        connector.connect = lambda **_kwargs: None  # never called by these tests
        snowflake.connector = connector
        sys.modules.setdefault("snowflake", snowflake)
        sys.modules["snowflake.connector"] = connector

    sys.path.insert(0, str(REPO_ROOT / "scripts" / "snowflake"))
    try:
        import bootstrap_connector

        return bootstrap_connector
    finally:
        sys.path.pop(0)


bootstrap = _load_module()


def test_parse_env_reads_plain_and_quoted_values(tmp_path: Path) -> None:
    """A plain value, a double-quoted value with \\n, and a comment line."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "SNOWFLAKE_ACCOUNT=myorg-myacct\n"
        'SNOWFLAKE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\\nBODY\\n-----END PRIVATE KEY-----"\n'
        "\n"
    )

    env = bootstrap.parse_env(path=env_file)

    assert env["SNOWFLAKE_ACCOUNT"] == "myorg-myacct"
    assert env["SNOWFLAKE_PRIVATE_KEY"] == (
        "-----BEGIN PRIVATE KEY-----\nBODY\n-----END PRIVATE KEY-----"
    )
    assert "# a comment" not in env


def test_parse_env_keeps_trailing_comment_verbatim(tmp_path: Path) -> None:
    """Documents the trap: an inline comment becomes part of the value.

    This mirrors cdk/bin/env-config.ts exactly. It is asserted rather than fixed so the
    Python and TypeScript parsers cannot drift: a value that works for `cdk deploy` must
    parse identically here.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=hunter2  # my passphrase\n")

    env = bootstrap.parse_env(path=env_file)

    assert env["SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"] == "hunter2  # my passphrase"


def test_require_raises_on_missing_and_blank_keys() -> None:
    """Fail loudly: neither an absent key nor a whitespace-only value may pass."""
    with pytest.raises(RuntimeError, match="SNOWFLAKE_ADMIN_USER"):
        bootstrap.require(env={}, key="SNOWFLAKE_ADMIN_USER")

    with pytest.raises(RuntimeError, match="SNOWFLAKE_ADMIN_PASSWORD"):
        bootstrap.require(
            env={"SNOWFLAKE_ADMIN_PASSWORD": "   "}, key="SNOWFLAKE_ADMIN_PASSWORD"
        )


def test_require_returns_stripped_value() -> None:
    """A present value is returned with surrounding whitespace removed."""
    assert bootstrap.require(env={"K": " v "}, key="K") == "v"


def test_split_statements_drops_comments_and_empties() -> None:
    """Comment lines and trailing semicolons must not become statements."""
    sql = (
        "-- a leading comment\n"
        "USE ROLE ACCOUNTADMIN;\n"
        "\n"
        "  -- indented comment\n"
        "CREATE ROLE IF NOT EXISTS R;\n"
        ";\n"
    )

    statements = bootstrap.split_statements(sql=sql)

    assert statements == ["USE ROLE ACCOUNTADMIN", "CREATE ROLE IF NOT EXISTS R"]


def test_label_elides_long_key_literals() -> None:
    """A public key body must not be echoed to the terminal in progress output."""
    key_body = "A" * 60
    statement = f"ALTER USER ATHENA_CONNECTOR SET RSA_PUBLIC_KEY = '{key_body}'"

    rendered = bootstrap.label(statement=statement)

    assert key_body not in rendered
    assert "'<public key>'" in rendered


def test_label_collapses_whitespace_and_truncates() -> None:
    """Multi-line DDL renders as one short line."""
    statement = (
        "CREATE USER IF NOT EXISTS ATHENA_CONNECTOR\n  TYPE = SERVICE\n  " + "X" * 200
    )

    rendered = bootstrap.label(statement=statement)

    assert "\n" not in rendered
    assert len(rendered) <= 88
    assert rendered.startswith(
        "CREATE USER IF NOT EXISTS ATHENA_CONNECTOR TYPE = SERVICE"
    )
