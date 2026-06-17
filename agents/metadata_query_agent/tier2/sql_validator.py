"""sqlglot-based parse-only SQL validator.

We don't need full semantic validation here — sqlglot's parser is enough to
catch the kinds of typos and stray tokens an LLM produces, and the orchestrator
will run one repair round on a parse failure before falling through to Tier 3.
"""
from __future__ import annotations

import sqlglot


class SqlSyntaxError(ValueError):
    """Raised when sqlglot rejects the SQL as unparseable."""


def validate_sql(sql: str, *, dialect: str) -> None:
    """Parse ``sql`` under ``dialect``; raise :class:`SqlSyntaxError` on failure.

    Args:
        sql: SQL text to validate.
        dialect: sqlglot dialect identifier (e.g. ``"athena"``).
    """
    try:
        sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError as e:
        raise SqlSyntaxError(str(e)) from e
