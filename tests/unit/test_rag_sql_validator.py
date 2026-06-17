"""Tests for the SQL validator (sqlglot wrapper)."""
import pytest

from agents.metadata_query_agent.tier2.sql_validator import (
    SqlSyntaxError, validate_sql,
)


def test_valid_select_passes():
    validate_sql("SELECT 1", dialect="athena")


def test_invalid_raises():
    with pytest.raises(SqlSyntaxError):
        validate_sql("SELEC 1", dialect="athena")
